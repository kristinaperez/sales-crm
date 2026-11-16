"""Начальные данные: руководитель, готовая воронка, демоданные; управление воронками."""
from __future__ import annotations

import os
import secrets
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import ROLE_HEAD, ROLE_MANAGER, STAGE_LOST, STAGE_OPEN, STAGE_WON, Deal, Pipeline, Stage, User
from ..security import hash_password

DEFAULT_PIPELINE = "Холодные продажи"
DEFAULT_STAGES = [
    ("Новая", STAGE_OPEN, None),
    ("Готова к контакту", STAGE_OPEN, None),
    ("Первый контакт", STAGE_OPEN, "first_contact"),
    ("Есть ответ", STAGE_OPEN, "reply"),
    ("Демо", STAGE_OPEN, "demo"),
    ("Предложение", STAGE_OPEN, None),
    ("Оплачено", STAGE_WON, "paid"),
    ("Закрыто без продажи", STAGE_LOST, None),
]


class Invalid(Exception):
    pass


def ensure_default_pipeline(db: Session) -> Pipeline:
    pipeline = db.scalar(select(Pipeline).order_by(Pipeline.is_default.desc(), Pipeline.id))
    if pipeline is not None:
        return pipeline
    pipeline = Pipeline(name=DEFAULT_PIPELINE, is_default=True)
    for pos, (name, kind, metric) in enumerate(DEFAULT_STAGES):
        pipeline.stages.append(Stage(name=name, position=pos, kind=kind, metric_key=metric))
    db.add(pipeline)
    db.flush()
    return pipeline


def ensure_head(db: Session) -> tuple[User, str | None]:
    """Создаёт руководителя, если его ещё нет. Возвращает (пользователь, сгенерированный пароль или None)."""
    head = db.scalar(select(User).where(User.role == ROLE_HEAD).order_by(User.id))
    if head is not None:
        return head, None
    login = os.environ.get("ADMIN_LOGIN", "head")
    given = os.environ.get("ADMIN_PASSWORD")
    password = given or secrets.token_urlsafe(9)
    head = User(login=login, name=os.environ.get("ADMIN_NAME", "Руководитель"), role=ROLE_HEAD, password_hash=hash_password(password))
    db.add(head)
    db.flush()
    # Пароль возвращается для печати, только если он был сгенерирован, а не задан в окружении.
    return head, (None if given else password)


def create_user(db: Session, login: str, name: str, password: str, role: str = ROLE_MANAGER) -> User:
    login, name = (login or "").strip().lower(), (name or "").strip()
    if not login or not name:
        raise Invalid("Укажите логин и имя.")
    if len(password or "") < 8:
        raise Invalid("Пароль должен быть не короче 8 символов.")
    if role not in (ROLE_HEAD, ROLE_MANAGER):
        raise Invalid("Неизвестная роль.")
    if db.scalar(select(User.id).where(func.lower(User.login) == login)):
        raise Invalid("Такой логин уже занят.")
    user = User(login=login, name=name[:200], role=role, password_hash=hash_password(password))
    db.add(user)
    db.flush()
    return user


# --- Воронки -----------------------------------------------------------------------------


def create_pipeline(db: Session, name: str) -> Pipeline:
    name = (name or "").strip()
    if not name:
        raise Invalid("Укажите название воронки.")
    if db.scalar(select(Pipeline.id).where(Pipeline.name == name)):
        raise Invalid("Воронка с таким названием уже есть.")
    pipeline = Pipeline(name=name[:200])
    for pos, (stage, kind, metric) in enumerate(
        [("Новая", STAGE_OPEN, None), ("Оплачено", STAGE_WON, "paid"), ("Закрыто без продажи", STAGE_LOST, None)]
    ):
        pipeline.stages.append(Stage(name=stage, position=pos, kind=kind, metric_key=metric))
    db.add(pipeline)
    db.flush()
    return pipeline


def _renumber(pipeline: Pipeline) -> None:
    for pos, stage in enumerate(sorted(pipeline.stages, key=lambda s: s.position)):
        stage.position = pos


def add_stage(db: Session, pipeline: Pipeline, name: str) -> Stage:
    name = (name or "").strip()
    if not name:
        raise Invalid("Укажите название этапа.")
    # Новый этап встаёт перед завершающими.
    open_stages = [s for s in pipeline.stages if s.kind == STAGE_OPEN]
    position = (open_stages[-1].position + 1) if open_stages else 0
    for stage in pipeline.stages:
        if stage.position >= position:
            stage.position += 1
    stage = Stage(pipeline_id=pipeline.id, name=name[:200], position=position, kind=STAGE_OPEN)
    pipeline.stages.append(stage)
    db.flush()
    _renumber(pipeline)
    return stage


def update_stage(db: Session, stage: Stage, name: str, kind: str, metric_key: str | None) -> None:
    from ..models import METRICS, STAGE_KINDS

    name = (name or "").strip()
    if not name:
        raise Invalid("У этапа должно быть название.")
    if kind not in STAGE_KINDS:
        raise Invalid("Неизвестный тип этапа.")
    if kind != stage.kind:
        if db.scalar(select(func.count()).select_from(Deal).where(Deal.stage_id == stage.id)):
            raise Invalid("На этапе есть сделки — сначала перенесите их, потом меняйте тип этапа.")
        if stage.pipeline.first_stage.id == stage.id and kind != STAGE_OPEN:
            raise Invalid("Первый этап воронки должен быть рабочим.")
    stage.name, stage.kind = name[:200], kind
    stage.metric_key = metric_key if metric_key in METRICS else None


def move_stage(db: Session, stage: Stage, direction: int) -> None:
    stages = sorted(stage.pipeline.stages, key=lambda s: s.position)
    idx = stages.index(stage)
    other = idx + direction
    if not 0 <= other < len(stages):
        return
    if other == 0 and stage.kind != STAGE_OPEN or (idx == 0 and stages[other].kind != STAGE_OPEN):
        raise Invalid("Первый этап воронки должен быть рабочим.")
    stages[idx].position, stages[other].position = stages[other].position, stages[idx].position
    db.flush()


def delete_stage(db: Session, stage: Stage) -> None:
    pipeline = stage.pipeline
    if db.scalar(select(func.count()).select_from(Deal).where(Deal.stage_id == stage.id)):
        raise Invalid("На этапе есть сделки — сначала перенесите их на другой этап.")
    if len(pipeline.stages) <= 1 or pipeline.first_stage.id == stage.id:
        raise Invalid("Первый этап воронки удалить нельзя.")
    pipeline.stages.remove(stage)
    db.flush()
    _renumber(pipeline)


# --- Демоданные ---------------------------------------------------------------------------

DEMO_PASSWORD = "demo-crm-2026"
DEMO_LIST = """\
| Компания | Город | Сайт | Телефон | Ниша |
|---|---|---|---|---|
| Клиника «Улыбка» | Тверь | ulybka-tver.example | +7 4822 33-44-55 | стоматология |
| Школа «Лингва» | Пермь | lingva-school.example | +7 342 200-10-20 | онлайн-школа |
| Автосервис «Гараж 77» | Москва | garage77.example | +7 495 123-45-67 | автосервис |
| Студия «Бронза» | Самара | bronza-studio.example | +7 846 300-40-50 | салон красоты |
| Мебель Про | Тула | mebel-pro.example | +7 4872 70-80-90 | мебель на заказ |
| Окна Сити | Курск | okna-city.example | +7 4712 55-66-77 | окна и двери |
| Фитнес-клуб «Атлет» | Екатеринбург | atlet-fit.example | +7 343 555-66-77 | фитнес |
| Цветы «Ромашка» | Казань | romashka-flowers.example | +7 843 200-11-22 | доставка цветов |
"""


def seed_demo(db: Session) -> bool:
    """Два менеджера и восемь компаний со сделками на разных этапах. Возвращает False, если база не пустая."""
    from ..models import Company
    from . import deals as deal_service
    from . import importer
    from .worklists import today

    if db.scalar(select(func.count()).select_from(Company)):
        return False
    head, _ = ensure_head(db)
    pipeline = ensure_default_pipeline(db)
    managers = []
    for login, name in (("anna", "Анна Смирнова"), ("boris", "Борис Орлов")):
        user = db.scalar(select(User).where(User.login == login)) or create_user(db, login, name, DEMO_PASSWORD)
        managers.append(user)
    db.commit()

    batch = importer.create_text_batch(db, head, "text", "демоданные", DEMO_LIST)
    importer.confirm(db, batch.id, head, pipeline.id, None)
    ids = list(db.scalars(select(Company.id).order_by(Company.id)))
    deal_service.bulk_assign(db, head, ids, [m.id for m in managers])

    stages = {s.name: s for s in pipeline.stages}
    plan = ["Готова к контакту", "Первый контакт", "Есть ответ", "Демо", "Первый контакт", "Новая", "Предложение", "Есть ответ"]
    order = [s.name for s in pipeline.stages]
    for deal, target in zip(db.scalars(select(Deal).order_by(Deal.id)).all(), plan):
        owner = deal.owner or head
        for name in order[1 : order.index(target) + 1]:
            if name == "Первый контакт":
                deal_service.log_touch(
                    db, deal, owner, "message", "Написали в мессенджер, отправили короткое предложение",
                    stage_id=stages[name].id, expected_version=deal.version,
                )
            else:
                deal_service.change_stage(db, deal, stages[name].id, owner, deal.version)
        if target in ("Первый контакт", "Есть ответ"):
            deal_service.add_task(db, deal, owner, "Повторный контакт", today() + timedelta(days=(deal.id % 3) - 1))
            db.commit()
        elif target in ("Демо", "Предложение"):
            deal_service.set_next_step(db, deal, "Созвон по итогам демо", today() + timedelta(days=2), owner, deal.version)
    return True
