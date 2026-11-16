"""Тесты работают на отдельной базе SQLite, созданной теми же миграциями, что и боевая."""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="crm-test-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ADMIN_LOGIN"] = "head"
os.environ["ADMIN_PASSWORD"] = "head-pass-123"

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def _database():
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, "head")
    from app import cli
    from app.db import SessionLocal
    from app.services import setup

    cli.cmd_init()
    with SessionLocal() as db:
        setup.create_user(db, "anna", "Анна Смирнова", "anna-pass-123")
        setup.create_user(db, "boris", "Борис Орлов", "boris-pass-123")
        db.commit()
    yield


def _client(login: str, password: str) -> TestClient:
    from app.main import app

    client = TestClient(app, base_url="http://testserver", headers={"Origin": "http://testserver"}, follow_redirects=False)
    r = client.post("/login", data={"login": login, "password": password})
    assert r.status_code == 303, r.text
    return client


@pytest.fixture(scope="session")
def head():
    return _client("head", "head-pass-123")


@pytest.fixture(scope="session")
def anna():
    return _client("anna", "anna-pass-123")


@pytest.fixture(scope="session")
def boris():
    return _client("boris", "boris-pass-123")


@pytest.fixture()
def db():
    from app.db import SessionLocal

    with SessionLocal() as session:
        yield session
