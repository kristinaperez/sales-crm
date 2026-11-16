"""Общее для обработчиков: шаблоны, текущий пользователь, формы, сообщения."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from .. import config
from ..db import get_db
from ..models import ACTIVITY_KINDS, METRICS, PAYMENT_STATUSES, ROLES, STAGE_KINDS, User
from ..services.deals import format_money
from ..services.access import Forbidden
from ..services.normalize import ensure_scheme
from ..services.worklists import today

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


class LoginRequired(Exception):
    pass


def _dt(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.replace(tzinfo=timezone.utc).astimezone(config.APP_TZ).strftime("%d.%m.%Y %H:%M")


def _d(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


def _qs(params: dict, **override) -> str:
    merged = {**params, **override}
    return urlencode({k: v for k, v in merged.items() if v not in ("", None)})


templates.env.filters["dt"] = _dt
templates.env.filters["d"] = _d
templates.env.filters["href"] = ensure_scheme
templates.env.filters["money"] = lambda minor: f"{format_money(minor or 0)} {config.CURRENCY}"
templates.env.filters["amount"] = lambda minor: format_money(minor or 0).replace(" ", "")
templates.env.globals.update(
    qs=_qs, today=today, ACTIVITY_KINDS=ACTIVITY_KINDS, METRICS=METRICS, STAGE_KINDS=STAGE_KINDS,
    ROLES=ROLES, PAYMENT_STATUSES=PAYMENT_STATUSES, CURRENCY=config.CURRENCY,
)


def flash(request: Request, message: str, level: str = "ok") -> None:
    # Присваиваем заново: изменение вложенного списка сессия могла бы не заметить.
    request.session["flash"] = [*request.session.get("flash", []), [level, message]]


def render(request: Request, name: str, ctx: dict | None = None, status_code: int = 200):
    context = dict(ctx or {})
    context.setdefault("user", getattr(request.state, "user", None))
    context["flashes"] = request.session.pop("flash", [])
    context["path"] = request.url.path
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if (user is None or not user.is_active or request.session.get("pv") != user.password_hash[-10:]
            or request.session.get("sn") != user.session_nonce):
        request.session.clear()
        raise LoginRequired()
    request.state.user = user
    return user


def owner_user(user: User = Depends(current_user)) -> User:
    if not user.is_owner:
        raise Forbidden()
    return user


def boss_user(user: User = Depends(current_user)) -> User:
    """Владелец или руководитель группы."""
    if not user.is_boss:
        raise Forbidden()
    return user


async def form_data(request: Request) -> FormData:
    return await request.form(max_fields=6000, max_files=5, max_part_size=config.MAX_IMPORT_BYTES)


def to_int(value) -> int | None:
    """Целое из формы или адреса; мусор и числа вне диапазона идентификаторов — None."""
    try:
        number = int(str(value).strip()[:20])
    except (TypeError, ValueError):
        return None
    return number if 0 <= number < 2**62 else None


def to_date(value) -> date | None:
    try:
        parsed = date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError):
        return None
    return parsed if 2000 <= parsed.year <= 2100 else None


def safe_next(url: str | None, default: str) -> str:
    """Только внутренние адреса — чтобы вход не превращался в открытый редирект."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return default
