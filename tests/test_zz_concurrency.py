"""Одновременная работа через HTTP: запросы не должны падать с «database is locked»."""
import threading
from datetime import timedelta

from sqlalchemy import select

from app.models import Deal, User
from app.services.worklists import today
from tests.conftest import _client


def test_many_users_write_at_once_without_errors(db):
    logins = ["anna", "boris"]
    pwd = {"anna": "anna-pass-123", "boris": "boris-pass-123"}
    deals = {l: db.scalar(select(Deal.id).join(User, Deal.owner_id == User.id).where(User.login == l, Deal.is_open.is_(True))) for l in logins}
    db.rollback()
    statuses, errors = [], []
    barrier = threading.Barrier(8)

    def work(login, n):
        try:
            client = _client(login, pwd[login])
            barrier.wait()
            for i in range(12):
                r = client.post(f"/deals/{deals[login]}/touch", data={"kind": "note", "body": f"заметка {n}-{i}"})
                statuses.append(r.status_code)
                r = client.post(f"/deals/{deals[login]}/tasks", data={"title": f"t{n}-{i}", "due_date": (today() + timedelta(days=3)).isoformat()})
                statuses.append(r.status_code)
                statuses.append(client.get("/tasks").status_code)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(logins[n % 2], n)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors[:2]
    assert set(statuses) <= {200, 303}, sorted(set(statuses))
