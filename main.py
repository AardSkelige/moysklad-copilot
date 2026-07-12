import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from aiogram.types import BotCommand, BotCommandScopeChat

from core import config
from core.database import init_db
from core.logger import logger
from handlers import main_router
from services.moysklad_service import sync_expense_categories_from_moysklad
from shared import session_scope


async def setup_bot_commands(bot: Bot):
    """Меню команд (кнопка «/» в Telegram). Владельцу аудита — расширенный набор."""
    base = [
        BotCommand(command='start', description='Главное меню'),
        BotCommand(command='cancel', description='Выйти из текущего режима'),
    ]
    try:
        await bot.set_my_commands(base)
        if config.AUDIT_ENABLED:
            await bot.set_my_commands(
                base + [
                    BotCommand(command='audit', description='🔍 Проверить учёт сейчас'),
                    BotCommand(command='audit_findings', description='📋 Неразобранные находки'),
                    BotCommand(command='audit_status', description='📊 Статус проверок'),
                ],
                scope=BotCommandScopeChat(chat_id=config.AUDIT_OWNER_TELEGRAM_ID),
            )
    except Exception as e:
        logger.warning(f'Не удалось установить меню команд: {e}')


async def main():
    await init_db()
    logger.info("База данных инициализирована")

    try:
        async with session_scope() as session:
            stats = await sync_expense_categories_from_moysklad(session)
        logger.info(
            f"Автосинк категорий при старте: "
            f"создано={stats['created']}, обновлено={stats['updated']}, удалено={stats['deleted']}"
        )
    except Exception as e:
        logger.warning(f"Автосинк категорий при старте не удался (продолжаем): {e}")

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    dp.include_router(main_router)

    if config.AUDIT_ENABLED:
        from services.audit.scheduler import setup_audit_scheduler
        setup_audit_scheduler(bot)

    await setup_bot_commands(bot)

    logger.info("Бот запущен и готов к работе!")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
