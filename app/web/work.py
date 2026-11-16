"""Рабочие экраны: воронка, «Мои задачи», оплаты, рейтинг, отчёт, справка."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..models import PAY_PENDING, PAYMENT_STATUSES, Pipeline, User
from ..services import payments as pay_svc
from ..services import worklists
from ..services.access import assignable_users
from .common import boss_user, current_user, redirect, render, to_date, to_int

router = APIRouter()


@router.get("/")
def index(user: User = Depends(current_user)):
    return redirect("/pipeline" if user.is_boss else "/tasks")


@router.get("/pipeline")
def pipeline_board(request: Request, pipeline_id: int | None = None, owner: str = "",
                   user: User = Depends(current_user), db: Session = Depends(get_db)):
    pipelines = list(db.scalars(
        select(Pipeline).where(Pipeline.is_archived.is_(False)).options(selectinload(Pipeline.stages))
        .order_by(Pipeline.is_default.desc(), Pipeline.id)
    ))
    pipeline = next((p for p in pipelines if p.id == pipeline_id), pipelines[0] if pipelines else None)
    columns = worklists.board(db, user, pipeline, to_int(owner)) if pipeline else []
    return render(request, "pipeline.html", {
        "pipelines": pipelines, "pipeline": pipeline, "columns": columns, "owner": owner,
        "managers": assignable_users(db, user),
    })


@router.get("/tasks")
def my_tasks(request: Request, tab: str = "today", owner: str = "",
             user: User = Depends(current_user), db: Session = Depends(get_db)):
    lists = worklists.task_lists(db, user, to_int(owner))
    if tab not in ("today", "overdue", "no_next"):
        tab = "today"
    return render(request, "tasks.html", {
        "lists": lists, "tab": tab, "items": lists[tab], "owner": owner, "managers": assignable_users(db, user),
    })


@router.get("/payments")
def payments_page(request: Request, status: str = PAY_PENDING, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    if status not in PAYMENT_STATUSES and status != "all":
        status = PAY_PENDING
    items = pay_svc.list_payments(db, user, None if status == "all" else status)
    return render(request, "payments.html", {
        "items": items, "status": status, "pending": pay_svc.pending_count(db, user),
        "can_decide": {p.id for p in items if p.status == PAY_PENDING and pay_svc.can_confirm(user, p)},
    })


@router.get("/rating")
def rating_page(request: Request, month: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    now = worklists.today()
    year, mon = now.year, now.month
    try:
        y, m = (int(x) for x in month.split("-"))
        if 2000 <= y <= 2100 and 1 <= m <= 12:
            year, mon = y, m
    except ValueError:
        pass
    return render(request, "rating.html", {"rating": pay_svc.rating(db, user, year, mon), "month": f"{year:04d}-{mon:02d}"})


@router.get("/reports")
def reports(request: Request, date_from: str = "", date_to: str = "",
            user: User = Depends(boss_user), db: Session = Depends(get_db)):
    now = worklists.today()
    start = to_date(date_from) or date(now.year, now.month, 1)
    end = to_date(date_to) or now
    if end < start:
        start, end = end, start
    return render(request, "reports.html", {"report": worklists.manager_report(db, user, start, end)})


@router.get("/help")
def help_page(request: Request, user: User = Depends(current_user)):
    return render(request, "help.html", {})
