from contextlib import asynccontextmanager

from core.database import SessionLocal
from core.logger import logger


@asynccontextmanager
async def session_scope():
    session = SessionLocal()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("Ошибка при работе с базой данных, откат транзакции")
        raise
    finally:
        await session.close()
