import io
import time
import zipfile

import pytest

from app import services
from app.config import settings


def _make_job(client, payload):
    r = client.post("/jobs", json=payload)
    assert r.status_code == 202, r.text
    return r.json()


# ---------- Creating a generation job ----------

def test_create_job_returns_202_and_job_info(client, payload):
    body = _make_job(client, payload)
    assert body["id"]
    assert body["event_name"] == "Python Bootcamp 2026"
    assert body["progress"]["total"] == 2
    assert body["links"]["certificates"] == f"/jobs/{body['id']}/certificates"


def test_issue_date_defaults_to_today(client, payload):
    del payload["issue_date"]
    body = _make_job(client, payload)
    assert body["issue_date"]


def test_get_unknown_job_404(client):
    assert client.get("/jobs/does-not-exist").status_code == 404


# ---------- Input validation ----------

@pytest.mark.parametrize("mutation", [
    lambda p: p.update(recipients=[]),            # empty list
    lambda p: p.pop("recipients"),
    lambda p: p.update(event_name=""),
    lambda p: p.pop("issued_by"),
    lambda p: p.update(issue_date="not-a-date"),
    lambda p: p.update(recipients=["just a string"]),
])
def test_request_level_validation_rejected_with_422(client, payload, mutation):
    mutation(payload)
    assert client.post("/jobs", json=payload).status_code == 422


def test_too_many_recipients_rejected(client, payload):
    settings.max_recipients = 1
    assert client.post("/jobs", json=payload).status_code == 422


def test_invalid_recipients_are_recorded_as_failed_not_whole_request_rejected(client, payload):
    payload["recipients"] += [
        {"name": "", "email": "x@example.com"},          # empty name
        {"name": "No Email"},                            # missing email
        {"name": "Bad Email", "email": "not-an-email"},  # malformed email
        {"name": "A" * 101, "email": "long@example.com"},  # name too long
    ]
    job = _make_job(client, payload)
    assert job["status"] == "completed_with_errors"
    assert job["progress"] == {"total": 6, "success": 2, "failed": 4, "pending": 0, "percent_complete": 100.0}

    failed = client.get(f"/jobs/{job['id']}/certificates", params={"status": "failed"}).json()
    assert failed["total"] == 4
    errors = {i["position"]: i["error"] for i in failed["items"]}
    assert "name" in errors[2] and "email" in errors[3] and "invalid email" in errors[4] and "name" in errors[5]


def test_all_invalid_job_is_failed_immediately(client, payload):
    payload["recipients"] = [{"name": "", "email": "bad"}]
    job = _make_job(client, payload)
    assert job["status"] == "failed"
    assert job["completed_at"] is not None


# ---------- Certificate generation ----------

def test_certificates_generated_as_valid_pdfs(client, payload):
    job = _make_job(client, payload)
    items = client.get(f"/jobs/{job['id']}/certificates").json()["items"]
    assert [i["status"] for i in items] == ["success", "success"]
    r = client.get(items[0]["download_url"])
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")


def test_whitespace_is_trimmed(client, payload):
    payload["recipients"] = [{"name": "  Priya Sharma  ", "email": " priya@example.com "}]
    job = _make_job(client, payload)
    item = client.get(f"/jobs/{job['id']}/certificates").json()["items"][0]
    assert item["recipient_name"] == "Priya Sharma"
    assert item["recipient_email"] == "priya@example.com"
    assert item["status"] == "success"


# ---------- Job status / progress ----------

def test_job_status_completed(client, payload):
    job = _make_job(client, payload)
    body = client.get(f"/jobs/{job['id']}").json()
    assert body["status"] == "completed"
    assert body["progress"]["success"] == 2 and body["progress"]["percent_complete"] == 100.0
    assert body["completed_at"] is not None


def test_progress_while_pending(client, payload):
    """With background processing the job is queued first; progress must reflect that."""
    settings.sync_processing = False

    class _Capture:  # stand-in executor that never runs the job
        def submit(self, *a, **k):
            pass

    import app.main as main
    main._executor = _Capture()
    job = _make_job(client, payload)
    assert job["status"] == "pending"
    assert job["progress"] == {"total": 2, "success": 0, "failed": 0, "pending": 2, "percent_complete": 0.0}


def test_background_processing_end_to_end(client, payload):
    """Real thread pool: POST returns right away; polling eventually shows completion."""
    from concurrent.futures import ThreadPoolExecutor
    import app.main as main
    settings.sync_processing = False
    main._executor = ThreadPoolExecutor(max_workers=1)
    try:
        job = _make_job(client, payload)
        for _ in range(100):
            body = client.get(f"/jobs/{job['id']}").json()
            if body["status"] == "completed":
                break
            time.sleep(0.05)
        assert body["status"] == "completed"
        assert body["progress"]["success"] == 2
    finally:
        main._executor.shutdown(wait=True)


def test_process_job_is_idempotent(client, payload):
    job = _make_job(client, payload)
    services.process_job(job["id"])  # second call must be a no-op
    assert client.get(f"/jobs/{job['id']}").json()["progress"]["success"] == 2


# ---------- Individual certificate failure ----------

def test_one_failure_does_not_stop_other_certificates(client, payload, monkeypatch):
    real = services.render_certificate

    def flaky(data, path):
        if data.recipient_name == "Ravi Kumar":
            raise RuntimeError("disk exploded")
        return real(data, path)

    monkeypatch.setattr(services, "render_certificate", flaky)
    payload["recipients"].append({"name": "Meera Iyer", "email": "meera@example.com"})
    job = _make_job(client, payload)

    assert job["status"] == "completed_with_errors"
    assert job["progress"]["success"] == 2 and job["progress"]["failed"] == 1
    items = client.get(f"/jobs/{job['id']}/certificates").json()["items"]
    by_name = {i["recipient_name"]: i for i in items}
    assert by_name["Ravi Kumar"]["status"] == "failed"
    assert "disk exploded" in by_name["Ravi Kumar"]["error"]
    assert by_name["Ravi Kumar"]["download_url"] is None
    assert by_name["Meera Iyer"]["status"] == "success"  # processed AFTER the failure


def test_all_certificates_failing_marks_job_failed(client, payload, monkeypatch):
    def boom(data, path):
        raise RuntimeError("nope")

    monkeypatch.setattr(services, "render_certificate", boom)
    job = _make_job(client, payload)
    assert job["status"] == "failed"
    assert job["progress"]["failed"] == 2


def test_no_partial_file_left_after_render_failure(client, payload, monkeypatch):
    import app.certificate as cert_mod

    def explode(*a, **k):
        raise RuntimeError("mid-render failure")

    monkeypatch.setattr(cert_mod.canvas.Canvas, "showPage", explode)
    job = _make_job(client, payload)
    assert job["status"] == "failed"
    assert not list(settings.storage_dir.rglob("*.pdf*"))


# ---------- Retrieving generated certificates ----------

def test_list_certificates_pagination_and_filter(client, payload):
    payload["recipients"] = [{"name": f"Person {i}", "email": f"p{i}@example.com"} for i in range(5)]
    job = _make_job(client, payload)
    page = client.get(f"/jobs/{job['id']}/certificates", params={"limit": 2, "offset": 2}).json()
    assert page["total"] == 5 and [i["position"] for i in page["items"]] == [2, 3]
    assert client.get(f"/jobs/{job['id']}/certificates", params={"status": "failed"}).json()["total"] == 0
    assert client.get(f"/jobs/{job['id']}/certificates", params={"status": "bogus"}).status_code == 422


def test_download_zip_contains_only_successful(client, payload):
    payload["recipients"].append({"name": "", "email": "bad"})
    job = _make_job(client, payload)
    r = client.get(f"/jobs/{job['id']}/download")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert len(names) == 2 and all(n.endswith(".pdf") for n in names)


def test_download_zip_404_when_nothing_generated(client, payload):
    payload["recipients"] = [{"name": "", "email": "bad"}]
    job = _make_job(client, payload)
    assert client.get(f"/jobs/{job['id']}/download").status_code == 404


def test_download_failed_certificate_returns_409(client, payload):
    payload["recipients"].append({"name": "", "email": "bad"})
    job = _make_job(client, payload)
    failed = client.get(f"/jobs/{job['id']}/certificates", params={"status": "failed"}).json()["items"][0]
    assert client.get(f"/certificates/{failed['id']}/download").status_code == 409


def test_download_unknown_certificate_404(client):
    assert client.get("/certificates/nope/download").status_code == 404


def test_download_when_file_deleted_returns_410(client, payload):
    job = _make_job(client, payload)
    item = client.get(f"/jobs/{job['id']}/certificates").json()["items"][0]
    for f in settings.storage_dir.rglob("*.pdf"):
        f.unlink()
    assert client.get(item["download_url"]).status_code == 410
