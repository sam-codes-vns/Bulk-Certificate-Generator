import enum
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, enum.Enum):
    PENDING = "pending"                  # accepted, waiting for a worker
    PROCESSING = "processing"            # a worker is generating certificates
    COMPLETED = "completed"              # every certificate succeeded
    COMPLETED_WITH_ERRORS = "completed_with_errors"  # some succeeded, some failed
    FAILED = "failed"                    # nothing could be generated


class CertStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    event_name: Mapped[str] = mapped_column(String(150))
    issued_by: Mapped[str] = mapped_column(String(100))
    issue_date: Mapped[date] = mapped_column(Date)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.PENDING, index=True)
    total: Mapped[int] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)  # job-level crash only
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    certificates: Mapped[list["Certificate"]] = relationship(back_populates="job", order_by="Certificate.position")


class Certificate(Base):
    """One row per submitted recipient (valid or not), so every input is accounted for."""

    __tablename__ = "certificates"
    __table_args__ = (UniqueConstraint("job_id", "position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)  # index in the submitted list (0-based)
    recipient_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recipient_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    achievement: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[CertStatus] = mapped_column(Enum(CertStatus), default=CertStatus.PENDING, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    job: Mapped[Job] = relationship(back_populates="certificates")
