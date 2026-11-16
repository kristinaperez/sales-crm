"""Оплаты: внесение, подтверждение, исправление владельцем, рейтинг менеджеров."""
from __future__ import annotations

import re
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..models import (
    PAY_CONFIRMED,
    PAY_PENDING,
    PAY_REJECTED,
    PAYMENT_STATUSES,
    ROLE_LEAD,
    ROLE_MANAGER,
    Activity,
    AuditLog,
    Deal,
    Payment,
    User,
    utcnow,
)
from .access import Forbidden, require_owner, scope_deals
from .deals import Invalid, format_money, record_payment

MAX_AMOUNT_MINOR = 10**12


def parse_amount(raw) -> int | None:
    """«12 500,50» → 1250050 (копейки). Пустая строка → None."""
    text = re.sub(r"[\s ₽$€]", "", str(raw or "")).replace(",", ".")
    if not text:
        return None
    if not re.fullmatch(r"\d+(\.\d{1,2})?", text):
        raise Invalid("Сумма должна быть числом, например 45000 или 45000,50.")
    whole, _, frac = text.partition(".")
    minor = int(whole) * 100 + int((frac + "00")[:2])
    if minor <= 0 or minor > MAX_AMOUNT_MINOR:
        raise Invalid("Сумма должна быть больше нуля.")
    return minor


def audit(db: Session, user: User, action: str, entity: str, entity_id: int | None, summary: str) -> None:
    db.add(AuditLog(user_id=user.id, action=action, entity=entity, entity_id=entity_id, summary=summary))


def _log(db: Session, payment: Payment, user: User, body: str) -> None:
    deal = payment.deal
    db.add(Activity(company_id=deal.company_id, deal_id=deal.id, user_id=user.id,
                    owner_id=payment.credited_to_id, kind="payment", body=body))


def _describe(payment: Payment) -> str:
    who = payment.credited_to.name if payment.credited_to else "никому"
    return (f"{format_money(payment.amount_minor)} от {payment.paid_on.strftime('%d.%m.%Y')}, "
            f"засчитана: {who}, статус: {payment.status_label.lower()}")


def add_payment(db: Session, deal: Deal, user: User, amount_raw, paid_on: date | None, note: str = "") -> Payment:
    payment = record_payment(db, deal, user, parse_amount(amount_raw), paid_on, note)
    db.commit()
    return payment


def can_confirm(user: User, payment: Payment) -> bool:
    """Владелец — любую. Руководитель группы — оплаты своих сотрудников, но не собственные."""
    if user.is_owner:
        return True
    if not user.is_lead or not user.team_id or payment.credited_to_id == user.id:
        return False
    return payment.credited_to is not None and payment.credited_to.team_id == user.team_id


def set_status(db: Session, payment: Payment, user: User, status: str) -> None:
    if status not in (PAY_CONFIRMED, PAY_REJECTED):
        raise Invalid("Неизвестное решение по оплате.")
    if not can_confirm(user, payment):
        raise Forbidden()
    if payment.status != PAY_PENDING and not user.is_owner:
        raise Invalid("По этой оплате решение уже принято. Изменить его может только владелец.")
    if payment.status == status:
        return
    payment.status = status
    payment.confirmed_by_id, payment.confirmed_at = user.id, utcnow()
    verb = "подтверждена" if status == PAY_CONFIRMED else "отклонена"
    _log(db, payment, user, f"Оплата {format_money(payment.amount_minor)} от {payment.paid_on.strftime('%d.%m.%Y')} {verb}")
    db.commit()


def correct(db: Session, payment: Payment, user: User, amount_raw, paid_on: date | None,
            credited_to_id: int | None, status: str, note: str = "") -> None:
    """Исправление оплаты владельцем: сумма, дата, кому засчитана, статус. Пишется в историю и журнал."""
    require_owner(user)
    amount = parse_amount(amount_raw)
    if amount is None:
        raise Invalid("Укажите сумму оплаты.")
    if paid_on is None:
        raise Invalid("Укажите дату оплаты.")
    if status not in PAYMENT_STATUSES:
        raise Invalid("Неизвестный статус оплаты.")
    credited = db.get(User, credited_to_id) if credited_to_id else None
    if credited_to_id and credited is None:
        raise Invalid("Выбранный сотрудник не найден.")
    before = _describe(payment)
    payment.amount_minor, payment.paid_on = amount, paid_on
    payment.credited_to = credited
    payment.note = (note or "").strip()[:500] or None
    if payment.status != status:
        payment.status = status
        payment.confirmed_by_id = user.id if status != PAY_PENDING else None
        payment.confirmed_at = utcnow() if status != PAY_PENDING else None
    after = _describe(payment)
    if before == after:
        db.commit()
        return
    _log(db, payment, user, f"Оплата исправлена. Было: {before}. Стало: {after}")
    db.flush()
    audit(db, user, "исправление", "оплата", payment.id,
          f"Сделка №{payment.deal_id} ({payment.deal.company.name}). Было: {before}. Стало: {after}")
    db.commit()


def delete(db: Session, payment: Payment, user: User) -> None:
    require_owner(user)
    before = _describe(payment)
    _log(db, payment, user, f"Оплата удалена: {before}")
    audit(db, user, "удаление", "оплата", payment.id,
          f"Сделка №{payment.deal_id} ({payment.deal.company.name}). Удалена оплата: {before}")
    db.delete(payment)
    db.commit()


def list_payments(db: Session, user: User, status: str | None, limit: int = 300) -> list[Payment]:
    stmt = (
        select(Payment).join(Deal, Payment.deal_id == Deal.id)
        .options(selectinload(Payment.deal).selectinload(Deal.company), selectinload(Payment.credited_to),
                 selectinload(Payment.confirmed_by))
        .order_by(Payment.paid_on.desc(), Payment.id.desc()).limit(limit)
    )
    if status:
        stmt = stmt.where(Payment.status == status)
    return list(db.scalars(scope_deals(stmt, user)))


def pending_count(db: Session, user: User) -> int:
    stmt = select(func.count()).select_from(Payment).join(Deal, Payment.deal_id == Deal.id).where(Payment.status == PAY_PENDING)
    return db.scalar(scope_deals(stmt, user)) or 0


def month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


TOP_SIZE = 10


def rating(db: Session, viewer: User, year: int, month: int) -> dict:
    """Рейтинг за календарный месяц по сумме подтверждённых оплат (по дате оплаты).

    Топ-10 виден всем, своё место — каждому, полный список — владельцу.
    """
    start, end = month_bounds(year, month)
    totals = {
        uid: (total, count)
        for uid, total, count in db.execute(
            select(Payment.credited_to_id, func.sum(Payment.amount_minor), func.count())
            .where(Payment.status == PAY_CONFIRMED, Payment.paid_on >= start, Payment.paid_on < end,
                   Payment.credited_to_id.is_not(None))
            .group_by(Payment.credited_to_id)
        )
    }
    users = {
        u.id: u
        for u in db.scalars(
            select(User).options(selectinload(User.team))
            .where(User.is_active.is_(True), User.role.in_((ROLE_MANAGER, ROLE_LEAD)))
        )
    }
    missing = [uid for uid in totals if uid not in users]
    if missing:
        for u in db.scalars(select(User).options(selectinload(User.team)).where(User.id.in_(missing))):
            users[u.id] = u
    lines = [
        {"user": u, "total": totals.get(uid, (0, 0))[0], "count": totals.get(uid, (0, 0))[1]}
        for uid, u in users.items()
    ]
    lines.sort(key=lambda x: (-x["total"], -x["count"], x["user"].name))
    for place, line in enumerate(lines, start=1):
        line["place"] = place
    return {
        "year": year, "month": month, "start": start,
        "top": lines[:TOP_SIZE],
        "mine": next((x for x in lines if x["user"].id == viewer.id), None),
        "full": lines if viewer.is_owner else None,
        "participants": len(lines),
        "total": sum(x["total"] for x in lines),
    }
