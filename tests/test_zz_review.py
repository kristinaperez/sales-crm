"""Проверки по итогам независимого ревью перед выкладкой. Идут последними, на общей базе."""
import time

from sqlalchemy import func, select

from app.models import AuditLog, Company, Deal, Payment, User
from app.services import setup
from app.services.normalize import ensure_scheme
from app.services.worklists import today
from app.web import auth
from tests.conftest import _client

PWD = "team-pass-123"


def _uid(db, login):
    db.rollback()
    return db.scalar(select(User.id).where(User.login == login))


def test_long_unbroken_line_does_not_freeze_import(head):
    for blob in ("a" * 300_000, "a." * 150_000, "x@" * 100_000, "[" * 200_000):
        started = time.time()
        r = head.post("/import", data={"mode": "text", "text": blob})
        assert r.status_code in (303, 400) and time.time() - started < 5, (blob[:4], r.status_code, time.time() - started)


def test_broken_csv_gives_message_not_500(head):
    for content in (b'name;site\n"unclosed;a.ru\n' + b"x" * 300_000, bytes(range(256)) * 50, b"a;b\rc;d\re;f"):
        r = head.post("/import", data={"mode": "csv"}, files={"file": ("bad.csv", content, "text/csv")})
        assert r.status_code in (303, 400), r.status_code


def test_login_form_does_not_keep_or_echo_huge_values():
    from fastapi.testclient import TestClient

    from app.main import app

    anon = TestClient(app, base_url="http://testserver", headers={"Origin": "http://testserver"}, follow_redirects=False)
    auth._FAILS.clear()
    r = anon.post("/login", data={"login": "z" * 500_000, "password": "x"})
    assert r.status_code == 401 and len(r.content) < 20_000
    assert max(len(k) for k in auth._FAILS) < 200
    r = anon.post("/login", data={"login": "head", "password": "p" * 100_000})
    assert r.status_code == 401
    for i in range(auth._MAX_KEYS + 50):
        auth._fail(f"user{i}|1.2.3.4")
    assert len(auth._FAILS) <= auth._MAX_KEYS + 1
    auth._FAILS.clear()
    # лимит по адресу: перебор разных логинов с одного адреса тоже останавливается
    codes = [anon.post("/login", data={"login": f"guess{i}", "password": "x"}).status_code for i in range(auth._IP_LIMIT + 2)]
    assert codes[0] == 401 and codes[-1] == 429
    auth._FAILS.clear()


def test_logout_revokes_copied_session_cookie():
    client = _client("nina", PWD)
    stolen = dict(client.cookies)
    assert client.get("/tasks").status_code == 200
    assert client.post("/logout").status_code == 303
    from fastapi.testclient import TestClient

    from app.main import app

    thief = TestClient(app, base_url="http://testserver", headers={"Origin": "http://testserver"}, follow_redirects=False, cookies=stolen)
    r = thief.get("/tasks")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert _client("nina", PWD).get("/tasks").status_code == 200  # новый вход работает


def test_forwarded_host_header_is_not_trusted_and_csp_is_set(head):
    r = head.post("/settings/teams", data={"name": "Чужая"}, headers={"Origin": "http://evil.example", "X-Forwarded-Host": "evil.example"})
    assert r.status_code == 403
    csp = head.get("/pipeline").headers["content-security-policy"]
    assert "script-src 'self'" in csp and "form-action 'self'" in csp
    assert "onchange=" not in head.get("/pipeline").text and "onchange=" not in head.get("/tasks").text


def test_only_http_links_are_accepted_and_rendered(head, db):
    company = db.scalar(select(Company).where(Company.website_norm == "tundra-log.ru"))
    base = {"name": company.name, "city": company.city or ""}
    evil = "javascript://x.ru/%0aalert(document.cookie)"
    r = head.post(f"/companies/{company.id}/edit", data={**base, "website": evil})
    assert r.status_code == 400
    r = head.post(f"/companies/{company.id}/edit", data={**base, "website": "tundra-log.ru", "messenger_url": evil})
    assert r.status_code == 400 and "мессенджер" in r.text
    head.post(f"/companies/{company.id}/links", data={"url": evil})
    head.post(f"/companies/{company.id}/links", data={"url": "data:text/html,<script>alert(1)</script>"})
    page = head.get(f"/companies/{company.id}").text
    assert "javascript:" not in page and "data:text/html" not in page
    assert ensure_scheme(evil) == "#" and ensure_scheme("JAVASCRIPT:alert(1)") == "#" and ensure_scheme("t.me/x") == "https://t.me/x"
    bid = head.post("/import", data={"mode": "text", "text": f"Злая Фирма, Омск, {evil}, +7 900 000-00-00\n"})
    assert bid.status_code == 303
    preview = head.get(bid.headers["location"]).text
    # опасная схема отбрасывается; в поле сайта остаётся обычный адрес, который откроется как https://
    assert 'value="javascript:' not in preview and 'value="x.ru/' in preview


def test_second_lead_cannot_confirm_a_leads_payment(head, db):
    north = db.scalar(select(User.team_id).where(User.login == "lena"))
    if not _uid(db, "lena2"):
        setup.create_user(db, "lena2", "Лена Вторая", PWD, "lead", north)
        db.commit()
    lena, lena2 = _client("lena", PWD), _client("lena2", PWD)
    deal = db.scalar(select(Deal).join(Company).where(Company.website_norm == "siyanie-market.ru"))
    assert lena.post(f"/deals/{deal.id}/payments", data={"amount": "999999", "paid_on": today().isoformat()}).status_code == 303
    db.rollback()
    payment = db.scalar(select(Payment).where(Payment.deal_id == deal.id, Payment.amount_minor == 99999900))
    assert lena2.post(f"/payments/{payment.id}/status", data={"decision": "confirmed"}).status_code == 403
    db.rollback()
    assert db.get(Payment, payment.id).status == "pending"
    assert "Подтвердить" not in lena2.get("/payments").text.split("999 999")[1].split("</tr>")[0]
    # владелец отклоняет, потом передумывает — второе решение попадает в журнал
    assert head.post(f"/payments/{payment.id}/status", data={"decision": "rejected"}).status_code == 303
    before = db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == payment.id, AuditLog.entity == "оплата"))
    assert head.post(f"/payments/{payment.id}/status", data={"decision": "confirmed"}).status_code == 303
    db.rollback()
    after = db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == payment.id, AuditLog.entity == "оплата"))
    assert (before, after) == (0, 1)
    assert head.post(f"/payments/{payment.id}/delete").status_code == 303


def test_lead_can_reassign_deals_of_disabled_manager(head, db):
    lena = _client("lena", PWD)
    nina_id, max_id = _uid(db, "nina"), _uid(db, "max")
    deal = db.scalar(select(Deal).where(Deal.owner_id == nina_id, Deal.is_open.is_(True)))
    company_id, deal_id = deal.company_id, deal.id
    assert head.post(f"/settings/users/{nina_id}", data={"action": "toggle"}).status_code == 303
    try:
        r = lena.post("/companies/assign", data={"owner": str(nina_id), "scope": "all", "manager_id": [str(max_id)]})
        assert r.status_code == 303
        db.rollback()
        assert db.get(Deal, deal_id).owner_id == max_id
        assert db.scalar(select(func.count()).select_from(Deal).where(Deal.owner_id == nina_id, Deal.is_open.is_(True))) == 0
    finally:
        head.post(f"/settings/users/{nina_id}", data={"action": "toggle"})
    assert company_id


def test_out_of_range_numbers_and_dates_are_handled(head):
    huge = "9" * 23
    for url in (f"/deals/{huge}", f"/companies/{huge}", f"/import/{huge}", f"/companies?page={huge}", f"/companies?stage_id={huge}",
                f"/pipeline?pipeline_id={huge}", f"/tasks?owner={huge}"):
        assert head.get(url).status_code in (200, 404, 422), url
    assert head.get("/reports?date_from=0001-01-01&date_to=9999-12-31").status_code == 200
    assert head.get("/rating?month=9999-99").status_code == 200
    assert head.post(f"/deals/{huge}/stage", data={"stage_id": huge, "version": huge}).status_code in (404, 422)


def test_duplicate_message_does_not_reveal_foreign_company_name(db):
    pavel = _client("pavel", PWD)
    foreign = db.scalar(select(Company).where(Company.website_norm == "tundra-log.ru"))
    r = pavel.post("/companies/new", data={"name": "Проба", "website": "tundra-log.ru"})
    assert r.status_code == 400 and foreign.name not in r.text and "уже есть в базе" in r.text and "в работе у" in r.text
