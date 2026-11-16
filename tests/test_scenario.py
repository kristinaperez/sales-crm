"""Сквозной сценарий из ТЗ. Тесты идут по порядку и опираются на общее состояние базы."""
import re
import threading
from datetime import timedelta

from sqlalchemy import func, select

from app.models import Activity, Company, CompanyLink, Deal, ImportBatch, ImportRow, Stage, Task, User
from app.services import importer
from app.services.worklists import today

MIXED_LIST = """Подобрал компании для холодного обхода:

1. **ООО «Ромашка»** — Казань, доставка цветов. Сайт: https://www.romashka-flowers.ru/contacts, тел. +7 (843) 200-11-22, info@romashka-flowers.ru
2. Студия загара «Бронза», Самара — https://vk.com/bronza_samara
3. https://dentalux-clinic.ru
4. [Автосервис Гараж 77](https://garage77.ru) — Москва, +7 495 123-45-67
5. Кофейня Зерно
6. **Фитнес-клуб Атлет**
   Город: Екатеринбург
   Сайт: atlet-fit.ru
   Телефон: 8 (343) 555-66-77
   Telegram: @atlet_fit
7. ROMASHKA-FLOWERS.RU — дубль первой строки
8. https://2gis.ru/moscow/firm/70000001012345

| Компания | Город | Сайт | Телефон | Ниша |
|---|---|---|---|---|
| Клиника Улыбка | Тверь | ulybka-tver.ru | +7 4822 33-44-55 | стоматология |
| Школа Лингва | Пермь | [lingva.school](https://lingva.school) | — | онлайн-школа |
"""

S = {}


def _batch_id(response) -> int:
    assert response.status_code == 303, response.text
    return int(re.search(r"/import/(\d+)", response.headers["location"]).group(1))


def _rows(db, batch_id):
    db.rollback()
    return {r.row_no: r for r in db.scalars(select(ImportRow).where(ImportRow.batch_id == batch_id))}


def _stage(db, name) -> Stage:
    db.rollback()
    return db.scalar(select(Stage).where(Stage.name == name))


def test_sqlite_runs_in_wal_with_foreign_keys(db):
    assert db.connection().exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
    assert db.connection().exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_01_head_pastes_mixed_list_and_sees_three_statuses(head, db):
    S["batch"] = bid = _batch_id(head.post("/import", data={"mode": "text", "text": MIXED_LIST}))
    rows = _rows(db, bid)
    assert len(rows) == 10
    status = {no: r.status for no, r in rows.items()}
    assert status[1] == "new" and status[4] == "new" and status[6] == "new" and status[9] == "new"
    assert status[2] == "review"  # только ссылка на соцсеть — не сайт
    assert status[3] == "review"  # только URL
    assert status[5] == "review"  # одно название
    assert status[8] == "review"  # только ссылка на карту
    assert status[7] == "duplicate" and rows[7].match_kind == "batch_website" and rows[7].match_row_no == 1
    assert rows[2].website == "" and rows[2].links == ["https://vk.com/bronza_samara"]
    page = head.get(f"/import/{bid}")
    assert page.status_code == 200
    for label in ("Новая компания", "Возможный дубль", "Нужно проверить", "Повтор строки №1"):
        assert label in page.text
    assert db.scalar(select(func.count()).select_from(Company)) == 0  # до подтверждения ничего не записано


def test_02_head_fixes_rows_in_preview(head, db):
    bid, rows = S["batch"], _rows(db, S["batch"])
    r3, r5 = rows[3], rows[5]
    data = {"row_id": [str(r3.id), str(r5.id)], "status": "", "page": "1"}
    for r in (r3, r5):
        for f in importer.EDITABLE:
            data[f"{f}_{r.id}"] = getattr(r, f)
    data[f"name_{r3.id}"] = "Стоматология Денталюкс"
    data[f"city_{r3.id}"] = "Уфа"
    data[f"excluded_{r5.id}"] = "1"  # «Кофейня Зерно» без контактов — исключаем
    assert head.post(f"/import/{bid}/rows", data=data).status_code == 303
    rows = _rows(db, bid)
    assert rows[3].status == "new" and rows[3].name == "Стоматология Денталюкс"
    assert rows[5].excluded is True
    assert "Исключено вами" in head.get(f"/import/{bid}").text


def test_03_confirm_import_creates_cards_and_deals(head, db):
    bid = S["batch"]
    pipeline_id = db.scalar(select(Stage.pipeline_id).where(Stage.name == "Новая"))
    r = head.post(f"/import/{bid}/confirm", data={"pipeline_id": str(pipeline_id), "owner_id": ""})
    assert r.status_code == 303
    db.rollback()
    batch = db.get(ImportBatch, bid)
    assert (batch.created_count, batch.review_count, batch.skipped_count, batch.excluded_count) == (6, 2, 1, 1)
    assert db.scalar(select(func.count()).select_from(Company)) == 8
    assert db.scalar(select(func.count()).select_from(Deal)) == 8
    result = head.get(f"/import/{bid}")
    assert "Создано карточек" in result.text and "Пропущено как дубли" in result.text
    link_only = db.scalar(select(Company).join(CompanyLink).where(CompanyLink.url.like("%2gis%")))
    assert link_only.status == "needs_review" and link_only.website_norm is None
    assert "2gis.ru" in link_only.source_fragment and "Импорт №" in link_only.source
    bronza = db.scalar(select(Company).where(Company.name.like("%Бронза%")))
    assert bronza.website is None and bronza.status == "needs_review"
    # второй раз тот же импорт подтвердить нельзя
    assert head.post(f"/import/{bid}/confirm", data={"pipeline_id": str(pipeline_id)}).status_code == 303
    assert db.scalar(select(func.count()).select_from(Company)) == 8


def test_04_head_distributes_deals_between_two_managers(head, db):
    ids = [str(i) for i in db.scalars(select(Company.id))]
    anna_id, boris_id = (db.scalar(select(User.id).where(User.login == x)) for x in ("anna", "boris"))
    r = head.post("/companies/assign", data={"company_id": ids, "manager_id": [str(anna_id), str(boris_id)], "scope": "selected"})
    assert r.status_code == 303
    db.rollback()
    per_owner = dict(db.execute(select(Deal.owner_id, func.count()).group_by(Deal.owner_id)).all())
    assert per_owner == {anna_id: 4, boris_id: 4}
    S["anna_id"], S["boris_id"] = anna_id, boris_id
    S["anna_deal"] = db.scalar(select(Deal.id).where(Deal.owner_id == anna_id).order_by(Deal.id))
    S["boris_deal"] = db.scalar(select(Deal.id).where(Deal.owner_id == boris_id).order_by(Deal.id))


def test_05_manager_sees_only_own_deals(anna, db):
    names_anna = set(db.scalars(select(Company.name).join(Deal).where(Deal.owner_id == S["anna_id"])))
    names_boris = set(db.scalars(select(Company.name).join(Deal).where(Deal.owner_id == S["boris_id"])))
    for url in ("/companies", "/pipeline"):
        page = anna.get(url).text
        assert all(n.replace("«", "&laquo;") in page or n in page for n in names_anna)
        assert not any(n in page for n in names_boris)


def test_06_manager_logs_first_contact_sets_followup_and_moves_to_reply(anna, db):
    deal_id = S["anna_deal"]
    deal = db.get(Deal, deal_id)
    r = anna.post(f"/deals/{deal_id}/touch", data={
        "version": str(deal.version), "kind": "message", "body": "Написала в WhatsApp, отправила короткое предложение",
        "stage_id": str(_stage(db, "Первый контакт").id), "followup_due": (today() + timedelta(days=2)).isoformat(),
        "followup_title": "Напомнить о предложении",
    })
    assert r.status_code == 303
    db.rollback()
    deal = db.get(Deal, deal_id)
    assert deal.stage.name == "Первый контакт"
    task = db.scalar(select(Task).where(Task.deal_id == deal_id))
    assert task.title == "Напомнить о предложении" and task.assignee_id == S["anna_id"] and task.done_at is None

    r = anna.post(f"/deals/{deal_id}/stage", data={"version": str(deal.version), "stage_id": str(_stage(db, "Есть ответ").id)})
    assert r.status_code == 303
    db.rollback()
    assert db.get(Deal, deal_id).stage.name == "Есть ответ"
    kinds = list(db.scalars(select(Activity.kind).where(Activity.deal_id == deal_id).order_by(Activity.id)))
    assert kinds.count("stage_change") == 2 and "message" in kinds and "task" in kinds and "owner_change" in kinds


def test_07_head_sees_manager_changes(head, db):
    page = head.get(f"/deals/{S['anna_deal']}").text
    for text in ("Есть ответ", "Написала в WhatsApp", "Напомнить о предложении", "Первый контакт → Есть ответ", "Анна Смирнова"):
        assert text in page
    assert "Есть ответ" in head.get("/pipeline").text
    report = head.get("/reports").text
    row = re.search(r"Анна Смирнова.*?</tr>", report, re.S).group(0)
    assert re.findall(r'<td class="num">(\d+)</td>', row) == ["1", "1", "0", "0"]
    tasks = head.get(f"/tasks?tab=no_next&owner={S['boris_id']}").text
    assert "Нет ни следующего шага, ни задачи" in tasks


def test_08_reimport_keeps_history_owners_and_manual_edits(head, db):
    deal = db.get(Deal, S["anna_deal"])
    company = db.get(Company, deal.company_id)
    r = head.post(f"/companies/{company.id}/edit", data={
        "name": company.name, "website": company.website or "", "city": company.city or "", "industry": "ручная правка",
        "phone": "+7 900 000-00-00", "email": company.email or "", "messenger_url": company.messenger_url or "", "note": "проверено руководителем",
    })
    assert r.status_code == 303
    db.rollback()
    before = {
        "companies": db.scalar(select(func.count()).select_from(Company)),
        "deals": db.scalar(select(func.count()).select_from(Deal)),
        "activities": db.scalar(select(func.count()).select_from(Activity)),
        "owners": dict(db.execute(select(Deal.id, Deal.owner_id)).all()),
        "stages": dict(db.execute(select(Deal.id, Deal.stage_id)).all()),
    }
    bid = _batch_id(head.post("/import", data={"mode": "text", "text": MIXED_LIST}))
    rows = _rows(db, bid)
    assert all(r.status == "duplicate" for r in rows.values()), {n: (r.status, r.name) for n, r in rows.items()}
    assert rows[5].match_kind == "excluded_before"  # исключённая в прошлый раз строка не возвращается сама
    preview = head.get(f"/import/{bid}").text
    assert "уже исключали в прошлом импорте" in preview
    assert "Ответственный: Анна Смирнова" in preview or "Ответственный: Борис Орлов" in preview
    pipeline_id = db.scalar(select(Stage.pipeline_id).where(Stage.name == "Новая"))
    assert head.post(f"/import/{bid}/confirm", data={"pipeline_id": str(pipeline_id)}).status_code == 303
    db.rollback()
    batch = db.get(ImportBatch, bid)
    assert (batch.created_count, batch.review_count, batch.skipped_count) == (0, 0, 10)
    after = {
        "companies": db.scalar(select(func.count()).select_from(Company)),
        "deals": db.scalar(select(func.count()).select_from(Deal)),
        "activities": db.scalar(select(func.count()).select_from(Activity)),
        "owners": dict(db.execute(select(Deal.id, Deal.owner_id)).all()),
        "stages": dict(db.execute(select(Deal.id, Deal.stage_id)).all()),
    }
    assert after == before
    company = db.get(Company, company.id)
    assert (company.industry, company.phone, company.note) == ("ручная правка", "+7 900 000-00-00", "проверено руководителем")


def test_09_manager_cannot_open_foreign_deal_by_direct_link(anna, boris, db):
    foreign = db.get(Deal, S["anna_deal"])
    assert boris.get(f"/deals/{foreign.id}").status_code == 404
    assert boris.get(f"/companies/{foreign.company_id}").status_code == 404
    stage_before = foreign.stage_id
    posts = [
        (f"/deals/{foreign.id}/stage", {"version": str(foreign.version), "stage_id": str(_stage(db, "Демо").id)}),
        (f"/deals/{foreign.id}/touch", {"kind": "note", "body": "чужая запись"}),
        (f"/deals/{foreign.id}/tasks", {"title": "x", "due_date": today().isoformat()}),
        (f"/deals/{foreign.id}/next-step", {"next_step": "x", "next_step_due": today().isoformat()}),
        (f"/companies/{foreign.company_id}/edit", {"name": "взлом"}),
        (f"/companies/{foreign.company_id}/contacts", {"full_name": "Чужой"}),
        (f"/tasks/{db.scalar(select(Task.id).where(Task.deal_id == foreign.id))}/done", {}),
    ]
    for url, data in posts:
        assert boris.post(url, data=data).status_code == 404, url
    db.rollback()
    assert db.get(Deal, foreign.id).stage_id == stage_before
    assert "взлом" not in db.get(Company, foreign.company_id).name
    # свою сделку менеджер не может передать сам, а разделы руководителя закрыты
    own = db.get(Deal, S["boris_deal"])
    assert boris.post(f"/deals/{own.id}/owner", data={"version": str(own.version), "owner_id": str(S["boris_id"])}).status_code == 403
    for url in ("/import", "/reports", "/settings/users", "/settings/pipelines", "/companies/export.csv", "/companies/new"):
        assert boris.get(url).status_code == 403, url
    assert boris.post("/companies/assign", data={"company_id": ["1"], "manager_id": [str(S["boris_id"])]}).status_code == 403
    assert boris.post("/import", data={"mode": "text", "text": "x.ru"}).status_code == 403
    # фильтр по чужому менеджеру не расширяет доступ
    assert "Ромашка" not in boris.get(f"/companies?owner={S['anna_id']}").text or True
    page = boris.get(f"/companies?owner={S['anna_id']}").text
    foreign_names = set(db.scalars(select(Company.name).join(Deal).where(Deal.owner_id == S["anna_id"])))
    assert not any(n in page for n in foreign_names)


def test_10_anonymous_is_redirected_to_login():
    from fastapi.testclient import TestClient

    from app.main import app

    anon = TestClient(app, base_url="http://testserver", headers={"Origin": "http://testserver"}, follow_redirects=False)
    for url in ("/", "/companies", f"/deals/{S['anna_deal']}", "/import", "/companies/export.csv"):
        r = anon.get(url)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), url
    assert anon.post("/login", data={"login": "anna", "password": "wrong"}).status_code == 401


def test_11_cross_site_post_is_rejected(head):
    assert head.post("/companies/assign", data={}, headers={"Origin": "http://evil.example"}).status_code == 403


def test_12_concurrent_stage_and_owner_change_reports_conflict(head, anna, db):
    deal = db.get(Deal, S["anna_deal"])
    stale = deal.version
    # руководитель и менеджер открыли карточку одновременно; менеджер успел первым
    assert anna.post(f"/deals/{deal.id}/stage", data={"version": str(stale), "stage_id": str(_stage(db, "Демо").id)}).status_code == 303
    r = head.post(f"/deals/{deal.id}/stage", data={"version": str(stale), "stage_id": str(_stage(db, "Предложение").id)})
    assert r.status_code == 409 and "Изменение не записано" in r.text and "этап «Демо»" in r.text
    r = head.post(f"/deals/{deal.id}/owner", data={"version": str(stale), "owner_id": str(S["boris_id"])})
    assert r.status_code == 409 and "Анна Смирнова" in r.text
    db.rollback()
    deal = db.get(Deal, deal.id)
    assert deal.stage.name == "Демо" and deal.owner_id == S["anna_id"]
    # с актуальной версией действие проходит
    assert head.post(f"/deals/{deal.id}/stage", data={"version": str(deal.version), "stage_id": str(_stage(db, "Предложение").id)}).status_code == 303


def test_13_lost_stage_requires_reason(anna, db):
    deal = db.get(Deal, S["anna_deal"])
    lost = _stage(db, "Закрыто без продажи")
    r = anna.post(f"/deals/{deal.id}/stage", data={"version": str(deal.version), "stage_id": str(lost.id), "lost_reason": ""})
    assert r.status_code == 400 and "причину отказа" in r.text


def test_14_parallel_imports_do_not_create_duplicates(db):
    from app.db import SessionLocal

    head = db.scalar(select(User).where(User.login == "head"))
    text = "Новая Компания Икс, Омск, https://parallel-x.ru, +7 3812 11-22-33\nВторая Игрек, Томск, parallel-y.ru\n"
    batches = []
    for _ in range(4):
        with SessionLocal() as s:
            batches.append(importer.create_text_batch(s, s.get(User, head.id), "text", None, text).id)
    # все четыре предпросмотра считают строки новыми
    assert all(r.status == "new" for b in batches for r in _rows(db, b).values())
    errors = []
    barrier = threading.Barrier(4)

    def run(batch_id):
        try:
            barrier.wait()
            with SessionLocal() as s:
                importer.confirm(s, batch_id, s.get(User, head.id), None, None)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(b,)) for b in batches]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Company).where(Company.website_norm.in_(["parallel-x.ru", "parallel-y.ru"]))) == 2
    created = sorted(db.get(ImportBatch, b).created_count for b in batches)
    skipped = sorted(db.get(ImportBatch, b).skipped_count for b in batches)
    assert created == [0, 0, 0, 2] and skipped == [0, 2, 2, 2]


def test_15_name_and_city_match_is_a_hint_that_user_can_override(head, db):
    bid = _batch_id(head.post("/import", data={"mode": "text", "text": "Клиника «Улыбка», Тверь, +7 999 111-22-33\n"}))
    row = _rows(db, bid)[1]
    assert row.status == "duplicate" and row.match_kind == "name_city" and not row.is_hard_duplicate
    assert "Похожи название и город" in head.get(f"/import/{bid}").text
    data = {"row_id": [str(row.id)], f"force_{row.id}": "1", "pipeline_id": ""}
    for f in importer.EDITABLE:
        data[f"{f}_{row.id}"] = getattr(row, f)
    assert head.post(f"/import/{bid}/confirm", data=data).status_code == 303
    db.rollback()
    batch = db.get(ImportBatch, bid)
    assert (batch.created_count, batch.review_count, batch.skipped_count) == (0, 1, 0)
    assert db.scalar(select(func.count()).select_from(Company).where(Company.name_norm == "клиника улыбка")) == 2


def test_16_csv_import_with_column_mapping(head, db):
    csv_text = "Фирма;Населённый пункт;Web;Мобильный;Комментарий\nПекарня Колос;Рязань;kolos-bakery.ru;+7 4912 00-11-22;пишут в директ\nvk-only;Тула;https://vk.com/vkonly;;\n"
    r = head.post("/import", data={"mode": "csv"}, files={"file": ("list.csv", csv_text.encode("cp1251"), "text/csv")})
    assert r.status_code == 303 and r.headers["location"].endswith("/mapping")
    bid = int(re.search(r"/import/(\d+)", r.headers["location"]).group(1))
    page = head.get(f"/import/{bid}/mapping")
    assert page.status_code == 200 and "Населённый пункт" in page.text
    r = head.post(f"/import/{bid}/mapping", data={"col_0": "name", "col_1": "city", "col_2": "website", "col_3": "phone", "col_4": "note", "has_header": "1"})
    assert r.status_code == 303
    rows = _rows(db, bid)
    assert (rows[1].name, rows[1].city, rows[1].website, rows[1].note) == ("Пекарня Колос", "Рязань", "kolos-bakery.ru", "пишут в директ")
    assert rows[2].website == "" and rows[2].links == ["https://vk.com/vkonly"] and rows[2].status == "review"
    assert head.post(f"/import/{bid}/confirm", data={"pipeline_id": ""}).status_code == 303
    db.rollback()
    assert db.scalar(select(Company.source).where(Company.website_norm == "kolos-bakery.ru")).endswith("CSV-файл: list.csv")


def test_17_google_sheets_range_and_txt_file(head, db):
    tsv = "Название\tГород\tСайт\nЛофт Мебель\tТула\tloft-mebel.ru\nПекарня Колос\tРязань\thttps://www.kolos-bakery.ru/\n"
    bid = _batch_id(head.post("/import", data={"mode": "sheets", "text": tsv}))
    rows = _rows(db, bid)
    assert rows[1].status == "new" and rows[2].status == "duplicate" and rows[2].match_kind == "website"
    bid = _batch_id(head.post("/import", data={"mode": "txt"}, files={"file": ("urls.txt", "https://only-url-one.ru\nhttps://only-url-two.ru\n".encode(), "text/plain")}))
    assert [r.status for r in _rows(db, bid).values()] == ["review", "review"]


def test_18_task_tabs_today_overdue_no_next_step(anna, boris, db):
    with db.begin():
        task = db.scalar(select(Task).where(Task.title == "Напомнить о предложении"))
        task.due_date = today() - timedelta(days=1)
    assert "Напомнить о предложении" in anna.get("/tasks?tab=overdue").text
    assert "Напомнить о предложении" not in anna.get("/tasks?tab=today").text
    own = db.get(Deal, S["boris_deal"])
    name = db.get(Company, own.company_id).name
    assert name in boris.get("/tasks?tab=no_next").text
    r = boris.post(f"/deals/{own.id}/next-step", data={"version": str(own.version), "next_step": "Позвонить директору", "next_step_due": today().isoformat()})
    assert r.status_code == 303
    assert "Позвонить директору" in boris.get("/tasks?tab=today").text
    assert name not in boris.get("/tasks?tab=no_next").text
    assert "Напомнить о предложении" not in boris.get("/tasks?tab=overdue").text  # чужие задачи не видны
    assert anna.post(f"/tasks/{task.id}/done", data={"back": "/tasks?tab=overdue"}).status_code == 303
    assert "Напомнить о предложении" not in anna.get("/tasks?tab=overdue").text


def test_19_filters_and_csv_export(head, db):
    page = head.get("/companies?city=Казань").text
    assert "Ромашка" in page and "Гараж 77" not in page
    page = head.get(f"/companies?stage_id={_stage(db, 'Предложение').id}").text
    assert "Найдено: 1" in page
    assert "Найдено: 4" in head.get(f"/companies?owner={S['boris_id']}").text
    assert "Улыбка" in head.get("/companies?industry=стоматология").text
    assert "Гараж 77" in head.get("/companies?q=гараж").text
    r = head.get("/companies/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    body = r.content.decode("utf-8-sig")
    assert body.splitlines()[0].startswith("ID;Название;Сайт;Город")
    assert "Анна Смирнова" in body and "Предложение" in body and "romashka-flowers.ru" in body


def test_20_head_adds_pipeline_and_stage(head, db):
    assert head.post("/settings/pipelines", data={"name": "Повторные продажи"}).status_code == 303
    from app.models import Pipeline

    pipeline = db.scalar(select(Pipeline).where(Pipeline.name == "Повторные продажи"))
    assert head.post(f"/settings/pipelines/{pipeline.id}", data={"action": "add_stage", "name": "Переговоры"}).status_code == 303
    db.rollback()
    names = [s.name for s in db.get(Pipeline, pipeline.id).stages]
    assert names == ["Новая", "Переговоры", "Оплачено", "Закрыто без продажи"]
    page = head.get("/settings/pipelines").text
    assert "Переговоры" in page and "Холодные продажи" in page


def test_21_backup_and_restore_keep_whole_database_with_history(db, tmp_path, capsys):
    import sqlite3

    from app import cli

    cli.cmd_backup(str(tmp_path))
    copy = next(tmp_path.glob("crm-*.db"))
    con = sqlite3.connect(copy)
    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("companies", "deals", "activities", "tasks", "users", "import_rows")}
    con.close()
    db.rollback()
    assert counts["companies"] == db.scalar(select(func.count()).select_from(Company))
    assert counts["activities"] == db.scalar(select(func.count()).select_from(Activity)) > 20
    assert counts["import_rows"] == db.scalar(select(func.count()).select_from(ImportRow))


def test_22_restore_brings_back_companies_deals_and_history(db, tmp_path):
    import os
    import sqlite3
    import subprocess
    import sys

    from app import cli

    backups = tmp_path / "b"
    cli.cmd_backup(str(backups))
    copy = next(backups.glob("crm-*.db"))
    target = tmp_path / "restored" / "crm.db"
    target.parent.mkdir()
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{target}"}
    out = subprocess.run([sys.executable, "-m", "app.cli", "restore", str(copy)], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    con = sqlite3.connect(target)
    db.rollback()
    assert con.execute("SELECT count(*) FROM companies").fetchone()[0] == db.scalar(select(func.count()).select_from(Company))
    assert con.execute("SELECT count(*) FROM activities").fetchone()[0] == db.scalar(select(func.count()).select_from(Activity))
    assert con.execute("SELECT count(*) FROM deals WHERE owner_id IS NOT NULL").fetchone()[0] >= 8
    con.close()
    bad = tmp_path / "bad.db"
    bad.write_text("это не база")
    out = subprocess.run([sys.executable, "-m", "app.cli", "restore", str(bad)], env=env, capture_output=True, text=True)
    assert out.returncode != 0


def test_23_parallel_imports_interleaving_by_portions(db, monkeypatch):
    """Импорты идут порциями и перемежаются: дублей всё равно нет."""
    from app.db import SessionLocal

    monkeypatch.setattr(importer, "CONFIRM_CHUNK", 3)
    head_id = db.scalar(select(User.id).where(User.login == "head"))
    text = "".join(f"Порционная {i}, Омск, portion-{i}.ru, +7 900 000-{i:02d}-00\n" for i in range(30)) + "Без сайта Порция, Томск, +7 900 111-22-33\n"
    batches = []
    for _ in range(3):
        with SessionLocal() as s:
            batches.append(importer.create_text_batch(s, s.get(User, head_id), "text", None, text).id)
    errors, barrier = [], threading.Barrier(3)

    def run(batch_id):
        try:
            barrier.wait()
            with SessionLocal() as s:
                importer.confirm(s, batch_id, s.get(User, head_id), None, None)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(b,)) for b in batches]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Company).where(Company.website_norm.like("portion-%"))) == 30
    assert db.scalar(select(func.count()).select_from(Company).where(Company.name_norm == "без сайта порция")) == 1
    totals = [db.get(ImportBatch, b) for b in batches]
    assert sum(b.created_count for b in totals) == 31 and sum(b.skipped_count for b in totals) == 62
    assert all(b.status == "confirmed" for b in totals)


def test_24_interrupted_import_can_be_resumed_without_duplicates(head, db, monkeypatch):
    monkeypatch.setattr(importer, "CONFIRM_CHUNK", 2)
    text = "".join(f"Прерванная {i}, Тула, resume-{i}.ru\n" for i in range(6))
    bid = _batch_id(head.post("/import", data={"mode": "text", "text": text}))
    original, calls = importer._create_chunk, []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("сервер перезапустили")
        return original(*args, **kwargs)

    monkeypatch.setattr(importer, "_create_chunk", flaky)
    head_id = db.scalar(select(User.id).where(User.login == "head"))
    from app.db import SessionLocal

    with SessionLocal() as s:
        try:
            importer.confirm(s, bid, s.get(User, head_id), None, None)
        except RuntimeError:
            s.rollback()
    db.rollback()
    assert db.get(ImportBatch, bid).status == "confirming"
    assert db.scalar(select(func.count()).select_from(Company).where(Company.website_norm.like("resume-%"))) == 2
    page = head.get(f"/import/{bid}")
    assert "Продолжить импорт" in page.text
    monkeypatch.setattr(importer, "_create_chunk", original)
    assert head.post(f"/import/{bid}/confirm", data={"resume": "1"}).status_code == 303
    db.rollback()
    batch = db.get(ImportBatch, bid)
    assert batch.status == "confirmed" and batch.created_count == 6
    assert db.scalar(select(func.count()).select_from(Company).where(Company.website_norm.like("resume-%"))) == 6


def test_25_preview_and_confirm_agree_on_name_matches(head, db):
    text = "Согласованная Фирма, +7 900 555-00-01\nСогласованная Фирма, Казань, +7 900 555-00-02\n"
    bid = _batch_id(head.post("/import", data={"mode": "text", "text": text}))
    rows = _rows(db, bid)
    assert rows[1].status == "new" and rows[2].status == "duplicate" and rows[2].match_kind == "batch_name"
    assert head.post(f"/import/{bid}/confirm", data={"pipeline_id": ""}).status_code == 303
    db.rollback()
    batch = db.get(ImportBatch, bid)
    assert (batch.created_count, batch.skipped_count) == (1, 1)
