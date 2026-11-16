"""Рабочие экраны: воронка, «Мои задачи», отчёт руководителя."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..models import ROLE_MANAGER, Pipeline, User
from ..services import worklists
from .common import current_user, head_user, redirect, render, to_date, to_int

router = APIRouter()


def _managers(db: Session) -> list[User]:
    return list(db.scalars(select(User).where(User.is_active.is_(True), User.role == ROLE_MANAGER).order_by(User.name)))


@router.get("/")
def index(user: User = Depends(current_user)):
    return redirect("/pipeline" if user.is_head else "/tasks")


@router.get("/pipeline")
def pipeline_board(request: Request, pipeline_id: int | None = None, owner: str = "",
                   user: User = Depends(current_user), db: Session = Depends(get_db)):
    pipelines = list(db.scalars(
        select(Pipeline).where(Pipeline.is_archived.is_(False)).options(selectinload(Pipeline.stages))
        .order_by(Pipeline.is_default.desc(), Pipeline.id)
    ))
    pipeline = next((p for p in pipelines if p.id == pipeline_id), pipelines[0] if pipelines else None)
    owner_id = to_int(owner) if user.is_head else None
    columns = worklists.board(db, user, pipeline, owner_id) if pipeline else []
    return render(request, "pipeline.html", {
        "pipelines": pipelines, "pipeline": pipeline, "columns": columns, "owner": owner,
        "managers": _managers(db) if user.is_head else [],
    })


@router.get("/tasks")
def my_tasks(request: Request, tab: str = "today", owner: str = "",
             user: User = Depends(current_user), db: Session = Depends(get_db)):
    owner_id = to_int(owner) if user.is_head else None
    lists = worklists.task_lists(db, user, owner_id)
    if tab not in ("today", "overdue", "no_next"):
        tab = "today"
    return render(request, "tasks.html", {
        "lists": lists, "tab": tab, "items": lists[tab], "owner": owner,
        "managers": _managers(db) if user.is_head else [],
    })


@router.get("/reports")
def reports(request: Request, date_from: str = "", date_to: str = "",
            user: User = Depends(head_user), db: Session = Depends(get_db)):
    now = worklists.today()
    start = to_date(date_from) or date(now.year, now.month, 1)
    end = to_date(date_to) or now
    if end < start:
        start, end = end, start
    return render(request, "reports.html", {"report": worklists.manager_report(db, start, end)})
