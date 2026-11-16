"""Сделки: этапы, ответственные, следующий шаг, касания, задачи.

Каждое изменение сделки проверяет номер версии: если сделку успел изменить кто-то другой,
операция отменяется и пользователь видит актуальное состояние.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from ..models import (
    MANUAL_ACTIVITY_KINDS,
    STAGE_LOST,
    Activity,
    Company,
    Deal,
    Pipeline,
    Stage,
    Task,
    User,
    utcnow,
)
from .access import Forbidden


class Conflict(Exception):
    """Сделку одновременно изменил другой пользователь."""

    def __init__(self, deal_id: int):
        super().__init__("conflict")
        self.deal_id = deal_id


class Invalid(Exception):
    """Ошибка ввода с текстом для пользователя."""


def _check_version(deal: Deal, expected: int | None) -> None:
    if expected is not None and deal.version != expected:
        raise Conflict(deal.id)


def _commit(db: Session, deal_id: int) -> None:
    try:
        db.commit()
    except StaleDataError:
        db.rollback()
        raise Conflict(deal_id) from None


def _log(db: Session, deal: Deal, user: User, kind: str, body: str, **extra) -> Activity:
    act = Activity(
        company_id=deal.company_id, deal_id=deal.id, user_id=user.id,
        owner_id=deal.owner_id or user.id, kind=kind, body=body, **extra,
    )
    db.add(act)
    return act


def create_deal(db: Session, company: Company, pipeline: Pipeline, owner: User | None, user: User) -> Deal:
    if pipeline.is_archived or not pipeline.stages:
        raise Invalid("Воронка недоступна.")
    stage = pipeline.first_stage
    deal = Deal(company_id=company.id, pipeline_id=pipeline.id, stage_id=stage.id, owner_id=owner.id if owner else None)
    db.add(deal)
    db.flush()
    _log(db, deal, user, "system", f"Сделка создана в воронке «{pipeline.name}», этап «{stage.name}»", to_stage_id=stage.id)
    return deal


def _apply_stage(db: Session, deal: Deal, stage_id: int, user: User, lost_reason: str | None) -> bool:
    stage = db.get(Stage, stage_id)
    if stage is None or stage.pipeline_id != deal.pipeline_id:
        raise Invalid("Этот этап не относится к воронке сделки.")
    if stage.id == deal.stage_id:
        return False
    reason = (lost_reason or "").strip()
    if stage.kind == STAGE_LOST and not reason:
        raise Invalid("Укажите причину отказа — без неё сделку нельзя закрыть.")
    old = deal.stage
    deal.stage_id = stage.id
    deal.is_open = not stage.is_closed
    deal.closed_at = utcnow() if stage.is_closed else None
    deal.lost_reason = reason[:500] if stage.kind == STAGE_LOST else None
    body = f"{old.name} → {stage.name}"
    if stage.kind == STAGE_LOST:
        body += f". Причина отказа: {reason}"
    _log(db, deal, user, "stage_change", body, from_stage_id=old.id, to_stage_id=stage.id)
    if stage.is_closed:
        deal.next_step, deal.next_step_due = None, None
    return True


def change_stage(db: Session, deal: Deal, stage_id: int, user: User, expected_version: int | None, lost_reason: str | None = None) -> None:
    _check_version(deal, expected_version)
    _apply_stage(db, deal, stage_id, user, lost_reason)
    _commit(db, deal.id)


def _apply_owner(db: Session, deal: Deal, owner: User | None, user: User) -> bool:
    if (owner.id if owner else None) == deal.owner_id:
        return False
    old_name = deal.owner.name if deal.owner else "не назначен"
    new_name = owner.name if owner else "не назначен"
    deal.owner_id = owner.id if owner else None
    act = _log(db, deal, user, "owner_change", f"{old_name} → {new_name}")
    act.owner_id = owner.id if owner else None
    # Открытые задачи переходят к новому ответственному.
    for task in db.scalars(select(Task).where(Task.deal_id == deal.id, Task.done_at.is_(None))):
        task.assignee_id = owner.id if owner else None
    return True


def change_owner(db: Session, deal: Deal, owner_id: int | None, user: User, expected_version: int | None) -> None:
    if not user.is_head:
        raise Forbidden()
    _check_version(deal, expected_version)
    owner = None
    if owner_id:
        owner = db.get(User, owner_id)
        if owner is None or not owner.is_active:
            raise Invalid("Выбранный сотрудник недоступен.")
    _apply_owner(db, deal, owner, user)
    _commit(db, deal.id)


def set_next_step(db: Session, deal: Deal, text: str, due: date | None, user: User, expected_version: int | None) -> None:
    _check_version(deal, expected_version)
    text = (text or "").strip()[:500]
    if text and due is None:
        raise Invalid("У следующего шага должен быть срок.")
    if (deal.next_step or "") == text and deal.next_step_due == due:
        return
    deal.next_step = text or None
    deal.next_step_due = due if text else None
    if text:
        _log(db, deal, user, "system", f"Следующий шаг: {text} (до {due.strftime('%d.%m.%Y')})")
    else:
        _log(db, deal, user, "system", "Следующий шаг снят")
    _commit(db, deal.id)


def add_task(db: Session, deal: Deal, user: User, title: str, due: date | None, kind: str = "followup") -> Task:
    title = (title or "").strip()[:500] or "Повторный контакт"
    if due is None:
        raise Invalid("У задачи должен быть срок.")
    task = Task(
        deal_id=deal.id, assignee_id=deal.owner_id or user.id, title=title, kind=kind,
        due_date=due, created_by_id=user.id,
    )
    db.add(task)
    _log(db, deal, user, "task", f"Поставлена задача «{title}» на {due.strftime('%d.%m.%Y')}")
    return task


def complete_task(db: Session, task: Task, user: User) -> None:
    if task.done_at is None:
        task.done_at = utcnow()
        _log(db, task.deal, user, "task", f"Задача выполнена: «{task.title}»")
    db.commit()


def log_touch(
    db: Session,
    deal: Deal,
    user: User,
    kind: str,
    body: str,
    *,
    stage_id: int | None = None,
    lost_reason: str | None = None,
    followup_due: date | None = None,
    followup_title: str | None = None,
    expected_version: int | None = None,
) -> None:
    """Одна форма менеджера: что сделал, результат, новый этап и повторный контакт."""
    if kind not in MANUAL_ACTIVITY_KINDS:
        raise Invalid("Неизвестный тип записи.")
    body = (body or "").strip()
    if not body:
        raise Invalid("Опишите результат — запись без текста не сохраняется.")
    if stage_id and stage_id != deal.stage_id:
        _check_version(deal, expected_version)
    _log(db, deal, user, kind, body)
    if stage_id:
        _apply_stage(db, deal, stage_id, user, lost_reason)
    if followup_due is not None:
        add_task(db, deal, user, followup_title or "Повторный контакт", followup_due)
    _commit(db, deal.id)


def bulk_assign(db: Session, user: User, company_ids: list[int], manager_ids: list[int]) -> dict:
    """Массовое назначение: открытые сделки выбранных компаний делятся между менеджерами по кругу.

    Если у компании ещё нет сделки, она создаётся в основной воронке.
    """
    if not user.is_head:
        raise Forbidden()
    managers = [m for m in db.scalars(select(User).where(User.id.in_(manager_ids), User.is_active.is_(True)).order_by(User.id))]
    if not managers:
        raise Invalid("Выберите хотя бы одного ответственного.")
    if not company_ids:
        raise Invalid("Не выбрано ни одной компании.")
    default = db.scalar(select(Pipeline).where(Pipeline.is_archived.is_(False)).order_by(Pipeline.is_default.desc(), Pipeline.id))
    assigned = created = turn = 0
    ids = sorted(set(company_ids))

    def process(chunk: list[int], turn: int) -> tuple[int, int, int]:
        done = made = 0
        deals_by_company: dict[int, list[Deal]] = {}
        has_any: set[int] = set()
        for deal in db.scalars(
            select(Deal).where(Deal.company_id.in_(chunk)).order_by(Deal.id).execution_options(populate_existing=True)
        ):
            has_any.add(deal.company_id)
            if deal.is_open:
                deals_by_company.setdefault(deal.company_id, []).append(deal)
        companies = {c.id: c for c in db.scalars(select(Company).where(Company.id.in_(chunk)))}
        for cid in chunk:
            company = companies.get(cid)
            if company is None:
                continue
            manager = managers[turn % len(managers)]
            deals = deals_by_company.get(cid, [])
            if not deals and cid not in has_any and default is not None:
                create_deal(db, company, default, manager, user)
                made += 1
                done += 1
                turn += 1
                continue
            changed = False
            for deal in deals:
                changed = _apply_owner(db, deal, manager, user) or changed
            if deals:
                turn += 1
                done += 1 if changed else 0
        db.commit()
        return done, made, turn

    # Короткие транзакции по 400 компаний; при столкновении с чужой правкой порция повторяется.
    for start in range(0, len(ids), 400):
        chunk = ids[start : start + 400]
        for attempt in range(3):
            try:
                done, made, turn = process(chunk, turn)
                assigned, created = assigned + done, created + made
                break
            except StaleDataError:
                db.rollback()
                if attempt == 2:
                    raise Conflict(0) from None
    return {"assigned": assigned, "created": created, "managers": [m.name for m in managers]}
