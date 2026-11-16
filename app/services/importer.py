"""Импорт компаний: предпросмотр, поиск дублей, правки и подтверждение одной транзакцией."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from sqlalchemy import func, select, update
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import config
from ..models import (
    BATCH_CONFIRMED,
    BATCH_CONFIRMING,
    BATCH_MAPPING,
    BATCH_PREVIEW,
    COMPANY_OK,
    COMPANY_REVIEW,
    MATCH_BATCH_LINK,
    MATCH_BATCH_NAME,
    MATCH_BATCH_WEBSITE,
    MATCH_EXCLUDED,
    MATCH_LINK,
    MATCH_NAME_CITY,
    MATCH_WEBSITE,
    ROW_DUPLICATE,
    ROW_NEW,
    ROW_REVIEW,
    Activity,
    Company,
    CompanyLink,
    Deal,
    ImportBatch,
    ImportRow,
    Pipeline,
    User,
    utcnow,
)
from .access import assignable_users
from .normalize import classify_url, display_host, normalize_city, normalize_link, normalize_name, normalize_website
from .parsing import ParsedRow, csv_rows, guess_field, parse_text, read_csv

EDITABLE = ("name", "website", "city", "industry", "phone", "email", "messenger_url", "note")


class ImportError_(Exception):
    """Ошибка импорта с понятным пользователю текстом."""


def raw_hash(raw: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", raw or "").strip().lower().encode()).hexdigest()


def _chunks(items: list, size: int = 400) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# --- Создание пакета ----------------------------------------------------------------------


def _store_rows(db: Session, batch: ImportBatch, parsed: list[ParsedRow]) -> None:
    if not parsed:
        raise ImportError_("Не удалось распознать ни одной строки. Проверьте, что список не пустой.")
    if len(parsed) > config.MAX_IMPORT_ROWS:
        raise ImportError_(
            f"В списке {len(parsed)} строк — больше лимита {config.MAX_IMPORT_ROWS}. Разделите его на части."
        )
    for no, p in enumerate(parsed, start=1):
        row = ImportRow(
            batch=batch, row_no=no, raw=p.raw, raw_hash=raw_hash(p.raw), name=p.name, website=p.website, city=p.city,
            industry=p.industry, phone=p.phone, email=p.email, messenger_url=p.messenger_url, note=p.note,
        )
        row.links = p.links
        db.add(row)
    db.flush()
    recompute(db, batch.id)


def create_text_batch(db: Session, user: User, kind: str, source_name: str | None, text: str) -> ImportBatch:
    """Вставка текста, TXT-файл или диапазон Google Таблиц."""
    if not (text or "").strip():
        raise ImportError_("Список пустой — вставьте текст или выберите файл.")
    parsed = parse_text(text, tabular=(kind == "sheets"))  # разбор — до начала записи в базу
    batch = ImportBatch(user_id=user.id, source_kind=kind, source_name=source_name, raw_text=text, status=BATCH_PREVIEW)
    db.add(batch)
    db.flush()
    _store_rows(db, batch, parsed)
    db.commit()
    return batch


def create_csv_batch(db: Session, user: User, source_name: str | None, text: str) -> ImportBatch:
    """CSV: сначала шаг сопоставления колонок."""
    table = read_csv(text)
    if not table:
        raise ImportError_("В CSV-файле нет строк.")
    batch = ImportBatch(user_id=user.id, source_kind="csv", source_name=source_name, raw_text=text, status=BATCH_MAPPING)
    db.add(batch)
    db.commit()
    return batch


def csv_preview(batch: ImportBatch) -> dict:
    table = read_csv(batch.raw_text or "")
    width = max(len(r) for r in table)
    first = table[0] + [""] * (width - len(table[0]))
    guessed = [guess_field(c) for c in first]
    has_header = sum(1 for g in guessed if g and g != "skip") >= 1
    if has_header:
        mapping = ["skip" if g == "skip" else (g if g and g != "contacts" else "note") for g in guessed]
    else:
        mapping = ["note"] * width
    return {
        "headers": first if has_header else [f"Колонка {i + 1}" for i in range(width)],
        "mapping": mapping,
        "has_header": has_header,
        "sample": table[1:6] if has_header else table[:5],
        "total": len(table) - (1 if has_header else 0),
        "width": width,
    }


def apply_csv_mapping(db: Session, batch: ImportBatch, mapping: list[str], has_header: bool) -> None:
    if batch.status != BATCH_MAPPING:
        raise ImportError_("Сопоставление колонок для этого импорта уже выполнено.")
    if "name" not in mapping and "website" not in mapping:
        raise ImportError_("Укажите хотя бы колонку «Название» или «Сайт».")
    table = read_csv(batch.raw_text or "")
    parsed = csv_rows(table, [m or "note" for m in mapping], has_header)
    batch.status = BATCH_PREVIEW
    _store_rows(db, batch, parsed)
    db.commit()


# --- Поиск дублей -------------------------------------------------------------------------


def _sanitize(row: ImportRow) -> None:
    """Если в поле «Сайт» оказалась соцсеть, карта или каталог — переносит её в ссылки."""
    site = (row.website or "").strip()
    row.website = site
    if not site:
        return
    kind, _ = classify_url(site)
    if kind == "website":
        return
    row.website = ""
    if kind == "invalid":
        row.note = f"{row.note}; сайт: {site}".strip("; ") if site not in row.note else row.note
        return
    if kind == "messenger" and not row.messenger_url:
        row.messenger_url = site
        return
    links = row.links
    if all(normalize_link(x) != normalize_link(site) for x in links):
        row.links = links + [site]


class _Matcher:
    """Ищет совпадения строк с базой и между собой. Состояние «уже встречалось» живёт на весь пакет."""

    def __init__(self, db: Session, batch_id: int, user_id: int):
        # user_id передаётся готовым: конструктор не должен читать базу до первой записи транзакции.
        self.db, self.batch_id, self.user_id = db, batch_id, user_id
        self.seen_sites: dict[str, int] = {}
        self.seen_links: dict[str, int] = {}
        self.seen_names: dict[str, list[tuple[str, int]]] = {}

    def match(self, rows: list[ImportRow]) -> None:
        db = self.db
        keys = []
        for row in rows:
            _sanitize(row)
            keys.append(
                (
                    normalize_website(row.website),
                    [normalize_link(x) for x in row.links],
                    normalize_name(row.name),
                    normalize_city(row.city),
                )
            )
        sites = sorted({k[0] for k in keys if k[0]})
        links = sorted({x for k in keys for x in k[1]})
        names = sorted({k[2] for k in keys if k[2]})

        db_sites: dict[str, int] = {}
        for chunk in _chunks(sites):
            for cid, norm in db.execute(select(Company.id, Company.website_norm).where(Company.website_norm.in_(chunk))):
                db_sites[norm] = cid
        db_links: dict[str, int] = {}
        for chunk in _chunks(links):
            for cid, norm in db.execute(
                select(CompanyLink.company_id, CompanyLink.url_norm)
                .where(CompanyLink.url_norm.in_(chunk)).order_by(CompanyLink.id.desc())
            ):
                db_links[norm] = cid
        db_names: dict[str, list[tuple[int, str]]] = {}
        for chunk in _chunks(names):
            for cid, norm, city in db.execute(
                select(Company.id, Company.name_norm, Company.city_norm)
                .where(Company.name_norm.in_(chunk)).order_by(Company.id)
            ):
                db_names.setdefault(norm, []).append((cid, city or ""))
        # Строки, которые этот же пользователь исключил в своих уже подтверждённых импортах.
        excluded_before: set[str] = set()
        for chunk in _chunks(sorted({r.raw_hash for r in rows if r.raw_hash})):
            excluded_before.update(
                db.scalars(
                    select(ImportRow.raw_hash)
                    .join(ImportBatch, ImportRow.batch_id == ImportBatch.id)
                    .where(
                        ImportRow.raw_hash.in_(chunk), ImportRow.result == "excluded",
                        ImportBatch.status == BATCH_CONFIRMED, ImportRow.batch_id != self.batch_id,
                        ImportBatch.user_id == self.user_id,
                    )
                )
            )

        def earlier(seen: dict, key, row: ImportRow) -> int | None:
            no = seen.get(key)
            return no if no is not None and no != row.row_no else None

        for row, (site, link_keys, name, city) in zip(rows, keys):
            status, kind, company_id, row_no = ROW_NEW, None, None, None
            link_hit = next((x for x in link_keys if x in db_links), None)
            batch_link = next((earlier(self.seen_links, x, row) for x in link_keys if earlier(self.seen_links, x, row)), None)
            name_hit = None
            if name:
                name_hit = next((cid for cid, c in db_names.get(name, []) if c == city or not c or not city), None)

            # Правило то же, что и для базы: название совпало, а город совпал или у одной из строк не указан.
            batch_name = next(
                (no for c, no in self.seen_names.get(name, []) if no != row.row_no and (c == city or not c or not city)),
                None,
            ) if name else None

            if site and site in db_sites:
                status, kind, company_id = ROW_DUPLICATE, MATCH_WEBSITE, db_sites[site]
            elif site and earlier(self.seen_sites, site, row):
                status, kind, row_no = ROW_DUPLICATE, MATCH_BATCH_WEBSITE, self.seen_sites[site]
            elif link_hit:
                status, kind, company_id = ROW_DUPLICATE, MATCH_LINK, db_links[link_hit]
            elif batch_link:
                status, kind, row_no = ROW_DUPLICATE, MATCH_BATCH_LINK, batch_link
            elif name_hit:
                status, kind, company_id = ROW_DUPLICATE, MATCH_NAME_CITY, name_hit
            elif batch_name:
                status, kind, row_no = ROW_DUPLICATE, MATCH_BATCH_NAME, batch_name
            elif row.raw_hash in excluded_before:
                status, kind = ROW_DUPLICATE, MATCH_EXCLUDED
            elif not row.name.strip() or not (site or row.phone or row.email or row.messenger_url):
                status = ROW_REVIEW

            row.status, row.match_kind, row.match_company_id, row.match_row_no = status, kind, company_id, row_no
            if row.is_hard_duplicate:
                row.force_create = False

            if row.will_create:
                if site:
                    self.seen_sites.setdefault(site, row.row_no)
                for x in link_keys:
                    self.seen_links.setdefault(x, row.row_no)
                if name and (city, row.row_no) not in self.seen_names.setdefault(name, []):
                    self.seen_names[name].append((city, row.row_no))


def recompute(db: Session, batch_id: int) -> None:
    """Пересчитывает статусы всех строк пакета по текущему состоянию базы (для предпросмотра)."""
    rows = list(db.scalars(select(ImportRow).where(ImportRow.batch_id == batch_id).order_by(ImportRow.row_no)))
    user_id = db.scalar(select(ImportBatch.user_id).where(ImportBatch.id == batch_id))
    _Matcher(db, batch_id, user_id).match(rows)
    db.flush()


# --- Правки в предпросмотре -----------------------------------------------------------------


def update_rows(db: Session, batch: ImportBatch, edits: dict[int, dict]) -> None:
    """edits: {row_id: {поле: значение, 'excluded': bool, 'force_create': bool}}."""
    if batch.status != BATCH_PREVIEW:
        raise ImportError_("Этот импорт уже подтверждён — строки изменить нельзя.")
    if edits:
        rows = db.scalars(select(ImportRow).where(ImportRow.batch_id == batch.id, ImportRow.id.in_(list(edits))))
        for row in rows:
            data = edits[row.id]
            for fld in EDITABLE:
                if fld in data:
                    setattr(row, fld, (data[fld] or "").strip())
            row.excluded = bool(data.get("excluded"))
            row.force_create = bool(data.get("force_create"))
    recompute(db, batch.id)
    db.commit()


def bulk_exclude(db: Session, batch: ImportBatch, status: str, excluded: bool) -> None:
    if batch.status != BATCH_PREVIEW:
        raise ImportError_("Этот импорт уже подтверждён — строки изменить нельзя.")
    db.execute(
        update(ImportRow).where(ImportRow.batch_id == batch.id, ImportRow.status == status).values(excluded=excluded)
    )
    db.expire_all()
    recompute(db, batch.id)
    db.commit()


def summary(db: Session, batch_id: int) -> dict:
    """Счётчики для предпросмотра: сколько строк в каждом статусе и что произойдёт при подтверждении."""
    rows = db.execute(
        select(ImportRow.status, ImportRow.excluded, ImportRow.force_create, ImportRow.match_kind, func.count())
        .where(ImportRow.batch_id == batch_id)
        .group_by(ImportRow.status, ImportRow.excluded, ImportRow.force_create, ImportRow.match_kind)
    ).all()
    out = {"total": 0, ROW_NEW: 0, ROW_DUPLICATE: 0, ROW_REVIEW: 0, "excluded": 0, "will_create": 0, "will_review": 0, "will_skip": 0}
    for status, excluded, force, kind, n in rows:
        out["total"] += n
        out[status] += n
        if excluded:
            out["excluded"] += n
        elif status == ROW_DUPLICATE and not force:
            out["will_skip"] += n
        elif status == ROW_REVIEW:
            out["will_review"] += n
        else:
            out["will_create"] += n
    return out


# --- Подтверждение ------------------------------------------------------------------------


def _company_name(row: ImportRow) -> str:
    if row.name.strip():
        return row.name.strip()
    site = normalize_website(row.website)
    if site:
        return display_host(site)
    links = row.links
    if links:
        return links[0][:300]
    return row.phone or row.email or "Без названия"


CONFIRM_CHUNK = 250


def _lock_batch(db: Session, batch_id: int, allowed: tuple[str, ...]) -> bool:
    """Первая операция транзакции — запись. SQLite сразу берёт блокировку записи, поэтому
        всё, что транзакция прочитает дальше, уже не изменится до её конца."""
    if db.get_bind().dialect.name == "postgresql":
        # В PostgreSQL ту же очередь импортов даёт рекомендательная блокировка на время транзакции.
        db.execute(sql_text("SELECT pg_advisory_xact_lock(7461001)"))
    result = db.execute(
        update(ImportBatch)
        .where(ImportBatch.id == batch_id, ImportBatch.status.in_(allowed))
        .values(status=BATCH_CONFIRMING)
    )
    return result.rowcount == 1


def _create_chunk(db: Session, batch: ImportBatch, rows: list[ImportRow], matcher: _Matcher, user_id: int,
                  pipeline: Pipeline | None, owner_id: int | None, source: str) -> None:
    matcher.match(rows)  # перепроверка совпадений внутри транзакции записи
    fresh: list[tuple[ImportRow, Company, bool]] = []
    for row in rows:
        if row.excluded:
            row.result = "excluded"
            continue
        if not row.will_create:
            row.result, row.company_id = "skipped", row.match_company_id
            continue
        needs_review = row.status != ROW_NEW
        company = Company(
            name=_company_name(row)[:300],
            website=row.website or None,
            website_norm=normalize_website(row.website),
            city=row.city or None,
            industry=row.industry or None,
            phone=row.phone or None,
            email=row.email or None,
            messenger_url=row.messenger_url or None,
            note=row.note or None,
            source=source[:300],
            source_fragment=row.raw,
            status=COMPANY_REVIEW if needs_review else COMPANY_OK,
            import_batch_id=batch.id,
            created_by_id=user_id,
        )
        company.name_norm = normalize_name(company.name)
        company.city_norm = normalize_city(company.city)
        for url in row.links:
            kind, _ = classify_url(url)
            company.links.append(
                CompanyLink(url=url, url_norm=normalize_link(url),
                            kind=kind if kind in ("social", "map", "catalog", "messenger") else "other")
            )
        db.add(company)
        fresh.append((row, company, needs_review))
    db.flush()

    deals: dict[int, Deal] = {}
    if pipeline is not None:
        for _row, company, _ in fresh:
            deals[company.id] = Deal(
                company_id=company.id, pipeline_id=pipeline.id, stage_id=pipeline.first_stage.id, owner_id=owner_id
            )
        db.add_all(deals.values())
        db.flush()
    for row, company, needs_review in fresh:
        deal = deals.get(company.id)
        db.add(
            Activity(
                company_id=company.id, deal_id=deal.id if deal else None, user_id=user_id, owner_id=owner_id,
                kind="import",
                body=f"Карточка создана: {source}" + (f". Сделка в воронке «{pipeline.name}»" if pipeline else ""),
                to_stage_id=pipeline.first_stage.id if pipeline else None,
            )
        )
        row.company_id = company.id
        row.result = "review" if needs_review else "created"


def resolve_target(db: Session, user: User, pipeline_id: int | None, owner_id: int | None) -> tuple[int | None, int | None]:
    """В какую воронку и кому попадут сделки из загрузки пользователя.

    Владелец выбирает свободно (может не создавать сделки и не назначать ответственного).
    Менеджер всегда получает сделки на себя. Руководитель группы — на себя или сотрудника группы.
    У всех, кроме владельца, сделка создаётся обязательно: иначе компания «повисла» бы без хозяина.
    """
    if user.is_owner:
        return pipeline_id, owner_id
    pipeline = db.get(Pipeline, pipeline_id) if pipeline_id else None
    if pipeline is None or pipeline.is_archived:
        pipeline = db.scalar(
            select(Pipeline).where(Pipeline.is_archived.is_(False)).order_by(Pipeline.is_default.desc(), Pipeline.id)
        )
    if pipeline is None:
        raise ImportError_("В CRM нет ни одной воронки — обратитесь к владельцу.")
    allowed = {u.id for u in assignable_users(db, user)}
    return pipeline.id, owner_id if owner_id in allowed else user.id


def confirm(db: Session, batch_id: int, user: User, pipeline_id: int | None, owner_id: int | None,
            resume: bool = False) -> ImportBatch:
    """Создаёт карточки короткими транзакциями по CONFIRM_CHUNK строк.

    Каждая транзакция начинается с записи (блокировка), затем заново ищет совпадения для своих
    строк и только после этого создаёт карточки — параллельные импорты не могут создать дубль.
    Уникальный индекс по нормализованному сайту — вторая линия защиты (она же работает в PostgreSQL):
    при его срабатывании порция откатывается и проверяется заново.
    Прерванный импорт можно продолжить (resume=True): готовые строки повторно не обрабатываются.
    """
    user_id = user.id
    db.rollback()
    if not _lock_batch(db, batch_id, (BATCH_PREVIEW, BATCH_CONFIRMING) if resume else (BATCH_PREVIEW,)):
        db.rollback()
        raise ImportError_("Этот импорт уже подтверждён или отменён.")
    batch = db.get(ImportBatch, batch_id)
    if resume:
        pipeline_id, owner_id = batch.pipeline_id, batch.deal_owner_id
    pipeline = db.get(Pipeline, pipeline_id) if pipeline_id else None
    if pipeline_id and (pipeline is None or pipeline.is_archived or not pipeline.stages):
        db.rollback()
        raise ImportError_("Выбранная воронка недоступна.")
    owner = db.get(User, owner_id) if owner_id else None
    if owner_id and (owner is None or not owner.is_active):
        db.rollback()
        raise ImportError_("Выбранный ответственный недоступен.")
    batch.pipeline_id = pipeline.id if pipeline else None
    batch.deal_owner_id = owner.id if owner else None
    batch_user_id = batch.user_id
    source = f"Импорт №{batch.id} · {batch.source_label}"
    row_ids = list(
        db.scalars(
            select(ImportRow.id).where(ImportRow.batch_id == batch_id, ImportRow.result.is_(None)).order_by(ImportRow.row_no)
        )
    )
    db.commit()

    matcher = _Matcher(db, batch_id, batch_user_id)
    for chunk in _chunks(row_ids, CONFIRM_CHUNK):
        for attempt in range(3):
            try:
                _lock_batch(db, batch_id, (BATCH_CONFIRMING,))
                rows = list(db.scalars(select(ImportRow).where(ImportRow.id.in_(chunk)).order_by(ImportRow.row_no)))
                pipeline = db.get(Pipeline, pipeline_id) if pipeline_id else None
                _create_chunk(db, db.get(ImportBatch, batch_id), rows, matcher, user_id, pipeline, owner_id, source)
                db.commit()
                break
            except IntegrityError:
                db.rollback()
                if attempt == 2:
                    raise

    _lock_batch(db, batch_id, (BATCH_CONFIRMING,))
    counts = dict(
        db.execute(select(ImportRow.result, func.count()).where(ImportRow.batch_id == batch_id).group_by(ImportRow.result)).all()
    )
    batch = db.get(ImportBatch, batch_id)
    batch.status = BATCH_CONFIRMED
    batch.created_count, batch.review_count = counts.get("created", 0), counts.get("review", 0)
    batch.skipped_count, batch.excluded_count = counts.get("skipped", 0), counts.get("excluded", 0)
    batch.confirmed_at = utcnow()
    db.commit()
    return batch


def cancel(db: Session, batch: ImportBatch) -> None:
    if batch.status in (BATCH_PREVIEW, BATCH_MAPPING):
        batch.status = "cancelled"
        db.commit()
