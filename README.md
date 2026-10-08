# Bulk Certificate Generator (Backend)

A FastAPI service that accepts one request containing many recipients, generates a PDF
certificate for each from a single predefined template, tracks progress, and lets clients
download the results individually or as a ZIP.

**Stack:** Python 3.10+, FastAPI, SQLAlchemy 2 (SQLite by default; any relational DB via `DATABASE_URL`), ReportLab (PDF), pytest.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn app.main:app --reload
```

API at http://127.0.0.1:8000 · interactive docs (Swagger) at http://127.0.0.1:8000/docs

Optional environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./certgen.db` | SQLAlchemy URL (e.g. PostgreSQL) |
| `STORAGE_DIR` | `./storage` | Where PDFs are written |
| `MAX_RECIPIENTS` | `1000` | Max recipients per request |
| `WORKER_THREADS` | `2` | Jobs processed concurrently |
| `SYNC_PROCESSING` | `false` | Process inside the request (debugging/tests) |

## Run tests

```bash
pytest -q
```

Tests use a throwaway SQLite DB and temp storage, and run jobs synchronously (one test also exercises the real background thread pool).

## API usage

### 1. Submit a request — `POST /jobs`

```bash
curl -X POST http://127.0.0.1:8000/jobs -H "Content-Type: application/json" -d '{
  "event_name": "Python Bootcamp 2026",
  "issued_by": "Acme Academy",
  "issue_date": "2026-10-01",
  "recipients": [
    {"name": "Asha Verma", "email": "asha@example.com", "achievement": "Top Performer"},
    {"name": "Ravi Kumar", "email": "ravi@example.com"},
    {"name": "", "email": "broken"}
  ]
}'
```

Returns `202 Accepted` right away with the job (`id`, `status: "pending"`, `progress`, `links`).
`issue_date` is optional (defaults to today); `achievement` is optional per recipient.

### 2. Check progress — `GET /jobs/{id}`

```json
{
  "id": "…", "status": "completed_with_errors",
  "progress": {"total": 3, "success": 2, "failed": 1, "pending": 0, "percent_complete": 100.0},
  "links": {"self": "/jobs/…", "certificates": "/jobs/…/certificates", "download_zip": "/jobs/…/download"}
}
```

Job statuses: `pending` → `processing` → `completed` | `completed_with_errors` | `failed`.

### 3. See per-recipient results — `GET /jobs/{id}/certificates`

Paginated (`?limit=100&offset=0`), filterable with `?status=failed|success|pending`.
Each item has `status`, `error` (the reason, if failed), and `download_url` (if successful).

### 4. Retrieve certificates

- One PDF: `GET /certificates/{certificate_id}/download`
- All successful ones as a ZIP: `GET /jobs/{id}/download`

```bash
curl -OJ http://127.0.0.1:8000/jobs/<job_id>/download
```

### Errors

| Code | When |
|---|---|
| 404 | Unknown job/certificate; ZIP requested but nothing generated |
| 409 | Downloading a certificate that failed / isn't ready |
| 410 | Certificate record exists but the file was removed from disk |
| 422 | Request-level validation (empty list, missing event name, bad date, too many recipients, bad filter value) |

## Design decisions

**Background processing (in-process thread pool).** `POST /jobs` stores the job and returns
`202` immediately; a `ThreadPoolExecutor` generates certificates and the client polls.
Rendering hundreds of PDFs inside the HTTP request would risk timeouts and block the client.
A thread pool keeps the project runnable with one command (no Redis/Celery). The trade-off:
jobs live in the web process, so scaling out means moving to a real queue (Celery/RQ/arq) —
`services.process_job(job_id)` is framework-independent and takes only a job id, so it can be
used as a Celery task unchanged. **Durability:** the job and every recipient row are committed
*before* processing starts; on startup, unfinished jobs are re-queued, and `process_job`
only touches `PENDING` rows, so a restart resumes rather than regenerates.

**Validation: per-recipient, not all-or-nothing.** Request-level problems (no recipients, no
event name, bad date, over the limit) return `422`. Bad *recipient* data (empty/too-long name,
invalid email, missing fields) does **not** reject the request: that recipient is stored
immediately as `failed` with a human-readable reason, and the valid ones proceed. Every
submitted recipient therefore has a row (with its original position), so nothing silently disappears.

**Failure isolation.** Each certificate is generated in its own `try/except` and committed
individually. One failure is recorded (`status=failed`, `error=…`) and the loop continues.
PDFs are written to a temp file and atomically renamed, so a failed render never leaves a
corrupt file. Job outcome: all OK → `completed`; mixed → `completed_with_errors`; none OK → `failed`.
An unexpected job-level crash (DB/disk) marks the job `failed` with `error` set.

**Data model.** `jobs` (event details, status, timestamps) and `certificates` (one per
recipient: position, name, email, status, error, file path). Progress counts are computed from
`certificates` with a `GROUP BY`, so they are always consistent with the real rows.

**Template.** One landscape A4 PDF drawn with ReportLab (border, name, event, optional
achievement, date, issuer, certificate ID). Long names/events auto-shrink to fit.

**Known limitations / possible next steps**
- Files are on local disk; for multi-server deployments use object storage (S3).
- No authentication or rate limiting (out of scope here).
- Built-in fonts are used if no Unicode TTF (DejaVu / Arial Unicode) is found; names that font can't display fail that one certificate with a clear error. Scripts like Devanagari need a font that includes them.
- SQLite is fine for development; use PostgreSQL with several workers/processes.

## Project layout

```
app/
  main.py         # routes, worker pool, startup recovery
  services.py     # create_job, process_job (core logic), progress, zip
  certificate.py  # PDF template rendering
  models.py       # Job, Certificate tables
  schemas.py      # request/response models + recipient validation
  database.py, config.py
tests/            # pytest suite (27 tests)
```
