from html import escape

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core import config
from core.logger import logger
from integrations.moysklad_audit import MoySkladAuditClient
from integrations.moysklad_base import new_session
from services.low_stock import find_low_stock, format_alert


async def send_stock_alerts(bot: Bot, client: MoySkladAuditClient | None = None) -> int:
    if not config.STOCK_ALERT_RECIPIENT_IDS:
        return 0
    client = client or MoySkladAuditClient()
    try:
        async with new_session() as session:
            low = await find_low_stock(client, session)
    except Exception as failure:
        logger.exception('[stock-alerts] МойСклад не отдал остатки, сводка сегодня не ушла')
        await _tell_owner(bot, f'⚠️ Сводка «ниже неснижаемого остатка» сегодня не ушла: МойСклад не ответил ({escape(str(failure)[:200])}).')
        return 0
    if not low:
        return 0
    messages = format_alert(low)
    for chat_id in config.STOCK_ALERT_RECIPIENT_IDS:
        for text in messages:
            try:
                await bot.send_message(chat_id, text)
            except Exception as failure:
                logger.exception(f'[stock-alerts] не удалось отправить {chat_id}')
                await _tell_owner(bot, f'⚠️ Сводка по остаткам не дошла до {chat_id}: {escape(str(failure)[:200])}. '
                                       'Возможно, человек не нажимал /start или заблокировал бота.')
                break
    return len(low)


async def _tell_owner(bot: Bot, text: str) -> None:
    if not config.AUDIT_OWNER_TELEGRAM_ID:
        return
    try:
        await bot.send_message(config.AUDIT_OWNER_TELEGRAM_ID, text)
    except Exception:
        logger.exception('[stock-alerts] не удалось предупредить владельца')


def setup_stock_alerts(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_stock_alerts, 'cron', hour=config.STOCK_ALERT_HOUR, minute=0, args=[bot],
                      max_instances=1, coalesce=True, id='stock_alerts')
    scheduler.start()
    return scheduler
