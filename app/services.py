"""Job creation and processing logic (framework-independent)."""
import logging
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .certificate import CertificateData, render_certificate
from .config import settings
from .database import SessionLocal
from .models import Certificate, CertStatus, Job, JobStatus
from .schemas import JobCreate, JobProgress, RecipientIn

log = logging.getLogger("certgen")


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"]) or "recipient"
        msg = err["msg"]
        if err["type"] == "missing":
            msg = "field required"
        parts.append(f"{field}: {msg}")
    return "; ".join(parts)


def create_job(db: Session, payload: JobCreate) -> Job:
    """Persist the job and ONE row per submitted recipient.

    Invalid recipients are stored immediately as FAILED with the reason, so the
    client can see exactly which inputs were rejected. Valid ones are PENDING.
    """
    if len(payload.recipients) > settings.max_recipients:
        raise ValueError(f"too many recipients (max {settings.max_recipients})")

    job = Job(
        event_name=payload.event_name.strip(),
        issued_by=payload.issued_by.strip(),
        issue_date=payload.issue_date,
        total=len(payload.recipients),
    )
    db.add(job)
    db.flush()

    valid_count = 0
    for position, raw in enumerate(payload.recipients):
        try:
            r = RecipientIn.model_validate(raw)
            cert = Certificate(job_id=job.id, position=position, recipient_name=r.name,
                               recipient_email=r.email, achievement=r.achievement)
            valid_count += 1
        except ValidationError as exc:
            # Keep whatever raw values are usable for display (truncated to column size).
            name, email = raw.get("name"), raw.get("email")
            cert = Certificate(
                job_id=job.id, position=position,
                recipient_name=name[:255] if isinstance(name, str) else None,
                recipient_email=email[:255] if isinstance(email, str) else None,
                status=CertStatus.FAILED, error=f"validation failed - {_format_validation_error(exc)}",
            )
        db.add(cert)

    # If nothing is valid there is nothing to process: finish immediately.
    if valid_count == 0:
        job.status = JobStatus.FAILED
        job.completed_at = datetime.now(timezone.utc)
    db.commit()
    return job


def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")[:40] or "certificate"


def _final_status(success: int, failed: int) -> JobStatus:
    if success == 0:
        return JobStatus.FAILED
    return JobStatus.COMPLETED if failed == 0 else JobStatus.COMPLETED_WITH_ERRORS


def process_job(job_id: str) -> None:
    """Generate all PENDING certificates for a job. Safe to call again (resumable):
    only PENDING rows are touched. Each certificate is isolated in its own try/except
    and committed individually, so progress is visible and one failure never stops the rest."""
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None or job.status in (JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_ERRORS, JobStatus.FAILED):
            return
        try:
            job.status = JobStatus.PROCESSING
            db.commit()

            pending_ids = db.scalars(
                select(Certificate.id)
                .where(Certificate.job_id == job_id, Certificate.status == CertStatus.PENDING)
                .order_by(Certificate.position)
            ).all()

            for cert_id in pending_ids:
                cert = db.get(Certificate, cert_id)
                try:
                    path = _output_path(job, cert)
                    render_certificate(
                        CertificateData(
                            recipient_name=cert.recipient_name, event_name=job.event_name,
                            issued_by=job.issued_by, issue_date=job.issue_date,
                            achievement=cert.achievement, certificate_id=cert.id,
                        ),
                        path,
                    )
                    cert.file_path = str(path)
                    cert.status = CertStatus.SUCCESS
                except Exception as exc:  # isolate: record and continue with the next recipient
                    log.warning("certificate %s failed: %s", cert_id, exc)
                    db.rollback()
                    cert = db.get(Certificate, cert_id)
                    cert.status = CertStatus.FAILED
                    cert.error = f"generation failed - {exc}"[:1000]
                db.commit()

            counts = status_counts(db, job_id)
            job.status = _final_status(counts[CertStatus.SUCCESS], counts[CertStatus.FAILED])
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
        except Exception as exc:  # job-level failure (e.g. DB / disk problem)
            log.exception("job %s crashed", job_id)
            db.rollback()
            job = db.get(Job, job_id)
            job.status = JobStatus.FAILED
            job.error = str(exc)[:1000]
            job.completed_at = datetime.now(timezone.utc)
            db.commit()


def _output_path(job: Job, cert: Certificate) -> Path:
    return settings.storage_dir / job.id / f"{cert.position + 1:05d}_{_safe_filename(cert.recipient_name)}.pdf"


def status_counts(db: Session, job_id: str) -> dict[CertStatus, int]:
    rows = db.execute(
        select(Certificate.status, func.count()).where(Certificate.job_id == job_id).group_by(Certificate.status)
    ).all()
    counts = {s: 0 for s in CertStatus}
    counts.update({status: n for status, n in rows})
    return counts


def get_progress(db: Session, job: Job) -> JobProgress:
    c = status_counts(db, job.id)
    done = c[CertStatus.SUCCESS] + c[CertStatus.FAILED]
    return JobProgress(
        total=job.total, success=c[CertStatus.SUCCESS], failed=c[CertStatus.FAILED], pending=c[CertStatus.PENDING],
        percent_complete=round(100 * done / job.total, 1) if job.total else 100.0,
    )


def build_zip(db: Session, job: Job, zip_path: Path) -> int:
    """Zip all successful certificates; returns how many were included."""
    certs = db.scalars(
        select(Certificate).where(Certificate.job_id == job.id, Certificate.status == CertStatus.SUCCESS)
        .order_by(Certificate.position)
    ).all()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:  # PDFs are already compressed
        for cert in certs:
            zf.write(cert.file_path, arcname=Path(cert.file_path).name)
    return len(certs)
