"""Модель данных CRM: пользователи, компании, контакты, сделки, воронки, задачи, история, импорт."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """Текущее время UTC без tzinfo (одинаково хранится в SQLite и PostgreSQL)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


ROLE_OWNER = "owner"  # владелец: видит и может исправить всё
ROLE_LEAD = "lead"  # руководитель группы: сделки своей группы
ROLE_MANAGER = "manager"  # менеджер: только свои сделки
ROLES = {ROLE_OWNER: "Владелец", ROLE_LEAD: "Руководитель группы", ROLE_MANAGER: "Менеджер"}


class Team(Base):
    """Группа менеджеров. Руководитель группы — сотрудник с ролью lead в этой группе."""

    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20), default=ROLE_MANAGER)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    team: Mapped[Team | None] = relationship()

    @property
    def is_owner(self) -> bool:
        return self.role == ROLE_OWNER

    @property
    def is_lead(self) -> bool:
        return self.role == ROLE_LEAD

    @property
    def is_boss(self) -> bool:
        """Владелец или руководитель группы — видит не только свои сделки."""
        return self.role in (ROLE_OWNER, ROLE_LEAD)

    @property
    def role_label(self) -> str:
        return ROLES.get(self.role, self.role)


COMPANY_OK = "ok"
COMPANY_REVIEW = "needs_review"


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    name_norm: Mapped[str] = mapped_column(String(300), default="", index=True)
    website: Mapped[str | None] = mapped_column(String(500))
    # Нормализованный адрес официального сайта — ключ поиска дублей.
    website_norm: Mapped[str | None] = mapped_column(String(300), unique=True)
    city: Mapped[str | None] = mapped_column(String(200))
    city_norm: Mapped[str] = mapped_column(String(200), default="", index=True)
    industry: Mapped[str | None] = mapped_column(String(200), index=True)
    phone: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(200))
    messenger_url: Mapped[str | None] = mapped_column(String(500))
    source: Mapped[str | None] = mapped_column(String(300))
    source_fragment: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=COMPANY_OK, index=True)
    import_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_batches.id", ondelete="SET NULL")
    )
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    links: Mapped[list[CompanyLink]] = relationship(
        back_populates="company", cascade="all, delete-orphan", order_by="CompanyLink.id"
    )
    contacts: Mapped[list[Contact]] = relationship(
        back_populates="company", cascade="all, delete-orphan", order_by="Contact.id"
    )
    deals: Mapped[list[Deal]] = relationship(back_populates="company", order_by="Deal.id")


LINK_KINDS = {
    "social": "Соцсеть",
    "map": "Карты",
    "catalog": "Каталог",
    "messenger": "Мессенджер",
    "other": "Ссылка",
}


class CompanyLink(Base):
    """Ссылки на каталоги, соцсети и карты: хранятся как данные, сайтом компании не считаются."""

    __tablename__ = "company_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(1000))
    url_norm: Mapped[str] = mapped_column(String(1000), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="other")

    company: Mapped[Company] = relationship(back_populates="links")

    @property
    def kind_label(self) -> str:
        return LINK_KINDS.get(self.kind, "Ссылка")


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    position: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(200))
    messenger: Mapped[str | None] = mapped_column(String(300))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    company: Mapped[Company] = relationship(back_populates="contacts")


STAGE_OPEN = "open"
STAGE_WON = "won"
STAGE_LOST = "lost"
STAGE_KINDS = {STAGE_OPEN: "В работе", STAGE_WON: "Успех", STAGE_LOST: "Отказ"}

# Какие этапы считаются в отчёте руководителя.
METRICS = {
    "first_contact": "Первые контакты",
    "reply": "Ответы",
    "demo": "Демо",
    "paid": "Оплаты",
}


class Pipeline(Base):
    __tablename__ = "pipelines"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    stages: Mapped[list[Stage]] = relationship(
        back_populates="pipeline", cascade="all, delete-orphan", order_by="Stage.position"
    )

    @property
    def first_stage(self) -> Stage:
        return self.stages[0]


class Stage(Base):
    __tablename__ = "stages"

    id: Mapped[int] = mapped_column(primary_key=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    position: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(10), default=STAGE_OPEN)
    metric_key: Mapped[str | None] = mapped_column(String(30))

    pipeline: Mapped[Pipeline] = relationship(back_populates="stages")

    @property
    def is_closed(self) -> bool:
        return self.kind in (STAGE_WON, STAGE_LOST)


class Deal(Base):
    __tablename__ = "deals"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"), index=True)
    stage_id: Mapped[int] = mapped_column(ForeignKey("stages.id"), index=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    next_step: Mapped[str | None] = mapped_column(String(500))
    next_step_due: Mapped[date | None] = mapped_column(Date, index=True)
    lost_reason: Mapped[str | None] = mapped_column(String(500))
    # Счётчик версий: защита от одновременной смены этапа или ответственного.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)

    company: Mapped[Company] = relationship(back_populates="deals")
    pipeline: Mapped[Pipeline] = relationship()
    stage: Mapped[Stage] = relationship()
    owner: Mapped[User | None] = relationship()

    __mapper_args__ = {"version_id_col": version}


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id", ondelete="CASCADE"), index=True)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(20), default="followup")
    due_date: Mapped[date] = mapped_column(Date, index=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    deal: Mapped[Deal] = relationship()
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id])


PAY_PENDING = "pending"
PAY_CONFIRMED = "confirmed"
PAY_REJECTED = "rejected"
PAYMENT_STATUSES = {PAY_PENDING: "Ждёт подтверждения", PAY_CONFIRMED: "Подтверждена", PAY_REJECTED: "Отклонена"}


class Payment(Base):
    """Оплата по сделке. В рейтинг идут только подтверждённые оплаты."""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id", ondelete="CASCADE"), index=True)
    # Сумма в копейках (целое число) — без ошибок округления.
    amount_minor: Mapped[int] = mapped_column(Integer)
    paid_on: Mapped[date] = mapped_column(Date, index=True)
    note: Mapped[str | None] = mapped_column(String(500))
    # Кому оплата засчитывается в рейтинге: ответственный за сделку в момент внесения.
    credited_to_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default=PAY_PENDING, index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    confirmed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    deal: Mapped[Deal] = relationship()
    credited_to: Mapped[User | None] = relationship(foreign_keys=[credited_to_id])
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])
    confirmed_by: Mapped[User | None] = relationship(foreign_keys=[confirmed_by_id])

    @property
    def status_label(self) -> str:
        return PAYMENT_STATUSES.get(self.status, self.status)


class AuditLog(Base):
    """Журнал исправлений владельца и изменений сотрудников: что было и что стало."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    action: Mapped[str] = mapped_column(String(40))
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text)

    user: Mapped[User | None] = relationship()


ACTIVITY_KINDS = {
    "call": "Звонок",
    "email": "Письмо",
    "message": "Сообщение",
    "note": "Заметка",
    "stage_change": "Смена этапа",
    "owner_change": "Смена ответственного",
    "task": "Задача",
    "payment": "Оплата",
    "import": "Импорт",
    "system": "Событие",
}
MANUAL_ACTIVITY_KINDS = ("message", "call", "email", "note")


class Activity(Base):
    """История по компании и сделке. Записи только добавляются."""

    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    # Ответственный за сделку в момент события — по нему считается отчёт.
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    body: Mapped[str | None] = mapped_column(Text)
    from_stage_id: Mapped[int | None] = mapped_column(ForeignKey("stages.id", ondelete="SET NULL"))
    to_stage_id: Mapped[int | None] = mapped_column(ForeignKey("stages.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    user: Mapped[User | None] = relationship(foreign_keys=[user_id])

    @property
    def kind_label(self) -> str:
        return ACTIVITY_KINDS.get(self.kind, self.kind)


BATCH_MAPPING = "mapping"
BATCH_PREVIEW = "preview"
BATCH_CONFIRMING = "confirming"
BATCH_CONFIRMED = "confirmed"
BATCH_CANCELLED = "cancelled"

SOURCE_KINDS = {
    "text": "Вставка текста",
    "txt": "TXT-файл",
    "csv": "CSV-файл",
    "sheets": "Google Таблицы",
}


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    source_kind: Mapped[str] = mapped_column(String(20))
    source_name: Mapped[str | None] = mapped_column(String(300))
    raw_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=BATCH_PREVIEW)
    pipeline_id: Mapped[int | None] = mapped_column(ForeignKey("pipelines.id"))
    # Кому назначаются созданные сделки (нужно, чтобы продолжить прерванный импорт).
    deal_owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_count: Mapped[int] = mapped_column(Integer, default=0)
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    rows: Mapped[list[ImportRow]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="ImportRow.row_no"
    )

    @property
    def source_label(self) -> str:
        label = SOURCE_KINDS.get(self.source_kind, self.source_kind)
        return f"{label}: {self.source_name}" if self.source_name else label


ROW_NEW = "new"
ROW_DUPLICATE = "duplicate"
ROW_REVIEW = "review"
ROW_STATUSES = {ROW_NEW: "Новая компания", ROW_DUPLICATE: "Возможный дубль", ROW_REVIEW: "Нужно проверить"}

# Причина совпадения. «Жёсткие» совпадения нельзя импортировать повторно.
MATCH_WEBSITE = "website"
MATCH_LINK = "link"
MATCH_NAME_CITY = "name_city"
MATCH_BATCH_WEBSITE = "batch_website"
MATCH_BATCH_LINK = "batch_link"
MATCH_BATCH_NAME = "batch_name"
MATCH_EXCLUDED = "excluded_before"  # эту же строку уже исключали в прошлом импорте
HARD_MATCHES = (MATCH_WEBSITE, MATCH_LINK, MATCH_BATCH_WEBSITE, MATCH_BATCH_LINK)


class ImportRow(Base):
    __tablename__ = "import_rows"
    __table_args__ = (
        UniqueConstraint("batch_id", "row_no"),
        Index("ix_import_rows_batch_status", "batch_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    row_no: Mapped[int] = mapped_column(Integer)
    raw: Mapped[str] = mapped_column(Text, default="")
    # Отпечаток исходной строки: по нему узнаём строки, исключённые в прошлых импортах.
    raw_hash: Mapped[str] = mapped_column(String(40), default="", index=True)

    name: Mapped[str] = mapped_column(String(300), default="")
    website: Mapped[str] = mapped_column(String(500), default="")
    city: Mapped[str] = mapped_column(String(200), default="")
    industry: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(100), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    messenger_url: Mapped[str] = mapped_column(String(500), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    links_json: Mapped[str] = mapped_column(Text, default="[]")

    status: Mapped[str] = mapped_column(String(20), default=ROW_NEW)
    match_kind: Mapped[str | None] = mapped_column(String(20))
    match_company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))
    match_row_no: Mapped[int | None] = mapped_column(Integer)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    # Пользователь явно решил создать карточку, несмотря на подсказку «название + город».
    force_create: Mapped[bool] = mapped_column(Boolean, default=False)
    # Итог после подтверждения: created / review / skipped / excluded.
    result: Mapped[str | None] = mapped_column(String(20))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))

    batch: Mapped[ImportBatch] = relationship(back_populates="rows")
    match_company: Mapped[Company | None] = relationship(foreign_keys=[match_company_id])

    @property
    def links(self) -> list[str]:
        try:
            return list(json.loads(self.links_json or "[]"))
        except ValueError:
            return []

    @links.setter
    def links(self, value: list[str]) -> None:
        self.links_json = json.dumps(value, ensure_ascii=False)

    @property
    def status_label(self) -> str:
        return ROW_STATUSES.get(self.status, self.status)

    @property
    def is_hard_duplicate(self) -> bool:
        return self.status == ROW_DUPLICATE and self.match_kind in HARD_MATCHES

    @property
    def will_create(self) -> bool:
        if self.excluded:
            return False
        if self.status == ROW_DUPLICATE:
            return self.force_create and not self.is_hard_duplicate
        return True
