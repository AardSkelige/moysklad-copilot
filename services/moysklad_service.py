"""Сервис синхронизации с МойСклад"""

from sqlalchemy.ext.asyncio import AsyncSession

from core.database import OperationType
from core.logger import logger
from integrations.moysklad import MoySkladClient
from services.category_service import sync_expense_categories


async def sync_delete(moysklad_id: str, op_type: OperationType):
    """
    Удаляет платеж из МойСклад.
    """
    try:
        client = MoySkladClient()
        await client.delete_payment(moysklad_id, op_type)

        logger.info(f'Successfully deleted payment {moysklad_id} from MoySklad')

    except Exception as e:
        logger.error(f'Failed to delete payment {moysklad_id} from MoySklad: {e}')


async def sync_expense_categories_from_moysklad(session: AsyncSession) -> dict:
    """
    1. Получить все expenseItem из МойСклад
    2. Синхронизировать с локальными категориями
    3. Вернуть статистику для показа пользователю
    """
    client = MoySkladClient()
    ms_items = await client.fetch_expense_items()
    stats = await sync_expense_categories(session, ms_items)
    stats['ms_count'] = len(ms_items)
    await session.commit()
    logger.info(
        f'Synced expense categories from MoySklad: '
        f'created={stats["created"]}, updated={stats["updated"]}, deleted={stats["deleted"]}'
    )
    return stats
