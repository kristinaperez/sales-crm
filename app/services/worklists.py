"""«Мои задачи»: Сегодня, Просрочено, Без следующего шага; доска воронки; отчёт руководителя."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, selectinload

from .. import config
from ..models import METRICS, ROLE_MANAGER, Activity, Deal, Pipeline, Stage, Task, User


def today() -> date:
    return datetime.now(config.APP_TZ).date()


def _scope(stmt, user: User, owner_id: int | None):
    """Менеджер видит только своё; руководитель — всё или одного менеджера."""
    if not user.is_head:
        return stmt.where(Deal.owner_id == user.id)
    if owner_id:
        return stmt.where(Deal.owner_id == owner_id)
    return stmt


_DEAL_LOAD = (selectinload(Deal.company), selectinload(Deal.stage), selectinload(Deal.owner))


def task_lists(db: Session, user: User, owner_id: int | None = None) -> dict:
    now = today()
    open_task = exists(select(Task.id).where(Task.deal_id == Deal.id, Task.done_at.is_(None)))

    def tasks(cond):
        stmt = (
            select(Task).join(Deal, Task.deal_id == Deal.id)
            .where(Task.done_at.is_(None), Deal.is_open.is_(True), cond)
            .options(selectinload(Task.deal).options(*_DEAL_LOAD)).order_by(Task.due_date, Task.id)
        )
        return list(db.scalars(_scope(stmt, user, owner_id)))

    def steps(cond):
        stmt = select(Deal).where(Deal.is_open.is_(True), cond).options(*_DEAL_LOAD).order_by(Deal.next_step_due, Deal.id)
        return list(db.scalars(_scope(stmt, user, owner_id)))

    def merge(task_rows, deal_rows):
        items = [{"type": "task", "task": t, "deal": t.deal, "title": t.title, "due": t.due_date} for t in task_rows]
        items += [{"type": "step", "task": None, "deal": d, "title": d.next_step, "due": d.next_step_due} for d in deal_rows]
        return sorted(items, key=lambda x: (x["due"], x["deal"].id))

    no_next = list(
        db.scalars(
            _scope(
                select(Deal).where(Deal.is_open.is_(True), Deal.next_step_due.is_(None), ~open_task)
                .options(*_DEAL_LOAD).order_by(Deal.updated_at).limit(500),
                user, owner_id,
            )
        )
    )
    return {
        "today": merge(tasks(Task.due_date == now), steps(Deal.next_step_due == now)),
        "overdue": merge(tasks(Task.due_date < now), steps(Deal.next_step_due < now)),
        "no_next": [{"type": "none", "task": None, "deal": d, "title": "", "due": None} for d in no_next],
        "date": now,
    }


def next_tasks(db: Session, deal_ids: list[int]) -> dict[int, Task]:
    """Ближайшая открытая задача по каждой сделке — чтобы в списках был виден план."""
    out: dict[int, Task] = {}
    for start in range(0, len(deal_ids), 400):
        stmt = (
            select(Task).where(Task.deal_id.in_(deal_ids[start : start + 400]), Task.done_at.is_(None))
            .order_by(Task.due_date.desc(), Task.id.desc())
        )
        for task in db.scalars(stmt):
            out[task.deal_id] = task  # последним остаётся самый ранний срок
    return out


def board(db: Session, user: User, pipeline: Pipeline, owner_id: int | None = None) -> list[dict]:
    """Колонки воронки: этап, число сделок и первые карточки."""
    counts_stmt = select(Deal.stage_id, func.count()).where(Deal.pipeline_id == pipeline.id).group_by(Deal.stage_id)
    counts = dict(db.execute(_scope(counts_stmt, user, owner_id)).all())
    columns = []
    for stage in pipeline.stages:
        stmt = (
            select(Deal).where(Deal.stage_id == stage.id).options(*_DEAL_LOAD)
            .order_by(Deal.next_step_due.is_(None), Deal.next_step_due, Deal.updated_at.desc())
            .limit(config.KANBAN_COLUMN_LIMIT)
        )
        columns.append({"stage": stage, "count": counts.get(stage.id, 0), "deals": list(db.scalars(_scope(stmt, user, owner_id)))})
    tasks = next_tasks(db, [d.id for col in columns for d in col["deals"]])
    for col in columns:
        col["tasks"] = tasks
    return columns


def _utc_bounds(date_from: date, date_to: date) -> tuple[datetime, datetime]:
    start = datetime.combine(date_from, time.min, tzinfo=config.APP_TZ)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=config.APP_TZ)
    return start.astimezone(timezone.utc).replace(tzinfo=None), end.astimezone(timezone.utc).replace(tzinfo=None)


def manager_report(db: Session, date_from: date, date_to: date) -> dict:
    """Сколько сделок за период перешло на этапы «первый контакт», «ответ», «демо», «оплата».

    Считаются уникальные сделки; событие относится к ответственному на момент перехода.
    """
    start, end = _utc_bounds(date_from, date_to)
    rows = db.execute(
        select(Activity.owner_id, Stage.metric_key, func.count(func.distinct(Activity.deal_id)))
        .join(Stage, Activity.to_stage_id == Stage.id)
        .where(
            Activity.kind == "stage_change", Stage.metric_key.is_not(None),
            Activity.created_at >= start, Activity.created_at < end,
        )
        .group_by(Activity.owner_id, Stage.metric_key)
    ).all()
    data: dict[int | None, dict[str, int]] = {}
    for owner_id, metric, n in rows:
        data.setdefault(owner_id, {})[metric] = n
    users = list(db.scalars(select(User).order_by(User.role.desc(), User.name)))
    lines = []
    for u in users:
        values = data.get(u.id, {})
        if u.role != ROLE_MANAGER and not values:
            continue
        if not u.is_active and not values:
            continue
        lines.append({"user": u, **{m: values.get(m, 0) for m in METRICS}})
    totals = {m: sum(line[m] for line in lines) for m in METRICS}
    return {"lines": lines, "totals": totals, "date_from": date_from, "date_to": date_to}
