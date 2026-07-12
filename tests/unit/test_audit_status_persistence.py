"""Регрессия: смена статуса находки кнопкой обязана сохраняться в БД.

Баг: expunge до flush терял UPDATE — «Ок» не закрывал находку,
и разбор по категориям показывал её снова.
"""

from contextlib import asynccontextmanager
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.database import Base, Finding, FindingStatus

pytestmark = [pytest.mark.asyncio, pytest.mark.unit, pytest.mark.audit]


@pytest_asyncio.fixture
async def handlers_env(monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:', echo=False)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def scope():
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    import handlers.audit.findings as findings_module
    monkeypatch.setattr(findings_module, 'session_scope', scope)

    now = datetime.now()
    async with factory() as s:
        s.add(Finding(
            check_id='fifo_vs_last_supply', section='Товары', severity='critical',
            fingerprint='fp-tea', entity_type='product', entity_id='p1',
            entity_href='', entity_name='Экстракт зеленого чая',
            title='Себестоимость расходится', payload='{}',
            status=FindingStatus.NOTIFIED, first_seen_at=now, last_seen_at=now,
        ))
        await s.commit()

    yield findings_module, factory
    await engine.dispose()


class TestStatusPersistence:
    async def test_ok_button_persists_ignored(self, handlers_env):
        findings_module, factory = handlers_env
        result = await findings_module._set_status(1, FindingStatus.IGNORED)
        assert result is not None
        # читаем СВЕЖЕЙ сессией — изменение обязано быть в БД
        async with factory() as s:
            row = (await s.execute(select(Finding).where(Finding.id == 1))).scalar_one()
        assert row.status == FindingStatus.IGNORED

    async def test_closed_finding_leaves_pending(self, handlers_env):
        findings_module, factory = handlers_env
        await findings_module._set_status(1, FindingStatus.IGNORED)
        pending = (FindingStatus.NEW, FindingStatus.NOTIFIED)
        async with factory() as s:
            rows = (await s.execute(
                select(Finding).where(Finding.status.in_(pending))
            )).scalars().all()
        assert rows == []   # разбор по категориям не покажет её снова
