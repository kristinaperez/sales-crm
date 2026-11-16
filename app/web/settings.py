"""Настройки: воронки и этапы (владелец), сотрудники и группы, журнал исправлений."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import FormData

from ..db import get_db
from ..models import ROLE_MANAGER, ROLE_OWNER, ROLES, AuditLog, Pipeline, Stage, Team, User
from ..security import hash_password
from ..services import setup
from ..services.access import NotFound
from ..services.payments import audit
from .common import boss_user, flash, form_data, owner_user, redirect, render, to_int

router = APIRouter(prefix="/settings")


@router.get("/pipelines")
def pipelines_page(request: Request, user: User = Depends(owner_user), db: Session = Depends(get_db)):
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
def pipeline_create(request: Request, form: FormData = Depends(form_data), user: User = Depends(owner_user),
                    db: Session = Depends(get_db)):
    _do(request, db, lambda: setup.create_pipeline(db, str(form.get("name", ""))), "Воронка создана. Настройте её этапы.")
    return redirect("/settings/pipelines")


@router.post("/pipelines/{pipeline_id}")
def pipeline_update(request: Request, pipeline_id: int, form: FormData = Depends(form_data),
                    user: User = Depends(owner_user), db: Session = Depends(get_db)):
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
                 user: User = Depends(owner_user), db: Session = Depends(get_db)):
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


# --- Сотрудники и группы ------------------------------------------------------------------
# Владелец управляет всеми. Руководитель группы — только менеджерами своей группы:
# добавить, переименовать, сменить пароль, отключить. Роли и группы меняет только владелец.


def _manageable(db: Session, user: User, user_id: int) -> User:
    target = db.get(User, user_id)
    if target is None:
        raise NotFound()
    if user.is_owner:
        return target
    if user.team_id and target.team_id == user.team_id and target.role == ROLE_MANAGER:
        return target
    raise NotFound()


@router.get("/users")
def users_page(request: Request, user: User = Depends(boss_user), db: Session = Depends(get_db)):
    stmt = select(User).options(selectinload(User.team)).order_by(User.is_active.desc(), User.name)
    if not user.is_owner:
        stmt = stmt.where((User.id == user.id) | ((User.team_id == user.team_id) & (User.team_id.is_not(None))))
    teams = db.scalars(select(Team).order_by(Team.name)).all() if user.is_owner else []
    return render(request, "settings_users.html", {"users": db.scalars(stmt).all(), "teams": teams})


@router.post("/users")
def user_create(request: Request, form: FormData = Depends(form_data), user: User = Depends(boss_user),
                db: Session = Depends(get_db)):
    role, team_id = str(form.get("role", ROLE_MANAGER)), to_int(form.get("team_id"))

    def run():
        nonlocal role, team_id
        if not user.is_owner:
            if not user.team_id:
                raise setup.Invalid("У вас нет группы — добавлять сотрудников может владелец.")
            role, team_id = ROLE_MANAGER, user.team_id
        created = setup.create_user(db, str(form.get("login", "")), str(form.get("name", "")),
                                    str(form.get("password", "")), role, team_id)
        audit(db, user, "создание", "сотрудник", created.id, f"Добавлен {created.name} ({created.login}), роль: {created.role_label}")

    _do(request, db, run, "Сотрудник добавлен. Передайте ему логин и пароль.")
    return redirect("/settings/users")


@router.post("/users/{user_id}")
def user_update(request: Request, user_id: int, form: FormData = Depends(form_data),
                user: User = Depends(boss_user), db: Session = Depends(get_db)):
    target = _manageable(db, user, user_id)
    action = str(form.get("action", ""))

    def last_owner() -> bool:
        return target.role == ROLE_OWNER and target.is_active and not db.scalar(
            select(User.id).where(User.role == ROLE_OWNER, User.is_active.is_(True), User.id != target.id))

    def run():
        if action == "toggle":
            if target.id == user.id:
                raise setup.Invalid("Нельзя отключить самого себя.")
            if last_owner():
                raise setup.Invalid("В системе должен остаться хотя бы один владелец.")
            target.is_active = not target.is_active
            audit(db, user, "изменение", "сотрудник", target.id, f"{target.name}: {'включён' if target.is_active else 'отключён'}")
        elif action == "password":
            password = str(form.get("password", ""))
            if len(password) < 8:
                raise setup.Invalid("Пароль должен быть не короче 8 символов.")
            target.password_hash = hash_password(password)
            audit(db, user, "изменение", "сотрудник", target.id, f"{target.name}: сменён пароль")
        elif action == "rename":
            name = str(form.get("name", "")).strip()
            if not name:
                raise setup.Invalid("Укажите имя.")
            target.name = name[:200]
        elif action == "assign":
            if not user.is_owner:
                raise setup.Invalid("Роль и группу меняет только владелец.")
            role, team_id = str(form.get("role", target.role)), to_int(form.get("team_id"))
            if role not in ROLES:
                raise setup.Invalid("Неизвестная роль.")
            if role != target.role and (target.id == user.id or last_owner()):
                raise setup.Invalid("Нельзя снять роль владельца с себя или с последнего владельца.")
            team = db.get(Team, team_id) if team_id else None
            if team_id and team is None:
                raise setup.Invalid("Такой группы нет.")
            before = f"{target.role_label}, группа: {target.team.name if target.team else '—'}"
            target.role = role
            target.team_id = None if role == ROLE_OWNER or team is None else team.id
            after = f"{ROLES[role]}, группа: {team.name if team and role != ROLE_OWNER else '—'}"
            if before != after:
                audit(db, user, "изменение", "сотрудник", target.id, f"{target.name}. Было: {before}. Стало: {after}")

    _do(request, db, run, "Сохранено.")
    return redirect("/settings/users")


@router.post("/teams")
def team_create(request: Request, form: FormData = Depends(form_data), user: User = Depends(owner_user),
                db: Session = Depends(get_db)):
    _do(request, db, lambda: setup.create_team(db, str(form.get("name", ""))), "Группа создана. Назначьте в неё руководителя и менеджеров.")
    return redirect("/settings/users")


@router.post("/teams/{team_id}")
def team_rename(request: Request, team_id: int, form: FormData = Depends(form_data), user: User = Depends(owner_user),
                db: Session = Depends(get_db)):
    team = db.get(Team, team_id)
    if team is None:
        raise NotFound()

    def run():
        name = str(form.get("name", "")).strip()
        if not name:
            raise setup.Invalid("Укажите название группы.")
        if db.scalar(select(Team.id).where(Team.name == name, Team.id != team.id)):
            raise setup.Invalid("Группа с таким названием уже есть.")
        team.name = name[:200]

    _do(request, db, run, "Сохранено.")
    return redirect("/settings/users")


@router.get("/audit")
def audit_page(request: Request, user: User = Depends(owner_user), db: Session = Depends(get_db)):
    entries = db.scalars(
        select(AuditLog).options(selectinload(AuditLog.user)).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(500)
    ).all()
    return render(request, "settings_audit.html", {"entries": entries})
