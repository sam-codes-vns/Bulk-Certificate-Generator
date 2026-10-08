import os
import tempfile

# Must be set before the app is imported.
_tmp = tempfile.mkdtemp(prefix="certgen_tests_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["STORAGE_DIR"] = f"{_tmp}/storage"
os.environ["SYNC_PROCESSING"] = "true"

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import Base, engine
from app.main import app


@pytest.fixture(autouse=True)
def clean_state(tmp_path):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    settings.storage_dir = tmp_path / "storage"
    settings.sync_processing = True
    settings.max_recipients = 1000
    yield


@pytest.fixture
def client():
    return TestClient(app)  # no `with`: lifespan (thread pool) not needed in sync mode


@pytest.fixture
def payload():
    return {
        "event_name": "Python Bootcamp 2026",
        "issued_by": "Acme Academy",
        "issue_date": "2026-10-01",
        "recipients": [
            {"name": "Asha Verma", "email": "asha@example.com", "achievement": "Top Performer"},
            {"name": "Ravi Kumar", "email": "ravi@example.com"},
        ],
    }
