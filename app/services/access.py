"""Права доступа. Проверяются на сервере для каждого запроса.

Владелец видит всё. Руководитель группы — сделки сотрудников своей группы и свои.
Менеджер — только сделки, где он ответственный. Карточка компании видна тому, кому видна
хотя бы одна её сделка.
"""
from __future__ import annotations

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from ..models import ROLE_LEAD, ROLE_MANAGER, Company, Deal, Payment, Task, User


class NotFound(Exception):
    """Объекта нет или он недоступен пользователю (наружу отдаётся одинаково — 404)."""


class Forbidden(Exception):
    """У пользователя нет прав на это действие."""


def require_owner(user: User) -> None:
    if not user.is_owner:
        raise Forbidden()


def require_boss(user: User) -> None:
    if not user.is_boss:
        raise Forbidden()


def deal_condition(user: User):
    """Условие SQL «сделка видна пользователю» или None, если видны все."""
    if user.is_owner:
        return None
    if user.is_lead and user.team_id:
        team = select(User.id).where(User.team_id == user.team_id)
        return or_(Deal.owner_id == user.id, Deal.owner_id.in_(team))
    return Deal.owner_id == user.id


def scope_deals(stmt: Select, user: User) -> Select:
    cond = deal_condition(user)
    return stmt if cond is None else stmt.where(cond)


def company_visible(user: User):
    """Условие SQL: у компании есть сделка, видимая пользователю."""
    return exists(select(Deal.id).where(Deal.company_id == Company.id, deal_condition(user)))


def scope_companies(stmt: Select, user: User) -> Select:
    return stmt if user.is_owner else stmt.where(company_visible(user))


def visible_company_ids(db: Session, user: User, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    return set(db.scalars(scope_companies(select(Company.id).where(Company.id.in_(ids)), user)))


def assignable_users(db: Session, user: User) -> list[User]:
    """Кому пользователь может назначать сделки: владелец — всем продающим, руководитель — своей группе."""
    stmt = select(User).where(User.is_active.is_(True), User.role.in_((ROLE_MANAGER, ROLE_LEAD))).order_by(User.name)
    if user.is_owner:
        return list(db.scalars(stmt))
    if user.is_lead:
        cond = User.id == user.id
        if user.team_id:
            cond = or_(cond, User.team_id == user.team_id)
        return list(db.scalars(stmt.where(cond)))
    return []


def get_deal(db: Session, user: User, deal_id: int) -> Deal:
    deal = db.scalar(scope_deals(select(Deal).where(Deal.id == deal_id), user))
    if deal is None:
        raise NotFound()
    return deal


def get_company(db: Session, user: User, company_id: int) -> Company:
    company = db.scalar(scope_companies(select(Company).where(Company.id == company_id), user))
    if company is None:
        raise NotFound()
    return company


def get_task(db: Session, user: User, task_id: int) -> Task:
    stmt = select(Task).join(Deal, Task.deal_id == Deal.id).where(Task.id == task_id)
    task = db.scalar(scope_deals(stmt, user))
    if task is None:
        raise NotFound()
    return task


def get_payment(db: Session, user: User, payment_id: int) -> Payment:
    stmt = select(Payment).join(Deal, Payment.deal_id == Deal.id).where(Payment.id == payment_id)
    payment = db.scalar(scope_deals(stmt, user))
    if payment is None:
        raise NotFound()
    return payment
