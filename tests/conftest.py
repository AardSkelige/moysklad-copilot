"""Общие фикстуры для тестов"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from core.database import Base, Category, ApplicableTo


@pytest_asyncio.fixture
async def test_db():
    """In-memory SQLite база для тестов"""
    engine = create_async_engine('sqlite+aiosqlite:///:memory:', echo=False)
    async_session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session = async_session()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


@pytest_asyncio.fixture
async def sample_category(test_db):
    """Фикстура — одна категория расходов"""
    cat = Category(
        name='Тестовая категория',
        group='Тест',
        applicable_to=ApplicableTo.BOTH,
        is_active=True,
        sort_order=1,
    )
    test_db.add(cat)
    await test_db.commit()
    await test_db.refresh(cat)
    return cat
