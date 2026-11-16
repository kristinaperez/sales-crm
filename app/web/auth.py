"""Вход, выход, смена своего пароля."""
from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from ..db import get_db
from ..models import User
from ..security import hash_password, verify_password
from .common import current_user, flash, form_data, redirect, render, safe_next

router = APIRouter()

# Счётчики неудачных входов в памяти процесса: по паре «логин + адрес» и по адресу целиком.
_FAILS: dict[str, list[float]] = {}
_WINDOW, _LIMIT, _IP_LIMIT, _MAX_KEYS = 600, 10, 50, 5000
_MAX_LOGIN, _MAX_PASSWORD = 120, 200


def _recent(key: str) -> int:
    now = time.time()
    fresh = [t for t in _FAILS.get(key, []) if now - t < _WINDOW]
    if fresh:
        _FAILS[key] = fresh
    else:
        _FAILS.pop(key, None)
    return len(fresh)


def _fail(*keys: str) -> None:
    if len(_FAILS) > _MAX_KEYS:
        now = time.time()
        for key in [k for k, v in _FAILS.items() if not v or now - v[-1] >= _WINDOW]:
            _FAILS.pop(key, None)
        if len(_FAILS) > _MAX_KEYS:
            _FAILS.clear()
    for key in keys:
        _FAILS.setdefault(key, []).append(time.time())


@router.get("/login")
def login_page(request: Request, next: str = ""):
    return render(request, "login.html", {"next": next, "error": None, "login": ""})


@router.post("/login")
def login(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    login_value = str(form.get("login", "")).strip().lower()
    password = str(form.get("password", ""))
    next_url = safe_next(str(form.get("next", ""))[:500], "/")
    ip = request.client.host if request.client else ""
    ip_key, key = f"ip|{ip}", f"{login_value[:_MAX_LOGIN]}|{ip}"
    if _recent(key) >= _LIMIT or _recent(ip_key) >= _IP_LIMIT:
        return render(request, "login.html", {"next": next_url, "login": login_value[:_MAX_LOGIN],
                                              "error": "Слишком много попыток. Подождите 10 минут."}, 429)
    too_long = len(login_value) > _MAX_LOGIN or len(password) > _MAX_PASSWORD
    user = None if too_long else db.scalar(select(User).where(func.lower(User.login) == login_value))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        _fail(key, ip_key)
        return render(request, "login.html", {"next": next_url, "login": login_value[:_MAX_LOGIN],
                                              "error": "Неверный логин или пароль."}, 401)
    _FAILS.pop(key, None)
    if not user.session_nonce:
        user.session_nonce = secrets.token_urlsafe(12)
        db.commit()
    request.session.clear()
    request.session["uid"] = user.id
    request.session["pv"] = user.password_hash[-10:]
    request.session["sn"] = user.session_nonce
    return redirect(next_url)


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    # Выход отзывает все сессии сотрудника: скопированная ранее cookie перестаёт работать.
    user = db.get(User, request.session.get("uid") or 0)
    if user is not None and request.session.get("sn") == user.session_nonce:
        user.session_nonce = secrets.token_urlsafe(12)
        db.commit()
    request.session.clear()
    return redirect("/login")


@router.get("/profile")
def profile(request: Request, user: User = Depends(current_user)):
    return render(request, "profile.html", {"error": None})


@router.post("/profile")
def change_password(request: Request, form: FormData = Depends(form_data), user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    old, new = str(form.get("old_password", "")), str(form.get("new_password", ""))
    if not verify_password(old, user.password_hash):
        return render(request, "profile.html", {"error": "Текущий пароль указан неверно."}, 400)
    if len(new) < 8:
        return render(request, "profile.html", {"error": "Новый пароль должен быть не короче 8 символов."}, 400)
    user.password_hash = hash_password(new)
    db.commit()
    request.session["pv"] = user.password_hash[-10:]
    flash(request, "Пароль изменён.")
    return redirect("/profile")
