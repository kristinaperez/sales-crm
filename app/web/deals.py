"""Карточка сделки, оплаты и действия с ними. Доступ проверяется для каждого запроса."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import FormData

from ..db import get_db
from ..models import MANUAL_ACTIVITY_KINDS, PAY_PENDING, Activity, Deal, Payment, Task, User
from ..services import companies as company_svc
from ..services import deals as svc
from ..services import payments as pay_svc
from ..services.access import NotFound, assignable_users, get_deal, get_payment, get_task
from .common import current_user, flash, form_data, owner_user, redirect, render, safe_next, to_date, to_int

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
            "message": "Пока вы работали с карточкой, у сделки сменился ответственный. Ваше изменение не записано.",
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
    payments = db.scalars(
        select(Payment).where(Payment.deal_id == deal.id)
        .options(selectinload(Payment.credited_to), selectinload(Payment.confirmed_by))
        .order_by(Payment.paid_on.desc(), Payment.id.desc())
    ).all()
    managers = assignable_users(db, user)
    credit_options = {u.id: u for u in managers}
    for p in payments:
        if p.credited_to is not None:
            credit_options.setdefault(p.credited_to.id, p.credited_to)
    return render(request, "deal.html", {
        "deal": deal, "company": deal.company, "history": history, "tasks": tasks, "managers": managers,
        "payments": payments, "can_decide": {p.id for p in payments if p.status == PAY_PENDING and pay_svc.can_confirm(user, p)},
        "credit_options": sorted(credit_options.values(), key=lambda u: u.name),
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
        db, deal, to_int(form.get("stage_id")) or 0, user, to_int(form.get("version")), str(form.get("lost_reason", "")),
        pay_svc.parse_amount(form.get("amount")), to_date(form.get("paid_on")),
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
    draft = {k: str(form.get(k, "")) for k in
             ("kind", "body", "followup_due", "followup_title", "stage_id", "lost_reason", "amount", "paid_on")}
    return _run(request, db, user, deal_id, lambda: svc.log_touch(
        db, deal, user, draft["kind"], draft["body"],
        stage_id=to_int(draft["stage_id"]), lost_reason=draft["lost_reason"],
        amount_minor=pay_svc.parse_amount(draft["amount"]), paid_on=to_date(draft["paid_on"]),
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


@router.post("/deals/{deal_id}/payments")
def add_payment(request: Request, deal_id: int, form: FormData = Depends(form_data),
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    deal = get_deal(db, user, deal_id)
    return _run(request, db, user, deal_id, lambda: pay_svc.add_payment(
        db, deal, user, form.get("amount"), to_date(form.get("paid_on")), str(form.get("note", ""))
    ), "Оплата внесена и ждёт подтверждения.")


@router.post("/payments/{payment_id}/status")
def payment_status(request: Request, payment_id: int, form: FormData = Depends(form_data),
                   user: User = Depends(current_user), db: Session = Depends(get_db)):
    payment = get_payment(db, user, payment_id)
    back = safe_next(str(form.get("back", "")), f"/deals/{payment.deal_id}")
    try:
        pay_svc.set_status(db, payment, user, str(form.get("decision", "")))
        flash(request, "Решение по оплате сохранено.")
    except svc.Invalid as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return redirect(back)


@router.post("/payments/{payment_id}/edit")
def payment_edit(request: Request, payment_id: int, form: FormData = Depends(form_data),
                 user: User = Depends(owner_user), db: Session = Depends(get_db)):
    payment = get_payment(db, user, payment_id)
    deal_id = payment.deal_id
    try:
        pay_svc.correct(db, payment, user, form.get("amount"), to_date(form.get("paid_on")),
                        to_int(form.get("credited_to_id")), str(form.get("status", "")), str(form.get("note", "")))
        flash(request, "Оплата исправлена. Что было и что стало — в истории сделки и в журнале исправлений.")
    except svc.Invalid as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return redirect(f"/deals/{deal_id}#payments")


@router.post("/payments/{payment_id}/delete")
def payment_delete(request: Request, payment_id: int, user: User = Depends(owner_user), db: Session = Depends(get_db)):
    payment = get_payment(db, user, payment_id)
    deal_id = payment.deal_id
    pay_svc.delete(db, payment, user)
    flash(request, "Оплата удалена. Запись об этом есть в истории сделки и в журнале исправлений.", "warn")
    return redirect(f"/deals/{deal_id}#payments")


@router.post("/deals/{deal_id}/delete")
def deal_delete(request: Request, deal_id: int, user: User = Depends(owner_user), db: Session = Depends(get_db)):
    deal = get_deal(db, user, deal_id)
    company_id = company_svc.delete_deal(db, deal, user)
    flash(request, "Сделка удалена вместе с задачами, оплатами и историей. Запись об этом есть в журнале исправлений.", "warn")
    return redirect(f"/companies/{company_id}")


