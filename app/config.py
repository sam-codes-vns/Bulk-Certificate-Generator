"""Runtime settings, read from environment variables with sane defaults."""
import os
from pathlib import Path


class Settings:
    def __init__(self) -> None:
        self.database_url: str = os.getenv("DATABASE_URL", "sqlite:///./certgen.db")
        self.storage_dir: Path = Path(os.getenv("STORAGE_DIR", "./storage"))
        # Max recipients accepted in one request.
        self.max_recipients: int = int(os.getenv("MAX_RECIPIENTS", "1000"))
        # Number of background worker threads (jobs processed concurrently).
        self.worker_threads: int = int(os.getenv("WORKER_THREADS", "2"))
        # If true, jobs run inside the request (used by tests / debugging).
        self.sync_processing: bool = os.getenv("SYNC_PROCESSING", "false").lower() == "true"


settings = Settings()
