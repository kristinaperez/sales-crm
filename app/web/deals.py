"""Карточка сделки и действия с ней. Доступ проверяется для каждого запроса."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import FormData

from ..db import get_db
from ..models import MANUAL_ACTIVITY_KINDS, ROLE_MANAGER, Activity, Deal, Task, User
from ..services import deals as svc
from ..services.access import NotFound, get_deal, get_task
from .common import current_user, flash, form_data, redirect, render, safe_next, to_date, to_int

router = APIRouter()


def render_deal(request: Request, db: Session, user: User, deal_id: int, *, error: str | None = None,
                conflict: bool = False, status: int = 200, draft: dict | None = None):
    db.rollback()  # читаем актуальное состояние
    try:
        get_deal(db, user, deal_id)
    except NotFound:
        if not conflict:
            raise
        return render(request, "error.html", {
            "title": "Сделка передана другому менеджеру",
            "message": "Пока вы работали с карточкой, руководитель сменил ответственного. Ваше изменение не записано.",
        }, 409)
    deal = db.scalar(
        select(Deal).where(Deal.id == deal_id).options(
            selectinload(Deal.company), selectinload(Deal.stage), selectinload(Deal.owner), selectinload(Deal.pipeline)
        ).execution_options(populate_existing=True)
    )
    history = db.scalars(
        select(Activity).where(
            Activity.company_id == deal.company_id, or_(Activity.deal_id == deal.id, Activity.deal_id.is_(None))
        ).options(selectinload(Activity.user)).order_by(Activity.created_at.desc(), Activity.id.desc()).limit(300)
    ).all()
    tasks = db.scalars(
        select(Task).where(Task.deal_id == deal.id).options(selectinload(Task.assignee))
        .order_by(Task.done_at.is_not(None), Task.due_date, Task.id)
    ).all()
    managers = db.scalars(select(User).where(User.is_active.is_(True), User.role == ROLE_MANAGER).order_by(User.name)).all() if user.is_head else []
    return render(request, "deal.html", {
        "deal": deal, "company": deal.company, "history": history, "tasks": tasks, "managers": managers,
        "error": error, "conflict": conflict, "draft": draft or {}, "kinds": MANUAL_ACTIVITY_KINDS,
    }, status)


def _run(request: Request, db: Session, user: User, deal_id: int, action, ok: str, draft: dict | None = None,
         back: str | None = None):
    """Выполняет действие; при конфликте или ошибке показывает карточку с актуальным состоянием."""
    try:
        action()
    except svc.Conflict:
        db.rollback()
        return render_deal(request, db, user, deal_id, conflict=True, status=409, draft=draft)
    except svc.Invalid as exc:
        db.rollback()
        return render_deal(request, db, user, deal_id, error=str(exc), status=400, draft=draft)
    flash(request, ok)
    return redirect(back or f"/deals/{deal_id}")


@router.get("/deals/{deal_id}")
def deal_card(request: Request, deal_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    get_deal(db, user, deal_id)
    return render_deal(request, db, user, deal_id)


@router.post("/deals/{deal_id}/stage")
def change_stage(request: Request, deal_id: int, form: FormData = Depends(form_data),
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    deal = get_deal(db, user, deal_id)
    back = safe_next(str(form.get("back", "")), f"/deals/{deal_id}")
    return _run(request, db, user, deal_id, lambda: svc.change_stage(
        db, deal, to_int(form.get("stage_id")) or 0, user, to_int(form.get("version")), str(form.get("lost_reason", ""))
    ), "Этап изменён.", back=back)


@router.post("/deals/{deal_id}/owner")
def change_owner(request: Request, deal_id: int, form: FormData = Depends(form_data),
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    deal = get_deal(db, user, deal_id)
    return _run(request, db, user, deal_id, lambda: svc.change_owner(
        db, deal, to_int(form.get("owner_id")), user, to_int(form.get("version"))
    ), "Ответственный изменён.")


@router.post("/deals/{deal_id}/next-step")
def next_step(request: Request, deal_id: int, form: FormData = Depends(form_data),
              user: User = Depends(current_user), db: Session = Depends(get_db)):
    deal = get_deal(db, user, deal_id)
    return _run(request, db, user, deal_id, lambda: svc.set_next_step(
        db, deal, str(form.get("next_step", "")), to_date(form.get("next_step_due")), user, to_int(form.get("version"))
    ), "Следующий шаг сохранён.")


@router.post("/deals/{deal_id}/touch")
def touch(request: Request, deal_id: int, form: FormData = Depends(form_data),
          user: User = Depends(current_user), db: Session = Depends(get_db)):
    deal = get_deal(db, user, deal_id)
    draft = {k: str(form.get(k, "")) for k in ("kind", "body", "followup_due", "followup_title", "stage_id", "lost_reason")}
    return _run(request, db, user, deal_id, lambda: svc.log_touch(
        db, deal, user, draft["kind"], draft["body"],
        stage_id=to_int(draft["stage_id"]), lost_reason=draft["lost_reason"],
        followup_due=to_date(draft["followup_due"]), followup_title=draft["followup_title"],
        expected_version=to_int(form.get("version")),
    ), "Запись добавлена в историю.", draft=draft)


@router.post("/deals/{deal_id}/tasks")
def add_task(request: Request, deal_id: int, form: FormData = Depends(form_data),
             user: User = Depends(current_user), db: Session = Depends(get_db)):
    deal = get_deal(db, user, deal_id)

    def action():
        svc.add_task(db, deal, user, str(form.get("title", "")), to_date(form.get("due_date")))
        db.commit()

    return _run(request, db, user, deal_id, action, "Задача поставлена.")


@router.post("/tasks/{task_id}/done")
def task_done(request: Request, task_id: int, form: FormData = Depends(form_data),
              user: User = Depends(current_user), db: Session = Depends(get_db)):
    task = get_task(db, user, task_id)
    svc.complete_task(db, task, user)
    flash(request, "Задача выполнена.")
    return redirect(safe_next(str(form.get("back", "")), f"/deals/{task.deal_id}"))
