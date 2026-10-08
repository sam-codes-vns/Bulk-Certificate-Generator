import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import CertStatus, JobStatus

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class RecipientIn(BaseModel):
    """Validated per recipient so one bad row doesn't reject the whole request."""

    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=254)
    achievement: str | None = Field(default=None, max_length=200)

    @field_validator("name", "achievement", mode="before")
    @classmethod
    def _clean_text(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            if _CONTROL_CHARS.search(v):
                raise ValueError("must not contain control characters")
            return v or None  # "" -> None, so required `name` then fails min-length/None check
        return v

    @field_validator("email", mode="before")
    @classmethod
    def _clean_email(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            if not _EMAIL_RE.match(v):
                raise ValueError("invalid email address")
        return v


class JobCreate(BaseModel):
    event_name: str = Field(min_length=1, max_length=150)
    issued_by: str = Field(min_length=1, max_length=100)
    issue_date: date = Field(default_factory=date.today)
    # Kept as raw dicts on purpose; each item is validated individually in the service.
    recipients: list[dict[str, Any]] = Field(min_length=1)


class JobProgress(BaseModel):
    total: int
    success: int
    failed: int
    pending: int
    percent_complete: float


class JobOut(BaseModel):
    id: str
    event_name: str
    issued_by: str
    issue_date: date
    status: JobStatus
    progress: JobProgress
    error: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    links: dict[str, str]


class CertificateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    position: int
    recipient_name: str | None
    recipient_email: str | None
    status: CertStatus
    error: str | None
    download_url: str | None = None


class CertificatePage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CertificateOut]
