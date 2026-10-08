import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import services
from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .models import Certificate, CertStatus, Job, JobStatus
from .schemas import CertificateOut, CertificatePage, JobCreate, JobOut

# In-process worker pool. Jobs are persisted in the DB first, so a crash loses no data
# (unfinished jobs are re-queued at startup). See README for the trade-offs vs Celery.
_executor: ThreadPoolExecutor | None = None


def submit_job(job_id: str) -> None:
    if settings.sync_processing:
        services.process_job(job_id)
    else:
        _executor.submit(services.process_job, job_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _executor
    Base.metadata.create_all(engine)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    _executor = ThreadPoolExecutor(max_workers=settings.worker_threads, thread_name_prefix="certgen")
    # Recovery: resume jobs interrupted by a restart (process_job only touches PENDING rows).
    with SessionLocal() as db:
        for job_id in db.scalars(select(Job.id).where(Job.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]))):
            _executor.submit(services.process_job, job_id)
    yield
    _executor.shutdown(wait=True, cancel_futures=False)


app = FastAPI(title="Bulk Certificate Generator", version="1.0.0", lifespan=lifespan)


def _job_out(db: Session, job: Job, request: Request) -> JobOut:
    base = f"/jobs/{job.id}"
    return JobOut(
        id=job.id, event_name=job.event_name, issued_by=job.issued_by, issue_date=job.issue_date,
        status=job.status, progress=services.get_progress(db, job), error=job.error,
        created_at=job.created_at, completed_at=job.completed_at,
        links={"self": base, "certificates": f"{base}/certificates", "download_zip": f"{base}/download"},
    )


def _get_job_or_404(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/jobs", response_model=JobOut, status_code=202)
def create_job(payload: JobCreate, request: Request, db: Session = Depends(get_db)):
    """Submit a bulk request. Returns 202 immediately; poll GET /jobs/{id} for progress."""
    try:
        job = services.create_job(db, payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if job.status == JobStatus.PENDING:
        submit_job(job.id)
        db.refresh(job)
    return _job_out(db, job, request)


@app.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, request: Request, db: Session = Depends(get_db)):
    return _job_out(db, _get_job_or_404(db, job_id), request)


@app.get("/jobs/{job_id}/certificates", response_model=CertificatePage)
def list_certificates(
    job_id: str,
    status: Literal["pending", "success", "failed"] | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Per-recipient results. Filter with ?status=failed to see what went wrong."""
    _get_job_or_404(db, job_id)
    q = select(Certificate).where(Certificate.job_id == job_id)
    if status:
        q = q.where(Certificate.status == CertStatus(status))
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(q.order_by(Certificate.position).limit(limit).offset(offset)).all()
    items = []
    for c in rows:
        out = CertificateOut.model_validate(c)
        if c.status == CertStatus.SUCCESS:
            out.download_url = f"/certificates/{c.id}/download"
        items.append(out)
    return CertificatePage(total=total, limit=limit, offset=offset, items=items)


@app.get("/certificates/{certificate_id}/download")
def download_certificate(certificate_id: str, db: Session = Depends(get_db)):
    cert = db.get(Certificate, certificate_id)
    if cert is None:
        raise HTTPException(404, "Certificate not found")
    if cert.status != CertStatus.SUCCESS:
        raise HTTPException(409, f"Certificate is not available (status: {cert.status.value})")
    path = Path(cert.file_path)
    if not path.exists():
        raise HTTPException(410, "Certificate file is no longer available")
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@app.get("/jobs/{job_id}/download")
def download_job_zip(job_id: str, background: BackgroundTasks, db: Session = Depends(get_db)):
    """ZIP of all successfully generated certificates (available once the job has any)."""
    job = _get_job_or_404(db, job_id)
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    zip_path = Path(tmp.name)
    background.add_task(zip_path.unlink, missing_ok=True)
    if services.build_zip(db, job, zip_path) == 0:
        zip_path.unlink(missing_ok=True)
        raise HTTPException(404, "No generated certificates available yet")
    return FileResponse(zip_path, media_type="application/zip", filename=f"certificates_{job.id}.zip",
                        background=background)
