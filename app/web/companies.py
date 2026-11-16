"""Список компаний, карточка, контакты, ссылки, экспорт, массовое назначение."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import FormData

from ..db import get_db
from ..models import Activity, Company, CompanyLink, Contact, Deal, Pipeline, User
from ..services import companies as svc
from ..services import deals as deal_svc
from ..services import importer
from ..services.access import NotFound, assignable_users, get_company, scope_deals, visible_company_ids
from ..services.worklists import today
from .common import boss_user, current_user, flash, form_data, owner_user, redirect, render, to_int

router = APIRouter()


def _filters(src) -> svc.Filters:
    return svc.Filters(
        q=str(src.get("q", "")).strip(), city=str(src.get("city", "")).strip(),
        industry=str(src.get("industry", "")).strip(), stage_id=to_int(src.get("stage_id")),
        owner=str(src.get("owner", "")).strip(), status=str(src.get("status", "")).strip(),
        no_deal=str(src.get("no_deal", "")) == "1",
    )


def _pipelines(db: Session) -> list[Pipeline]:
    return list(db.scalars(
        select(Pipeline).where(Pipeline.is_archived.is_(False)).options(selectinload(Pipeline.stages))
        .order_by(Pipeline.is_default.desc(), Pipeline.id)
    ))


def _duplicate(db: Session, user: User, company_id: int | None) -> dict | None:
    """Что показать про уже существующую компанию: ссылку — только если она видна пользователю."""
    if not company_id:
        return None
    owners = sorted({name or "не назначен" for (name,) in db.execute(
        select(User.name).select_from(Deal).outerjoin(User, Deal.owner_id == User.id).where(Deal.company_id == company_id)
    )})
    visible = company_id in visible_company_ids(db, user, [company_id])
    return {"id": company_id if visible else None, "owners": owners,
            "name": db.scalar(select(Company.name).where(Company.id == company_id)) if visible else None}


@router.get("/companies")
def company_list(request: Request, page: int = 1, user: User = Depends(current_user), db: Session = Depends(get_db)):
    f = _filters(request.query_params)
    if not user.is_boss:
        f.owner = ""
    if not user.is_owner:
        f.no_deal = False
    ctx = svc.list_companies(db, user, f, page)
    ctx.update(f=f, options=svc.filter_options(db, user), pipelines=_pipelines(db),
               managers=assignable_users(db, user), fq=f.as_query())
    return render(request, "companies.html", ctx)


@router.get("/companies/export.csv")
def export(request: Request, user: User = Depends(owner_user), db: Session = Depends(get_db)):
    body = svc.export_csv(db, user, _filters(request.query_params))
    name = f"companies-{today().isoformat()}.csv"
    return Response(body.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/companies/assign")
def assign(request: Request, form: FormData = Depends(form_data), user: User = Depends(boss_user),
           db: Session = Depends(get_db)):
    f = _filters(form)
    manager_ids = [i for i in (to_int(x) for x in form.getlist("manager_id")) if i]
    if form.get("scope") == "all":
        ids = svc.filtered_ids(db, user, f)
    else:
        ids = [i for i in (to_int(x) for x in form.getlist("company_id")) if i]
    back = str(form.get("back") or "/companies")
    if not back.startswith("/companies"):
        back = "/companies"
    try:
        result = deal_svc.bulk_assign(db, user, ids, manager_ids)
    except deal_svc.Invalid as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect(back)
    except deal_svc.Conflict:
        flash(request, "Пока вы назначали, часть сделок менял другой пользователь, и назначение остановилось на середине. Обновите список и повторите для оставшихся.", "error")
        return redirect(back)
    text = f"Назначено сделок: {result['assigned']} ({', '.join(result['managers'])})."
    if result["created"]:
        text += f" Из них создано новых: {result['created']}."
    flash(request, text)
    return redirect(back)


def _new_ctx(db: Session, user: User, data: dict, error: str | None = None, dup: dict | None = None) -> dict:
    return {"data": data, "error": error, "dup": dup, "pipelines": _pipelines(db), "managers": assignable_users(db, user)}


@router.get("/companies/new")
def new_company(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "company_new.html", _new_ctx(db, user, {}))


@router.post("/companies/new")
def create_company(request: Request, form: FormData = Depends(form_data), user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    data = {k: str(v) for k, v in form.items()}
    try:
        company = svc.create_company(db, data, user)
        pipeline_id, owner_id = importer.resolve_target(db, user, to_int(data.get("pipeline_id")), to_int(data.get("owner_id")))
        pipeline = db.get(Pipeline, pipeline_id) if pipeline_id else None
        if pipeline is not None:
            deal_svc.create_deal(db, company, pipeline, db.get(User, owner_id) if owner_id else None, user)
        db.commit()
    except (svc.Invalid, deal_svc.Invalid, importer.ImportError_) as exc:
        db.rollback()
        dup = _duplicate(db, user, getattr(exc, "company_id", None))
        return render(request, "company_new.html", _new_ctx(db, user, data, str(exc), dup), 400)
    flash(request, "Компания добавлена.")
    return redirect(f"/companies/{company.id}")


def _card(request: Request, db: Session, user: User, company: Company, error: str | None = None, status: int = 200,
          dup: dict | None = None):
    deals = list(db.scalars(scope_deals(
        select(Deal).where(Deal.company_id == company.id).options(
            selectinload(Deal.stage), selectinload(Deal.owner), selectinload(Deal.pipeline)).order_by(Deal.id.desc()),
        user,
    )))
    hist = select(Activity).where(Activity.company_id == company.id).options(selectinload(Activity.user))
    if not user.is_owner:
        # История по компании и по видимым сделкам, но не по чужим.
        hist = hist.where(or_(Activity.deal_id.is_(None), Activity.deal_id.in_([d.id for d in deals] or [0])))
    history = list(db.scalars(hist.order_by(Activity.created_at.desc(), Activity.id.desc()).limit(300)))
    return render(request, "company.html", {
        "company": company, "deals": deals, "history": history, "error": error, "dup": dup,
        "pipelines": _pipelines(db) if user.is_owner else [], "managers": assignable_users(db, user),
    }, status)


@router.get("/companies/{company_id}")
def company_card(request: Request, company_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _card(request, db, user, get_company(db, user, company_id))


@router.post("/companies/{company_id}/edit")
def edit_company(request: Request, company_id: int, form: FormData = Depends(form_data),
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    company = get_company(db, user, company_id)
    try:
        svc.update_company(db, company, {k: str(v) for k, v in form.items()}, user)
    except svc.Invalid as exc:
        db.rollback()
        dup = _duplicate(db, user, exc.company_id)
        return _card(request, db, user, get_company(db, user, company_id), str(exc), 400, dup)
    flash(request, "Карточка сохранена.")
    return redirect(f"/companies/{company_id}")


@router.post("/companies/{company_id}/delete")
def delete_company(request: Request, company_id: int, user: User = Depends(owner_user), db: Session = Depends(get_db)):
    company = get_company(db, user, company_id)
    name = company.name
    svc.delete_company(db, company, user)
    flash(request, f"Компания «{name}» удалена вместе со сделками и историей. Запись об этом есть в журнале исправлений.", "warn")
    return redirect("/companies")


@router.post("/companies/{company_id}/contacts")
def add_contact(request: Request, company_id: int, form: FormData = Depends(form_data),
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    company = get_company(db, user, company_id)
    try:
        svc.add_contact(db, company, {k: str(v) for k, v in form.items()})
        flash(request, "Контактное лицо добавлено.")
    except svc.Invalid as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return redirect(f"/companies/{company_id}#contacts")


@router.post("/companies/{company_id}/contacts/{contact_id}/delete")
def delete_contact(request: Request, company_id: int, contact_id: int, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    company = get_company(db, user, company_id)
    contact = db.get(Contact, contact_id)
    if contact is None or contact.company_id != company.id:
        raise NotFound()
    db.delete(contact)
    db.commit()
    flash(request, "Контактное лицо удалено.")
    return redirect(f"/companies/{company_id}#contacts")


@router.post("/companies/{company_id}/links")
def add_link(request: Request, company_id: int, form: FormData = Depends(form_data),
             user: User = Depends(current_user), db: Session = Depends(get_db)):
    company = get_company(db, user, company_id)
    try:
        svc.add_link(db, company, str(form.get("url", "")))
    except svc.Invalid as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return redirect(f"/companies/{company_id}")


@router.post("/companies/{company_id}/links/{link_id}/delete")
def delete_link(request: Request, company_id: int, link_id: int, user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    company = get_company(db, user, company_id)
    link = db.get(CompanyLink, link_id)
    if link is None or link.company_id != company.id:
        raise NotFound()
    db.delete(link)
    db.commit()
    return redirect(f"/companies/{company_id}")


@router.post("/companies/{company_id}/deals")
def add_deal(request: Request, company_id: int, form: FormData = Depends(form_data),
             user: User = Depends(owner_user), db: Session = Depends(get_db)):
    company = get_company(db, user, company_id)
    pipeline = db.get(Pipeline, to_int(form.get("pipeline_id")) or 0)
    owner = db.get(User, to_int(form.get("owner_id")) or 0)
    try:
        if pipeline is None:
            raise deal_svc.Invalid("Выберите воронку.")
        deal = deal_svc.create_deal(db, company, pipeline, owner, user)
        db.commit()
    except deal_svc.Invalid as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect(f"/companies/{company_id}")
    return redirect(f"/deals/{deal.id}")
