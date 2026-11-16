"""Компании и контакты: список с фильтрами, карточка, правки, экспорт CSV."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from .. import config
from ..models import COMPANY_OK, COMPANY_REVIEW, Activity, Company, CompanyLink, Contact, Deal, User
from .access import deal_condition, scope_companies, scope_deals
from .normalize import classify_url, normalize_city, normalize_link, normalize_name


class Invalid(Exception):
    def __init__(self, message: str, company_id: int | None = None):
        super().__init__(message)
        self.company_id = company_id


@dataclass
class Filters:
    q: str = ""
    city: str = ""
    industry: str = ""
    stage_id: int | None = None
    owner: str = ""  # id менеджера, "none" — без ответственного, "" — все
    status: str = ""
    no_deal: bool = False

    def as_query(self) -> dict:
        out = {
            "q": self.q, "city": self.city, "industry": self.industry,
            "stage_id": self.stage_id or "", "owner": self.owner, "status": self.status,
            "no_deal": "1" if self.no_deal else "",
        }
        return {k: v for k, v in out.items() if v not in ("", None)}


def _filtered(user: User, f: Filters):
    stmt = scope_companies(select(Company), user)
    if f.q:
        like = f"%{f.q.strip().lower()}%"
        raw = f"%{f.q.strip()}%"
        stmt = stmt.where(
            or_(
                Company.name_norm.like(f"%{normalize_name(f.q)}%") if normalize_name(f.q) else Company.name.ilike(raw),
                func.lower(Company.website).like(like),
                Company.phone.like(raw),
                func.lower(Company.email).like(like),
                Company.note.ilike(raw),
            )
        )
    if f.city:
        stmt = stmt.where(Company.city_norm == normalize_city(f.city))
    if f.industry:
        stmt = stmt.where(Company.industry == f.industry)
    if f.status in (COMPANY_OK, COMPANY_REVIEW):
        stmt = stmt.where(Company.status == f.status)
    deal_cond = [Deal.company_id == Company.id]
    use_deal = False
    if f.stage_id:
        deal_cond.append(Deal.stage_id == f.stage_id)
        use_deal = True
    if f.owner == "none":
        deal_cond.append(Deal.owner_id.is_(None))
        use_deal = True
    elif f.owner.isdigit():
        deal_cond.append(Deal.owner_id == int(f.owner))
        use_deal = True
    scope = deal_condition(user)
    if scope is not None:
        deal_cond.append(scope)
    if use_deal:
        stmt = stmt.where(exists(select(Deal.id).where(*deal_cond)))
    if f.no_deal and user.is_owner:
        stmt = stmt.where(~exists(select(Deal.id).where(Deal.company_id == Company.id)))
    return stmt


def primary_deals(db: Session, user: User, company_ids: list[int]) -> dict[int, Deal]:
    """Для списка: одна «главная» сделка на компанию (открытая и самая свежая), видимая пользователю."""
    if not company_ids:
        return {}
    stmt = (
        select(Deal)
        .where(Deal.company_id.in_(company_ids))
        .options(selectinload(Deal.stage), selectinload(Deal.owner), selectinload(Deal.pipeline))
        .order_by(Deal.is_open, Deal.id)
    )
    stmt = scope_deals(stmt, user)
    out: dict[int, Deal] = {}
    for deal in db.scalars(stmt):
        out[deal.company_id] = deal  # позже идут открытые и более новые
    return out


def list_companies(db: Session, user: User, f: Filters, page: int) -> dict:
    base = _filtered(user, f)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    page = max(1, page)
    items = list(
        db.scalars(base.order_by(Company.id.desc()).limit(config.PAGE_SIZE).offset((page - 1) * config.PAGE_SIZE))
    )
    deals = primary_deals(db, user, [c.id for c in items])
    from .worklists import next_tasks

    return {
        "items": items,
        "deals": deals,
        "tasks": next_tasks(db, [d.id for d in deals.values()]),
        "total": total,
        "page": page,
        "pages": max(1, -(-total // config.PAGE_SIZE)),
    }


def filtered_ids(db: Session, user: User, f: Filters) -> list[int]:
    return list(db.scalars(_filtered(user, f).with_only_columns(Company.id)))


def filter_options(db: Session, user: User) -> dict:
    cities = db.execute(
        scope_companies(select(func.min(Company.city), func.count()).where(Company.city.is_not(None), Company.city != ""), user)
        .group_by(Company.city_norm).order_by(func.count().desc()).limit(300)
    ).all()
    industries = db.execute(
        scope_companies(select(Company.industry, func.count()).where(Company.industry.is_not(None), Company.industry != ""), user)
        .group_by(Company.industry).order_by(func.count().desc()).limit(300)
    ).all()
    return {"cities": [c for c, _ in cities], "industries": [i for i, _ in industries]}


def apply_fields(db: Session, company: Company, data: dict) -> None:
    """Проверяет и записывает поля карточки. Сайт остаётся уникальным ключом."""
    name = (data.get("name") or "").strip()
    if not name:
        raise Invalid("У компании должно быть название.")
    website = (data.get("website") or "").strip()
    norm = None
    if website:
        kind, value = classify_url(website)
        if kind == "invalid":
            raise Invalid("Адрес сайта не похож на ссылку. Пример: example.ru")
        if kind != "website":
            raise Invalid("Это ссылка на соцсеть, карту, каталог или мессенджер. Сохраните её в блоке «Ссылки», а не как сайт.")
        norm = value
        other = db.scalar(select(Company).where(Company.website_norm == norm, Company.id != (company.id or 0)))
        if other is not None:
            raise Invalid(f"Компания с таким сайтом уже есть: «{other.name}».", other.id)
    company.name = name[:300]
    company.name_norm = normalize_name(name)
    company.website = website[:500] or None
    company.website_norm = norm
    company.city = (data.get("city") or "").strip()[:200] or None
    company.city_norm = normalize_city(company.city)
    company.industry = (data.get("industry") or "").strip()[:200] or None
    company.phone = (data.get("phone") or "").strip()[:100] or None
    company.email = (data.get("email") or "").strip()[:200] or None
    company.messenger_url = (data.get("messenger_url") or "").strip()[:500] or None
    company.note = (data.get("note") or "").strip() or None
    if "source" in data:
        company.source = (data.get("source") or "").strip()[:300] or None


def update_company(db: Session, company: Company, data: dict, user: User) -> None:
    apply_fields(db, company, data)
    if data.get("mark_checked"):
        company.status = COMPANY_OK
    db.add(Activity(company_id=company.id, user_id=user.id, kind="system", body="Карточка компании изменена"))
    db.commit()


def create_company(db: Session, data: dict, user: User) -> Company:
    company = Company(created_by_id=user.id, status=COMPANY_OK, name="")
    apply_fields(db, company, {**data, "source": data.get("source") or "Добавлена вручную"})
    db.add(company)
    db.flush()
    db.add(Activity(company_id=company.id, user_id=user.id, kind="system", body="Карточка создана вручную"))
    return company


def add_link(db: Session, company: Company, url: str) -> None:
    url = (url or "").strip()
    kind, _ = classify_url(url)
    if kind == "invalid":
        raise Invalid("Это не похоже на ссылку.")
    key = normalize_link(url)
    if any(link.url_norm == key for link in company.links):
        return
    company.links.append(CompanyLink(url=url[:1000], url_norm=key, kind=kind if kind != "website" else "other"))
    db.commit()


def add_contact(db: Session, company: Company, data: dict) -> Contact:
    name = (data.get("full_name") or "").strip()
    if not name:
        raise Invalid("Укажите имя контактного лица.")
    contact = Contact(
        company_id=company.id, full_name=name[:200],
        position=(data.get("position") or "").strip()[:200] or None,
        phone=(data.get("phone") or "").strip()[:100] or None,
        email=(data.get("email") or "").strip()[:200] or None,
        messenger=(data.get("messenger") or "").strip()[:300] or None,
        note=(data.get("note") or "").strip() or None,
    )
    db.add(contact)
    db.commit()
    return contact


EXPORT_HEADER = [
    "ID", "Название", "Сайт", "Город", "Отрасль", "Телефон", "Email", "Мессенджер", "Ссылки",
    "Источник", "Статус карточки", "Заметка", "Воронка", "Этап", "Ответственный",
    "Следующий шаг", "Срок шага", "Причина отказа", "Создана",
]


def export_csv(db: Session, user: User, f: Filters) -> str:
    """CSV для таблиц: «;» и BOM — открывается в Excel и импортируется в Google Таблицы."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(EXPORT_HEADER)
    ids = filtered_ids(db, user, f)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        companies = db.scalars(
            select(Company).where(Company.id.in_(chunk)).options(selectinload(Company.links)).order_by(Company.id)
        ).all()
        deals: dict[int, list[Deal]] = {}
        for deal in db.scalars(
            select(Deal).where(Deal.company_id.in_(chunk))
            .options(selectinload(Deal.stage), selectinload(Deal.owner), selectinload(Deal.pipeline)).order_by(Deal.id)
        ):
            deals.setdefault(deal.company_id, []).append(deal)
        for c in companies:
            base = [
                c.id, c.name, c.website or "", c.city or "", c.industry or "", c.phone or "", c.email or "",
                c.messenger_url or "", " ".join(link.url for link in c.links), c.source or "",
                "Нужно проверить" if c.status == COMPANY_REVIEW else "Проверена", c.note or "",
            ]
            created = c.created_at.strftime("%Y-%m-%d")
            for d in deals.get(c.id) or [None]:
                if d is None:
                    writer.writerow([_safe(x) for x in base + ["", "", "", "", "", "", created]])
                else:
                    writer.writerow(
                        [_safe(x) for x in base + [
                            d.pipeline.name, d.stage.name, d.owner.name if d.owner else "",
                            d.next_step or "", d.next_step_due.isoformat() if d.next_step_due else "",
                            d.lost_reason or "", created,
                        ]]
                    )
    return "﻿" + buf.getvalue()


def _safe(value) -> str:
    """Защита от формул в таблицах: значения, начинающиеся с = + - @, экранируются апострофом."""
    text = str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r") and not text.lstrip("+-").replace(" ", "").replace("(", "").replace(")", "").replace("-", "").isdigit():
        return "'" + text
    return text


def delete_company(db: Session, company: Company, user: User) -> None:
    """Удаление карточки владельцем — вместе со сделками, задачами, оплатами и историей."""
    from sqlalchemy import delete

    from .access import require_owner
    from .payments import audit

    require_owner(user)
    deals = db.scalar(select(func.count()).select_from(Deal).where(Deal.company_id == company.id)) or 0
    audit(db, user, "удаление", "компания", company.id,
          f"Удалена компания «{company.name}» (сайт: {company.website or '—'}), сделок: {deals}")
    db.execute(delete(Company).where(Company.id == company.id))
    db.commit()
    db.expunge_all()


def delete_deal(db: Session, deal: Deal, user: User) -> int:
    """Удаление сделки владельцем — вместе с её задачами, оплатами и историей. Возвращает id компании."""
    from sqlalchemy import delete

    from .access import require_owner
    from .payments import audit

    require_owner(user)
    company_id = deal.company_id
    audit(db, user, "удаление", "сделка", deal.id,
          f"Удалена сделка №{deal.id} компании «{deal.company.name}», этап «{deal.stage.name}», "
          f"ответственный: {deal.owner.name if deal.owner else 'не назначен'}")
    db.execute(delete(Deal).where(Deal.id == deal.id))
    db.commit()
    db.expunge_all()
    return company_id
