"""Импорт: загрузка, сопоставление колонок CSV, предпросмотр, подтверждение.

Загружать списки может каждый. Свои загрузки видит сам сотрудник, все — только владелец.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import FormData, UploadFile

from .. import config
from ..db import get_db
from ..models import (
    BATCH_CONFIRMED,
    BATCH_CONFIRMING,
    BATCH_MAPPING,
    BATCH_PREVIEW,
    ROW_DUPLICATE,
    ROW_NEW,
    ROW_REVIEW,
    ROW_STATUSES,
    Deal,
    ImportBatch,
    ImportRow,
    Pipeline,
    User,
)
from ..services import importer
from ..services.access import NotFound, assignable_users, visible_company_ids
from ..services.parsing import FIELDS, decode_bytes
from .common import current_user, flash, form_data, redirect, render, to_int

router = APIRouter()


def _batch(db: Session, user: User, batch_id: int) -> ImportBatch:
    batch = db.get(ImportBatch, batch_id)
    if batch is None or (not user.is_owner and batch.user_id != user.id):
        raise NotFound()
    return batch


def _form_ctx(db: Session, user: User, error: str | None = None, mode: str = "text", text: str = "") -> dict:
    stmt = select(ImportBatch).options(selectinload(ImportBatch.user)).order_by(ImportBatch.id.desc()).limit(10)
    if not user.is_owner:
        stmt = stmt.where(ImportBatch.user_id == user.id)
    return {"recent": db.scalars(stmt).all(), "error": error, "mode": mode, "text": text}


@router.get("/import")
def import_form(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "import_form.html", _form_ctx(db, user))


@router.post("/import")
def import_create(request: Request, form: FormData = Depends(form_data), user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    mode = str(form.get("mode", "text"))
    text = str(form.get("text", ""))
    upload = form.get("file")
    name = None
    try:
        if mode in ("txt", "csv"):
            if not isinstance(upload, UploadFile) or not upload.filename:
                raise importer.ImportError_("Выберите файл.")
            data = upload.file.read(config.MAX_IMPORT_BYTES + 1)
            if len(data) > config.MAX_IMPORT_BYTES:
                raise importer.ImportError_("Файл больше 2 МБ — разделите его на части.")
            text, name = decode_bytes(data), upload.filename[:200]
        if mode == "csv":
            db.rollback()
            batch = importer.create_csv_batch(db, user, name, text)
            return redirect(f"/import/{batch.id}/mapping")
        if mode not in ("text", "txt", "sheets"):
            raise importer.ImportError_("Неизвестный способ загрузки.")
        # Разбор идёт без блокировки базы: длинный список не задерживает остальных.
        db.rollback()
        parsed = importer.parse_list(mode, text)
        batch = importer.create_text_batch(db, user, mode, name, text, parsed)
    except importer.ImportError_ as exc:
        db.rollback()
        ctx = _form_ctx(db, user, str(exc), mode, text if mode in ("text", "sheets") else "")
        return render(request, "import_form.html", ctx, 400)
    return redirect(f"/import/{batch.id}")


@router.get("/import/{batch_id}/mapping")
def mapping_page(request: Request, batch_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    batch = _batch(db, user, batch_id)
    if batch.status != BATCH_MAPPING:
        return redirect(f"/import/{batch.id}")
    return render(request, "import_mapping.html", {"batch": batch, "csv": importer.csv_preview(batch), "fields": FIELDS, "error": None})


@router.post("/import/{batch_id}/mapping")
def mapping_apply(request: Request, batch_id: int, form: FormData = Depends(form_data),
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    batch = _batch(db, user, batch_id)
    preview = importer.csv_preview(batch)
    mapping = [str(form.get(f"col_{i}", "skip")) for i in range(preview["width"])]
    mapping = [m if m in FIELDS or m == "skip" else "skip" for m in mapping]
    try:
        has_header = form.get("has_header") == "1"
        raw_text = batch.raw_text or ""
        db.rollback()  # разбор файла — без блокировки базы
        parsed = importer.parse_csv(raw_text, mapping, has_header)
        batch = _batch(db, user, batch_id)
        importer.apply_csv_mapping(db, batch, mapping, has_header, parsed)
    except importer.ImportError_ as exc:
        db.rollback()
        preview["mapping"] = mapping
        ctx = {"batch": _batch(db, user, batch_id), "csv": preview, "fields": FIELDS, "error": str(exc)}
        return render(request, "import_mapping.html", ctx, 400)
    return redirect(f"/import/{batch.id}")


@router.get("/import/{batch_id}")
def preview(request: Request, batch_id: int, status: str = "", page: int = 1,
            user: User = Depends(current_user), db: Session = Depends(get_db)):
    batch = _batch(db, user, batch_id)
    if batch.status == BATCH_MAPPING:
        return redirect(f"/import/{batch.id}/mapping")
    stmt = select(ImportRow).where(ImportRow.batch_id == batch.id)
    if batch.status == BATCH_CONFIRMED:
        if status in ("created", "review", "skipped", "excluded"):
            stmt = stmt.where(ImportRow.result == status)
    elif status in ROW_STATUSES:
        stmt = stmt.where(ImportRow.status == status)
    elif status == "excluded":
        stmt = stmt.where(ImportRow.excluded.is_(True))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    pages = max(1, -(-total // config.PREVIEW_PAGE_SIZE))
    page = min(max(1, page), pages)
    rows = db.scalars(
        stmt.options(selectinload(ImportRow.match_company)).order_by(ImportRow.row_no)
        .limit(config.PREVIEW_PAGE_SIZE).offset((page - 1) * config.PREVIEW_PAGE_SIZE)
    ).all()
    # Чьи это компании: имя ответственного показываем всем, а ссылку на карточку — только тем, кому она видна.
    match_ids = sorted({cid for r in rows for cid in (r.match_company_id, r.company_id) if cid})
    owners: dict[int, list[str]] = {}
    if match_ids:
        for cid, owner in db.execute(
            select(Deal.company_id, User.name).outerjoin(User, Deal.owner_id == User.id)
            .where(Deal.company_id.in_(match_ids))
        ):
            owners.setdefault(cid, []).append(owner or "не назначен")
    ctx = {
        "batch": batch, "rows": rows, "status": status, "page": page, "pages": pages, "total": total,
        "summary": importer.summary(db, batch.id), "owners": owners, "ROW_STATUSES": ROW_STATUSES,
        "visible": visible_company_ids(db, user, match_ids),
        "pipelines": db.scalars(select(Pipeline).where(Pipeline.is_archived.is_(False)).order_by(Pipeline.is_default.desc(), Pipeline.id)).all(),
        "managers": assignable_users(db, user),
        "ROW_NEW": ROW_NEW, "ROW_DUPLICATE": ROW_DUPLICATE, "ROW_REVIEW": ROW_REVIEW,
    }
    if batch.status == BATCH_CONFIRMED:
        return render(request, "import_result.html", ctx)
    if batch.status == BATCH_CONFIRMING:
        return render(request, "import_running.html", ctx)
    if batch.status != BATCH_PREVIEW:
        flash(request, "Этот импорт отменён.", "warn")
        return redirect("/import")
    return render(request, "import_preview.html", ctx)


def _edits(form: FormData) -> dict[int, dict]:
    edits: dict[int, dict] = {}
    for raw_id in form.getlist("row_id"):
        rid = to_int(raw_id)
        if rid is None:
            continue
        data = {f: str(form.get(f"{f}_{rid}", "")) for f in importer.EDITABLE if f"{f}_{rid}" in form}
        data["excluded"] = form.get(f"excluded_{rid}") == "1"
        data["force_create"] = form.get(f"force_{rid}") == "1"
        edits[rid] = data
    return edits


def _back(batch_id: int, form: FormData) -> str:
    status, page = str(form.get("status", "")), to_int(form.get("page")) or 1
    return f"/import/{batch_id}?status={status}&page={page}"


@router.post("/import/{batch_id}/rows")
def save_rows(request: Request, batch_id: int, form: FormData = Depends(form_data),
              user: User = Depends(current_user), db: Session = Depends(get_db)):
    batch = _batch(db, user, batch_id)
    try:
        bulk = str(form.get("bulk", ""))
        importer.update_rows(db, batch, _edits(form))
        if bulk in ("exclude_review", "include_review"):
            importer.bulk_exclude(db, batch, ROW_REVIEW, bulk == "exclude_review")
        flash(request, "Правки сохранены, статусы строк пересчитаны.")
    except importer.ImportError_ as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return redirect(_back(batch_id, form))


@router.post("/import/{batch_id}/confirm")
def confirm(request: Request, batch_id: int, form: FormData = Depends(form_data),
            user: User = Depends(current_user), db: Session = Depends(get_db)):
    batch = _batch(db, user, batch_id)
    try:
        resume = batch.status == BATCH_CONFIRMING and form.get("resume") == "1"
        if not resume:
            importer.update_rows(db, batch, _edits(form))
        pipeline_id, owner_id = importer.resolve_target(db, user, to_int(form.get("pipeline_id")), to_int(form.get("owner_id")))
        importer.confirm(db, batch_id, user, pipeline_id, owner_id, resume=resume)
    except importer.ImportError_ as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect(_back(batch_id, form))
    return redirect(f"/import/{batch_id}")


@router.post("/import/{batch_id}/cancel")
def cancel(request: Request, batch_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    importer.cancel(db, _batch(db, user, batch_id))
    flash(request, "Импорт отменён, карточки не созданы.", "warn")
    return redirect("/import")
