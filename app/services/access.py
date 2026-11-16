"""Права доступа. Проверяются на сервере для каждого запроса.

Руководитель видит всё. Менеджер — только сделки, где он ответственный,
и карточки компаний, у которых есть такая сделка.
"""
from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from ..models import Company, Deal, Task, User


class NotFound(Exception):
    """Объекта нет или он недоступен пользователю (наружу отдаётся одинаково — 404)."""


class Forbidden(Exception):
    """Действие доступно только руководителю."""


def require_head(user: User) -> None:
    if not user.is_head:
        raise Forbidden()


def scope_deals(stmt: Select, user: User) -> Select:
    return stmt if user.is_head else stmt.where(Deal.owner_id == user.id)


def company_visible(user: User):
    """Условие SQL: компания видна пользователю."""
    return exists(select(Deal.id).where(Deal.company_id == Company.id, Deal.owner_id == user.id))


def scope_companies(stmt: Select, user: User) -> Select:
    return stmt if user.is_head else stmt.where(company_visible(user))


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
