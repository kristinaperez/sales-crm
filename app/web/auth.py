"""Вход, выход, смена своего пароля."""
from __future__ import annotations

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

_FAILS: dict[str, list[float]] = {}
_WINDOW, _LIMIT = 600, 10


def _blocked(key: str) -> bool:
    now = time.time()
    _FAILS[key] = [t for t in _FAILS.get(key, []) if now - t < _WINDOW]
    return len(_FAILS[key]) >= _LIMIT


@router.get("/login")
def login_page(request: Request, next: str = ""):
    return render(request, "login.html", {"next": next, "error": None, "login": ""})


@router.post("/login")
def login(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    login_value = str(form.get("login", "")).strip().lower()
    password = str(form.get("password", ""))
    key = f"{login_value}|{request.client.host if request.client else ''}"
    if _blocked(key):
        return render(request, "login.html", {"next": form.get("next", ""), "login": login_value,
                                              "error": "Слишком много попыток. Подождите 10 минут."}, 429)
    user = db.scalar(select(User).where(func.lower(User.login) == login_value))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        _FAILS.setdefault(key, []).append(time.time())
        return render(request, "login.html", {"next": form.get("next", ""), "login": login_value,
                                              "error": "Неверный логин или пароль."}, 401)
    _FAILS.pop(key, None)
    request.session.clear()
    request.session["uid"] = user.id
    request.session["pv"] = user.password_hash[-10:]
    return redirect(safe_next(str(form.get("next", "")), "/"))


@router.post("/logout")
def logout(request: Request):
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
