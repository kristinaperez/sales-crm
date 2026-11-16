"""Сборка приложения: маршруты, сессии, защита форм, страницы ошибок."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import config
from .services.access import Forbidden, NotFound
from .web import auth, companies, deals, imports, settings, work
from .web.common import LoginRequired, redirect, render


def create_app() -> FastAPI:
    app = FastAPI(title="CRM холодных продаж", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def same_origin_posts(request: Request, call_next):
        """Защита от CSRF: изменяющие запросы принимаются только со страниц самого приложения."""
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            source = request.headers.get("origin") or request.headers.get("referer")
            if not source or urlsplit(source).netloc != request.headers.get("host"):
                return PlainTextResponse("Запрос отклонён: он пришёл не со страницы CRM.", status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'",
        )
        if not request.url.path.startswith("/static"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.add_middleware(
        SessionMiddleware,
        secret_key=config.secret_key(),
        session_cookie="crm_session",
        max_age=14 * 24 * 3600,
        same_site="lax",
        https_only=config.SESSION_HTTPS_ONLY,
    )
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    for module in (auth, imports, companies, deals, work, settings):
        app.include_router(module.router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc):
        if request.method == "GET" and request.url.path != "/":
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return redirect(f"/login?next={quote(target)}")
        return redirect("/login")

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, _exc):
        return render(request, "error.html", {"title": "Не найдено", "message": "Такой страницы нет или у вас нет к ней доступа."}, 404)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, _exc):
        return render(request, "error.html", {"title": "Нет доступа", "message": "У вас нет прав на это действие."}, 403)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return render(request, "error.html", {"title": "Не найдено", "message": "Такой страницы нет или у вас нет к ней доступа."}, 404)
        return render(request, "error.html", {"title": f"Ошибка {exc.status_code}", "message": str(exc.detail)}, exc.status_code)

    @app.exception_handler(OverflowError)
    async def _overflow(request: Request, _exc):
        # Число в адресе, которое не помещается в идентификатор: такой записи заведомо нет.
        return render(request, "error.html", {"title": "Не найдено", "message": "Такой страницы нет или у вас нет к ней доступа."}, 404)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return PlainTextResponse("ok")

    return app


app = create_app()
