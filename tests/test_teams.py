"""Группы, загрузка списков менеджерами, оплаты, рейтинг и права владельца.

Идёт после test_scenario и опирается на его базу: компании «Ромашка» и др. уже закреплены за anna и boris.
"""
import re
from datetime import date

import pytest
from sqlalchemy import func, select

from app.models import Activity, AuditLog, Company, Deal, ImportBatch, ImportRow, Payment, Stage, Team, User
from app.services import importer, payments, setup
from app.services.worklists import today
from tests.conftest import _client

T = {}
PWD = "team-pass-123"


def _uid(db, login) -> int:
    db.rollback()
    return db.scalar(select(User.id).where(User.login == login))


def _stage(db, name) -> Stage:
    db.rollback()
    return db.scalar(select(Stage).where(Stage.name == name, Stage.pipeline_id == 1))


def _batch_id(response) -> int:
    assert response.status_code == 303, response.text
    return int(re.search(r"/import/(\d+)", response.headers["location"]).group(1))


def _confirm(client, db, bid, **extra):
    db.rollback()
    rows = list(db.scalars(select(ImportRow).where(ImportRow.batch_id == bid)))
    data = {"row_id": [str(r.id) for r in rows], **extra}
    for r in rows:
        for f in importer.EDITABLE:
            data[f"{f}_{r.id}"] = getattr(r, f)
    return client.post(f"/import/{bid}/confirm", data=data)


def _deal(db, site) -> Deal:
    db.rollback()
    return db.scalar(select(Deal).join(Company).where(Company.website_norm == site))


@pytest.fixture(scope="module")
def people(head, _database):
    """Владелец через интерфейс создаёт две группы, двух руководителей и трёх менеджеров."""
    from app.db import SessionLocal

    assert head.post("/settings/teams", data={"name": "Группа Север"}).status_code == 303
    assert head.post("/settings/teams", data={"name": "Группа Юг"}).status_code == 303
    with SessionLocal() as s:
        north = s.scalar(select(Team.id).where(Team.name == "Группа Север"))
        south = s.scalar(select(Team.id).where(Team.name == "Группа Юг"))
    for login, name, role, team in (
        ("lena", "Лена Северова", "lead", north), ("max", "Макс Петров", "manager", north),
        ("nina", "Нина Котова", "manager", north), ("oleg", "Олег Южный", "lead", south),
        ("pavel", "Павел Зимин", "manager", south),
    ):
        r = head.post("/settings/users", data={"login": login, "name": name, "password": PWD, "role": role, "team_id": str(team)})
        assert r.status_code == 303
    T["north"], T["south"] = north, south
    return {login: _client(login, PWD) for login in ("lena", "max", "nina", "oleg", "pavel")}


def test_owner_role_and_team_setup(head, people, db):
    assert db.scalar(select(User.role).where(User.login == "head")) == "owner"
    roles = dict(db.execute(select(User.login, User.role).where(User.login.in_(["lena", "max", "oleg"]))).all())
    assert roles == {"lena": "lead", "max": "manager", "oleg": "lead"}
    assert db.scalar(select(User.team_id).where(User.login == "nina")) == T["north"]
    page = head.get("/settings/users").text
    assert "Группа Север" in page and "Лена Северова" in page and "Руководитель группы" in page


def test_manager_imports_own_list_and_duplicates_resolve_themselves(people, db):
    max_ = people["max"]
    text = ("Северный Ветер, Мурманск, https://sever-veter.ru, +7 8152 11-22-33\n"
            "Полярная Звезда, Мурманск, polar-star.ru\n"
            "ООО Ромашка, Казань, https://romashka-flowers.ru\n")  # уже в базе, в работе у другого менеджера
    bid = _batch_id(max_.post("/import", data={"mode": "text", "text": text}))
    preview = max_.get(f"/import/{bid}")
    assert preview.status_code == 200
    romashka = db.scalar(select(Company).where(Company.website_norm == "romashka-flowers.ru"))
    owner_name = db.scalar(select(User.name).join(Deal, Deal.owner_id == User.id).where(Deal.company_id == romashka.id))
    assert f"В работе у: {owner_name}" in preview.text and "компания уже есть в базе" in preview.text
    assert f"/companies/{romashka.id}" not in preview.text  # ссылки на чужую карточку нет
    assert "Сделки по новым компаниям будут назначены вам" in preview.text
    # менеджер пробует назначить сделки другому и не создавать сделку — правила сильнее формы
    assert _confirm(max_, db, bid, pipeline_id="", owner_id=str(_uid(db, "pavel"))).status_code == 303
    db.rollback()
    batch = db.get(ImportBatch, bid)
    assert (batch.created_count, batch.skipped_count) == (2, 1)
    for site in ("sever-veter.ru", "polar-star.ru"):
        assert _deal(db, site).owner_id == _uid(db, "max")
    assert db.scalar(select(func.count()).select_from(Company).where(Company.website_norm == "romashka-flowers.ru")) == 1
    result = max_.get(f"/import/{bid}").text
    assert "уже в базе" in result and f"/companies/{romashka.id}" not in result
    assert "Северный Ветер" in max_.get("/companies").text
    assert max_.get(f"/companies/{romashka.id}").status_code == 404


def test_first_uploader_keeps_the_company(people, db):
    pavel = people["pavel"]
    bid = _batch_id(pavel.post("/import", data={"mode": "text", "text": "Северный Ветер, Мурманск, sever-veter.ru\nЮжный Берег, Сочи, yug-bereg.ru\n"}))
    assert "В работе у: Макс Петров" in pavel.get(f"/import/{bid}").text
    assert _confirm(pavel, db, bid).status_code == 303
    assert _deal(db, "sever-veter.ru").owner_id == _uid(db, "max")
    assert _deal(db, "yug-bereg.ru").owner_id == _uid(db, "pavel")
    # добавление вручную тоже не обходит проверку
    r = pavel.post("/companies/new", data={"name": "Ветер Севера", "website": "https://www.sever-veter.ru/about"})
    assert r.status_code == 400 and "Она в работе у: Макс Петров" in r.text and "Открыть её карточку" not in r.text
    r = pavel.post("/companies/new", data={"name": "Кавказ Тур", "website": "kavkaz-tur.ru", "city": "Сочи"})
    assert r.status_code == 303 and _deal(db, "kavkaz-tur.ru").owner_id == _uid(db, "pavel")


def test_import_batches_are_private(people, head, db):
    max_batch = db.scalar(select(ImportBatch.id).where(ImportBatch.user_id == _uid(db, "max")))
    assert people["nina"].get(f"/import/{max_batch}").status_code == 404
    assert people["lena"].get(f"/import/{max_batch}").status_code == 404
    assert people["nina"].post(f"/import/{max_batch}/cancel").status_code == 404
    assert head.get(f"/import/{max_batch}").status_code == 200
    assert f"/import/{max_batch}" not in people["nina"].get("/import").text


def test_lead_sees_own_team_only(people, db):
    deal = _deal(db, "sever-veter.ru")
    lena, oleg, nina = people["lena"], people["oleg"], people["nina"]
    assert lena.get(f"/deals/{deal.id}").status_code == 200
    assert lena.get(f"/companies/{deal.company_id}").status_code == 200
    assert "Северный Ветер" in lena.get("/pipeline").text and "Северный Ветер" in lena.get("/companies").text
    for other in (oleg, nina):
        assert other.get(f"/deals/{deal.id}").status_code == 404
        assert other.get(f"/companies/{deal.company_id}").status_code == 404
        assert "Северный Ветер" not in other.get("/companies").text
    assert "Южный Берег" in oleg.get("/companies").text and "Южный Берег" not in lena.get("/companies").text
    assert "Ромашка" not in lena.get("/companies").text  # сделки менеджеров без группы видит только владелец
    assert lena.get("/reports").status_code == 200
    report = lena.get("/reports").text
    assert "Макс Петров" in report and "Павел Зимин" not in report and "Анна Смирнова" not in report
    assert lena.get(f"/companies?owner={_uid(db, 'pavel')}").text.count("Найдено: 0") == 1


def test_lead_distributes_inside_team(people, db):
    lena = people["lena"]
    bid = _batch_id(lena.post("/import", data={"mode": "text", "text": "Тундра Логистик, Мурманск, tundra-log.ru\nАрктик Строй, Мурманск, arctic-stroy.ru\n"}))
    assert "Нина Котова" in lena.get(f"/import/{bid}").text and "Павел Зимин" not in lena.get(f"/import/{bid}").text
    assert _confirm(lena, db, bid, owner_id=str(_uid(db, "nina"))).status_code == 303
    assert _deal(db, "tundra-log.ru").owner_id == _uid(db, "nina")
    # чужого сотрудника выбрать нельзя — сделки остаются на руководителе
    bid = _batch_id(lena.post("/import", data={"mode": "text", "text": "Ледокол Сервис, Мурманск, ledokol-service.ru\n"}))
    assert _confirm(lena, db, bid, owner_id=str(_uid(db, "pavel"))).status_code == 303
    assert _deal(db, "ledokol-service.ru").owner_id == _uid(db, "lena")

    deal = _deal(db, "polar-star.ru")
    r = lena.post(f"/deals/{deal.id}/owner", data={"version": str(deal.version), "owner_id": str(_uid(db, "pavel"))})
    assert r.status_code == 400 and "своей группы" in r.text
    r = lena.post(f"/deals/{deal.id}/owner", data={"version": str(deal.version), "owner_id": str(_uid(db, "nina"))})
    assert r.status_code == 303 and _deal(db, "polar-star.ru").owner_id == _uid(db, "nina")
    # массовое назначение: чужие сделки руководитель не трогает
    foreign = _deal(db, "yug-bereg.ru")
    own = _deal(db, "ledokol-service.ru")
    r = lena.post("/companies/assign", data={"company_id": [str(foreign.company_id), str(own.company_id)],
                                             "manager_id": [str(_uid(db, "max")), str(_uid(db, "pavel"))], "scope": "selected"})
    assert r.status_code == 303
    assert _deal(db, "yug-bereg.ru").owner_id == _uid(db, "pavel")
    assert _deal(db, "ledokol-service.ru").owner_id == _uid(db, "max")
    oleg_try = people["oleg"].post(f"/deals/{own.id}/owner", data={"version": "1", "owner_id": str(_uid(db, "pavel"))})
    assert oleg_try.status_code == 404


def test_payment_needs_amount_and_confirmation(people, head, db):
    max_, lena, oleg, nina = people["max"], people["lena"], people["oleg"], people["nina"]
    deal = _deal(db, "sever-veter.ru")
    paid = _stage(db, "Оплачено")
    r = max_.post(f"/deals/{deal.id}/stage", data={"version": str(deal.version), "stage_id": str(paid.id)})
    assert r.status_code == 400 and "сумму и дату оплаты" in r.text
    r = max_.post(f"/deals/{deal.id}/stage", data={"version": str(deal.version), "stage_id": str(paid.id), "amount": "abc", "paid_on": today().isoformat()})
    assert r.status_code == 400 and "Сумма должна быть числом" in r.text
    r = max_.post(f"/deals/{deal.id}/stage", data={"version": str(deal.version), "stage_id": str(paid.id),
                                                    "amount": "45 000,50", "paid_on": today().isoformat()})
    assert r.status_code == 303
    db.rollback()
    payment = db.scalar(select(Payment).where(Payment.deal_id == deal.id))
    assert (payment.amount_minor, payment.status, payment.credited_to_id) == (4500050, "pending", _uid(db, "max"))
    assert _deal(db, "sever-veter.ru").stage.name == "Оплачено"
    T["pay_max"] = payment.id
    month = today().strftime("%Y-%m")
    assert "45 000,50" not in max_.get(f"/rating?month={month}").text  # до подтверждения в рейтинге нет
    assert "45 000,50" in max_.get("/payments").text and "Подтвердить" not in max_.get("/payments").text
    # сам себе, чужой руководитель и коллега подтвердить не могут
    assert max_.post(f"/payments/{payment.id}/status", data={"decision": "confirmed"}).status_code == 403
    assert oleg.post(f"/payments/{payment.id}/status", data={"decision": "confirmed"}).status_code == 404
    assert nina.post(f"/payments/{payment.id}/status", data={"decision": "confirmed"}).status_code == 404
    db.rollback()
    assert db.get(Payment, payment.id).status == "pending"
    assert "Подтвердить" in lena.get("/payments").text
    assert lena.post(f"/payments/{payment.id}/status", data={"decision": "confirmed", "back": "/payments"}).status_code == 303
    db.rollback()
    payment = db.get(Payment, payment.id)
    assert payment.status == "confirmed" and payment.confirmed_by_id == _uid(db, "lena")
    page = max_.get(f"/rating?month={month}").text
    assert "45 000,50" in page and "Ваше место" in page and "это вы" in page
    # решение руководителя окончательное для него самого: передумать может только владелец
    lena.post(f"/payments/{payment.id}/status", data={"decision": "rejected"})
    db.rollback()
    assert db.get(Payment, payment.id).status == "confirmed"


def test_lead_cannot_confirm_own_payment(people, head, db):
    lena = people["lena"]
    bid = _batch_id(lena.post("/import", data={"mode": "text", "text": "Сияние Маркет, Мурманск, siyanie-market.ru\n"}))
    assert _confirm(lena, db, bid).status_code == 303
    deal = _deal(db, "siyanie-market.ru")
    assert lena.post(f"/deals/{deal.id}/payments", data={"amount": "30000", "paid_on": today().isoformat(), "note": "аванс"}).status_code == 303
    db.rollback()
    payment = db.scalar(select(Payment).where(Payment.deal_id == deal.id))
    assert payment.credited_to_id == _uid(db, "lena")
    assert lena.post(f"/payments/{payment.id}/status", data={"decision": "confirmed"}).status_code == 403
    assert "свою оплату подтверждает владелец" in lena.get("/payments").text
    assert head.post(f"/payments/{payment.id}/status", data={"decision": "confirmed"}).status_code == 303
    db.rollback()
    assert db.get(Payment, payment.id).status == "confirmed"
    T["pay_lena"] = payment.id


def test_owner_corrects_and_deletes_payments(people, head, db):
    lena, max_ = people["lena"], people["max"]
    pid = T["pay_max"]
    data = {"amount": "40000", "paid_on": today().isoformat(), "credited_to_id": str(_uid(db, "max")), "status": "confirmed", "note": "сумма по выписке"}
    assert lena.post(f"/payments/{pid}/edit", data=data).status_code == 403
    assert max_.post(f"/payments/{pid}/edit", data=data).status_code == 403
    assert lena.post(f"/payments/{pid}/delete").status_code == 403
    assert head.post(f"/payments/{pid}/edit", data=data).status_code == 303
    db.rollback()
    payment = db.get(Payment, pid)
    assert (payment.amount_minor, payment.note) == (4000000, "сумма по выписке")
    month = today().strftime("%Y-%m")
    rating = head.get(f"/rating?month={month}").text
    assert "40 000" in rating and "45 000,50" not in rating
    log = db.scalar(select(AuditLog).where(AuditLog.entity == "оплата", AuditLog.entity_id == pid))
    assert "Было: 45 000,50" in log.summary and "Стало: 40 000" in log.summary
    assert "Оплата исправлена. Было: 45 000,50" in max_.get(f"/deals/{payment.deal_id}").text
    assert "сумма по выписке" in head.get("/settings/audit").text or "Было: 45 000,50" in head.get("/settings/audit").text
    # владелец может передать оплату другому менеджеру и снять подтверждение
    data.update(credited_to_id=str(_uid(db, "nina")), status="pending")
    assert head.post(f"/payments/{pid}/edit", data=data).status_code == 303
    db.rollback()
    payment = db.get(Payment, pid)
    assert (payment.credited_to_id, payment.status, payment.confirmed_by_id) == (_uid(db, "nina"), "pending", None)
    data.update(credited_to_id=str(_uid(db, "max")), status="confirmed")
    assert head.post(f"/payments/{pid}/edit", data=data).status_code == 303
    # удаление
    lena_pid = T["pay_lena"]
    deal_id = db.get(Payment, lena_pid).deal_id
    assert head.post(f"/payments/{lena_pid}/delete").status_code == 303
    db.rollback()
    assert db.get(Payment, lena_pid) is None
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "удаление", AuditLog.entity_id == lena_pid)) == 1
    assert "Оплата удалена: 30 000" in head.get(f"/deals/{deal_id}").text


def test_owner_can_fix_any_card_and_delete(head, people, db):
    deal = _deal(db, "kavkaz-tur.ru")
    deal_id, company_id = deal.id, deal.company_id
    r = head.post(f"/companies/{company_id}/edit", data={"name": "Кавказ Тур (исправлено)", "website": "kavkaz-tur.ru", "city": "Адлер"})
    assert r.status_code == 303
    assert "Кавказ Тур (исправлено)" in people["pavel"].get(f"/companies/{company_id}").text
    assert people["pavel"].post(f"/companies/{company_id}/delete").status_code == 403
    assert people["oleg"].post(f"/deals/{deal_id}/delete").status_code == 403
    assert head.post(f"/deals/{deal_id}/delete").status_code == 303
    db.rollback()
    assert db.get(Deal, deal_id) is None and db.get(Company, company_id) is not None
    assert db.scalar(select(func.count()).select_from(Activity).where(Activity.deal_id == deal_id)) == 0
    assert head.post(f"/companies/{company_id}/delete").status_code == 303
    db.rollback()
    assert db.get(Company, company_id) is None
    audit = head.get("/settings/audit").text
    assert "Удалена компания «Кавказ Тур (исправлено)»" in audit and "Удалена сделка" in audit
    # освободившийся сайт можно загрузить заново
    bid = _batch_id(people["pavel"].post("/import", data={"mode": "text", "text": "Кавказ Тур, Сочи, kavkaz-tur.ru\n"}))
    db.rollback()
    assert db.scalar(select(ImportRow.status).where(ImportRow.batch_id == bid)) == "new"
    people["pavel"].post(f"/import/{bid}/cancel")


def test_lead_manages_only_own_managers(people, head, db):
    lena = people["lena"]
    page = lena.get("/settings/users").text
    assert "Макс Петров" in page and "Павел Зимин" not in page and "Олег Южный" not in page
    # руководитель пытается создать руководителя в чужой группе — получается менеджер своей
    r = lena.post("/settings/users", data={"login": "rita", "name": "Рита Белова", "password": PWD, "role": "owner", "team_id": str(T["south"])})
    assert r.status_code == 303
    db.rollback()
    rita = db.scalar(select(User).where(User.login == "rita"))
    assert (rita.role, rita.team_id) == ("manager", T["north"])
    pavel_id, oleg_id, max_id = _uid(db, "pavel"), _uid(db, "oleg"), _uid(db, "max")
    for target in (pavel_id, oleg_id, _uid(db, "head"), _uid(db, "lena")):
        assert lena.post(f"/settings/users/{target}", data={"action": "password", "password": "hacked-pass-1"}).status_code == 404
    assert lena.post(f"/settings/users/{max_id}", data={"action": "assign", "role": "lead", "team_id": str(T["north"])}).status_code == 303
    db.rollback()
    assert db.get(User, max_id).role == "manager"  # роль меняет только владелец
    assert lena.post(f"/settings/users/{rita.id}", data={"action": "toggle"}).status_code == 303
    db.rollback()
    assert db.get(User, rita.id).is_active is False
    for url in ("/settings/pipelines", "/settings/audit", "/companies/export.csv"):
        assert lena.get(url).status_code == 403
    assert lena.post("/settings/teams", data={"name": "Своя"}).status_code == 403
    assert people["max"].get("/settings/users").status_code == 403
    # владелец переводит менеджера в другую группу — его сделки уходят из видимости прежнего руководителя
    deal = _deal(db, "ledokol-service.ru")
    assert head.post(f"/settings/users/{max_id}", data={"action": "assign", "role": "manager", "team_id": str(T["south"])}).status_code == 303
    assert lena.get(f"/deals/{deal.id}").status_code == 404 and people["oleg"].get(f"/deals/{deal.id}").status_code == 200
    assert head.post(f"/settings/users/{max_id}", data={"action": "assign", "role": "manager", "team_id": str(T["north"])}).status_code == 303
    assert lena.get(f"/deals/{deal.id}").status_code == 200
    # владелец не может снять роль с себя
    head_id = _uid(db, "head")
    head.post(f"/settings/users/{head_id}", data={"action": "assign", "role": "manager", "team_id": ""})
    db.rollback()
    assert db.get(User, head_id).role == "owner"


def test_rating_publishes_top_ten_and_own_place(people, head, db):
    from app.db import SessionLocal

    month = date(2031, 3, 1)  # отдельный месяц, чтобы не мешали другие тесты
    with SessionLocal() as s:
        owner = s.scalar(select(User).where(User.login == "head"))
        deal = s.scalar(select(Deal).join(Company).where(Company.website_norm == "tundra-log.ru"))
        for i in range(12):
            u = setup.create_user(s, f"seller{i:02d}", f"Продавец {i:02d}", PWD, "manager", T["south"])
            s.flush()
            p = Payment(deal_id=deal.id, amount_minor=(100 + i) * 100000, paid_on=month.replace(day=5), credited_to_id=u.id,
                        status="confirmed", created_by_id=owner.id)
            s.add(p)
        s.add(Payment(deal_id=deal.id, amount_minor=999_000_00, paid_on=month.replace(day=6), status="pending",
                      credited_to_id=s.scalar(select(User.id).where(User.login == "pavel")), created_by_id=owner.id))
        s.add(Payment(deal_id=deal.id, amount_minor=5_000_00, paid_on=month.replace(day=7), status="confirmed",
                      credited_to_id=s.scalar(select(User.id).where(User.login == "pavel")), created_by_id=owner.id))
        s.commit()
        result = payments.rating(s, s.scalar(select(User).where(User.login == "pavel")), 2031, 3)
        assert [x["user"].name for x in result["top"]] == [f"Продавец {i:02d}" for i in range(11, 1, -1)]
        assert result["full"] is None and result["mine"]["place"] == 13 and result["mine"]["total"] == 5_000_00
        full = payments.rating(s, owner, 2031, 3)["full"]
        assert len(full) >= 13 and full[0]["total"] == 111 * 100000
    page = people["pavel"].get("/rating?month=2031-03").text
    assert page.count('<span class="place">') == 10
    assert "Продавец 11" in page and "Продавец 01" not in page and "Продавец 00" not in page
    assert "Ваше место" in page and re.search(r"<b>13 <span", page) and "999 000" not in page
    owner_page = head.get("/rating?month=2031-03").text
    assert "Все менеджеры" in owner_page and "Продавец 00" in owner_page and "Ваше место" not in owner_page
    assert "Все менеджеры" not in people["oleg"].get("/rating?month=2031-03").text
    # оплата из другого месяца в этот рейтинг не попадает
    assert "Макс Петров" in people["pavel"].get(f"/rating?month={today().strftime('%Y-%m')}").text


def test_help_page_shows_owner_instructions_only_to_owner(people, head):
    assert "Как исправить оплату (владелец)" in head.get("/help").text
    manager_help = people["nina"].get("/help").text
    assert "Как отметить оплату" in manager_help and "Как исправить оплату (владелец)" not in manager_help


def test_migration_keeps_data_and_turns_head_into_owner(tmp_path):
    import os
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    target = tmp_path / "old.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{target}"}
    alembic = [sys.executable, "-m", "alembic"]
    assert subprocess.run(alembic + ["upgrade", "0001"], env=env, cwd=root, capture_output=True).returncode == 0
    con = sqlite3.connect(target)
    con.executescript("""
        insert into users(id,login,name,password_hash,role,is_active,created_at) values
          (1,'head','Рук','x','head',1,'2026-01-01'),(2,'anna','Анна','x','manager',1,'2026-01-01');
        insert into pipelines(id,name,is_default,is_archived,created_at) values (1,'P',1,0,'2026-01-01');
        insert into stages(id,pipeline_id,name,position,kind) values (1,1,'Новая',0,'open');
        insert into companies(id,name,name_norm,city_norm,status,created_at,updated_at,created_by_id) values (1,'К','к','','ok','2026-01-01','2026-01-01',1);
        insert into deals(id,company_id,pipeline_id,stage_id,owner_id,is_open,version,created_at,updated_at) values (1,1,1,1,2,1,1,'2026-01-01','2026-01-01');
        insert into import_batches(id,user_id,source_kind,status,created_count,review_count,skipped_count,excluded_count,created_at) values (1,1,'text','confirmed',1,0,0,0,'2026-01-01');
        insert into activities(id,company_id,deal_id,user_id,owner_id,kind,body,created_at) values (1,1,1,1,2,'import','x','2026-01-01');
    """)
    con.commit()
    con.close()
    out = subprocess.run(alembic + ["upgrade", "head"], env=env, cwd=root, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    con = sqlite3.connect(target)
    assert con.execute("select login, role, team_id from users order by id").fetchall() == [("head", "owner", None), ("anna", "manager", None)]
    assert con.execute("select owner_id from deals").fetchall() == [(2,)]
    assert con.execute("select count(*) from activities").fetchone()[0] == 1
    assert con.execute("pragma foreign_key_check").fetchall() == []
    assert con.execute("select count(*) from payments").fetchone()[0] == 0
    con.close()
