"""Настройки руководителя: воронки, этапы, сотрудники."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import FormData

from ..db import get_db
from ..models import ROLE_HEAD, Pipeline, Stage, User
from ..security import hash_password
from ..services import setup
from ..services.access import NotFound
from .common import flash, form_data, head_user, redirect, render

router = APIRouter(prefix="/settings")


@router.get("/pipelines")
def pipelines_page(request: Request, user: User = Depends(head_user), db: Session = Depends(get_db)):
    pipelines = db.scalars(select(Pipeline).options(selectinload(Pipeline.stages)).order_by(Pipeline.is_default.desc(), Pipeline.id)).all()
    return render(request, "settings_pipelines.html", {"pipelines": pipelines})


def _do(request: Request, db: Session, action, ok: str) -> None:
    try:
        action()
        db.commit()
        flash(request, ok)
    except setup.Invalid as exc:
        db.rollback()
        flash(request, str(exc), "error")


@router.post("/pipelines")
def pipeline_create(request: Request, form: FormData = Depends(form_data), user: User = Depends(head_user),
                    db: Session = Depends(get_db)):
    _do(request, db, lambda: setup.create_pipeline(db, str(form.get("name", ""))), "Воронка создана. Настройте её этапы.")
    return redirect("/settings/pipelines")


@router.post("/pipelines/{pipeline_id}")
def pipeline_update(request: Request, pipeline_id: int, form: FormData = Depends(form_data),
                    user: User = Depends(head_user), db: Session = Depends(get_db)):
    pipeline = db.get(Pipeline, pipeline_id)
    if pipeline is None:
        raise NotFound()
    action = str(form.get("action", ""))

    def run():
        if action == "add_stage":
            setup.add_stage(db, pipeline, str(form.get("name", "")))
        elif action == "rename":
            name = str(form.get("name", "")).strip()
            if not name:
                raise setup.Invalid("Укажите название воронки.")
            pipeline.name = name[:200]
        elif action == "archive":
            if pipeline.is_default:
                raise setup.Invalid("Основную воронку нельзя убрать в архив.")
            pipeline.is_archived = not pipeline.is_archived
        elif action == "default":
            for p in db.scalars(select(Pipeline)):
                p.is_default = p.id == pipeline.id
            pipeline.is_archived = False

    _do(request, db, run, "Сохранено.")
    return redirect("/settings/pipelines")


@router.post("/stages/{stage_id}")
def stage_update(request: Request, stage_id: int, form: FormData = Depends(form_data),
                 user: User = Depends(head_user), db: Session = Depends(get_db)):
    stage = db.get(Stage, stage_id)
    if stage is None:
        raise NotFound()
    action = str(form.get("action", "save"))

    def run():
        if action == "delete":
            setup.delete_stage(db, stage)
        elif action in ("up", "down"):
            setup.move_stage(db, stage, -1 if action == "up" else 1)
        else:
            setup.update_stage(db, stage, str(form.get("name", "")), str(form.get("kind", "open")), str(form.get("metric_key", "")) or None)

    _do(request, db, run, "Сохранено.")
    return redirect("/settings/pipelines")


@router.get("/users")
def users_page(request: Request, user: User = Depends(head_user), db: Session = Depends(get_db)):
    return render(request, "settings_users.html", {"users": db.scalars(select(User).order_by(User.role, User.name)).all()})


@router.post("/users")
def user_create(request: Request, form: FormData = Depends(form_data), user: User = Depends(head_user),
                db: Session = Depends(get_db)):
    _do(request, db, lambda: setup.create_user(
        db, str(form.get("login", "")), str(form.get("name", "")), str(form.get("password", "")), str(form.get("role", "manager"))
    ), "Сотрудник добавлен. Передайте ему логин и пароль.")
    return redirect("/settings/users")


@router.post("/users/{user_id}")
def user_update(request: Request, user_id: int, form: FormData = Depends(form_data),
                user: User = Depends(head_user), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if target is None:
        raise NotFound()
    action = str(form.get("action", ""))

    def run():
        if action == "toggle":
            if target.id == user.id:
                raise setup.Invalid("Нельзя отключить самого себя.")
            if target.is_active and target.role == ROLE_HEAD and not db.scalar(
                select(User.id).where(User.role == ROLE_HEAD, User.is_active.is_(True), User.id != target.id)
            ):
                raise setup.Invalid("В системе должен остаться хотя бы один руководитель.")
            target.is_active = not target.is_active
        elif action == "password":
            password = str(form.get("password", ""))
            if len(password) < 8:
                raise setup.Invalid("Пароль должен быть не короче 8 символов.")
            target.password_hash = hash_password(password)
        elif action == "rename":
            name = str(form.get("name", "")).strip()
            if not name:
                raise setup.Invalid("Укажите имя.")
            target.name = name[:200]

    _do(request, db, run, "Сохранено.")
    return redirect("/settings/users")
