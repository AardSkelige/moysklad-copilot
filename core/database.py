import enum

from sqlalchemy import (
    Column, DateTime, Integer, String, Boolean, Text, Enum as SAEnum, UniqueConstraint
)
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

from core import config

Base = declarative_base()
engine = create_async_engine(config.DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


class OperationType(str, enum.Enum):
    INCOME = 'INCOME'
    EXPENSE = 'EXPENSE'


class DocumentSubtype(str, enum.Enum):
    PAYMENT_IN = 'paymentin'   # Входящий платёж (безнал)
    CASH_IN = 'cashin'         # Приходный ордер (наличные)
    PAYMENT_OUT = 'paymentout'  # Исходящий платёж (безнал)
    CASH_OUT = 'cashout'        # Расходный ордер (наличные)


class ApplicableTo(str, enum.Enum):
    INCOME = 'INCOME'
    EXPENSE = 'EXPENSE'
    BOTH = 'BOTH'


class Category(Base):
    __tablename__ = 'categories'

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    group = Column(String, nullable=False, default='')
    applicable_to = Column(SAEnum(ApplicableTo), nullable=False, default=ApplicableTo.BOTH)
    is_active = Column(Boolean, default=True, nullable=False)
    is_system = Column(Boolean, default=False, nullable=False)
    sort_order = Column(Integer, default=0)
    moysklad_expense_item_href = Column(String, nullable=True)


class FindingStatus(str, enum.Enum):
    NEW = 'new'
    NOTIFIED = 'notified'
    ACKNOWLEDGED = 'acknowledged'
    IGNORED = 'ignored'
    FIXED = 'fixed'
    RESOLVED = 'resolved'   # проблема исчезла из данных сама


class Finding(Base):
    __tablename__ = 'findings'

    id = Column(Integer, primary_key=True)
    check_id = Column(String, nullable=False)
    section = Column(String, nullable=False)
    severity = Column(String, nullable=False)
    fingerprint = Column(String, nullable=False, unique=True, index=True)
    entity_type = Column(String, nullable=False)
    entity_id = Column(String, nullable=False, default='')
    entity_href = Column(String, nullable=False, default='')
    entity_name = Column(String, nullable=False, default='')
    title = Column(String, nullable=False)
    payload = Column(Text, nullable=False, default='{}')   # JSON: всё для explain/fix
    status = Column(SAEnum(FindingStatus), nullable=False, default=FindingStatus.NEW)
    first_seen_at = Column(DateTime, nullable=False)
    last_seen_at = Column(DateTime, nullable=False)
    notified_at = Column(DateTime, nullable=True)
    tg_message_id = Column(Integer, nullable=True)


class AuditRun(Base):
    __tablename__ = 'audit_runs'

    id = Column(Integer, primary_key=True)
    run_type = Column(String, nullable=False)   # 'incremental' | 'full'
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String, nullable=False, default='running')  # running | ok | error
    error_text = Column(Text, nullable=True)
    findings_new = Column(Integer, nullable=False, default=0)
    api_calls = Column(Integer, nullable=False, default=0)


class CommentReviewSeen(Base):
    """Сколько раз документ показывался в ревью комментариев.

    Документ попадается владельцу не больше двух раз подряд; счётчик обнуляется,
    если тексты документа с последнего показа изменились (state_hash)."""

    __tablename__ = 'comment_review_seen'
    __table_args__ = (UniqueConstraint('entity', 'entity_id'),)

    id = Column(Integer, primary_key=True)
    entity = Column(String, nullable=False)
    entity_id = Column(String, nullable=False)
    shown_count = Column(Integer, nullable=False, default=0)
    state_hash = Column(String, nullable=False, default='')
    last_shown_at = Column(DateTime, nullable=False)


class AuditMute(Base):
    __tablename__ = 'audit_mutes'

    id = Column(Integer, primary_key=True)
    check_id = Column(String, nullable=False)
    entity_id = Column(String, nullable=True)   # NULL = замьючена вся проверка
    created_at = Column(DateTime, nullable=False)


async def init_db():
    import os
    os.makedirs('data', exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Миграция: добавить is_system если колонки ещё нет
        try:
            await conn.execute(
                __import__('sqlalchemy').text(
                    'ALTER TABLE categories ADD COLUMN is_system BOOLEAN NOT NULL DEFAULT 0'
                )
            )
        except Exception:
            pass  # колонка уже существует
