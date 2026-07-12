"""Планировщик аудита: инкрементальный прогон каждые N минут + полный скан ночью."""

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core import config
from core.logger import logger
from services.audit.analyst import AuditAnalyst
from services.audit.checks import build_registry
from services.audit.notifier import AuditNotifier
from services.audit.runner import AuditRunner


async def run_audit(bot: Bot, run_type: str, daily_summary: bool = False,
                    deliver: bool = True) -> int:
    """Прогон аудита. deliver=False — находки только сохраняются, без сообщения
    по каждой (для ручного запуска: владелец рядом и сам пойдёт разбирать).
    Возвращает число новых находок."""
    analyst = AuditAnalyst() if config.DEEPSEEK_API_KEY else None
    runner = AuditRunner(build_registry(), analyst=analyst)
    notifier = AuditNotifier(bot)
    try:
        new_findings = await runner.run(run_type)
        if deliver:
            await notifier.deliver(new_findings)
        if daily_summary:
            await notifier.send_daily_summary(len(new_findings))
        return len(new_findings)
    except Exception as e:
        logger.exception(f'[audit] {run_type} прогон упал')
        await notifier.notify_error(str(e))
        return 0


def setup_audit_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_audit, 'interval',
        minutes=config.AUDIT_INCREMENTAL_MINUTES,
        args=[bot, 'incremental'],
        max_instances=1, coalesce=True,
        id='audit_incremental',
    )
    scheduler.add_job(
        run_audit, 'cron',
        hour=config.AUDIT_FULL_HOUR, minute=0,
        args=[bot, 'full', True],   # ночной скан всегда отчитывается сводкой
        max_instances=1, coalesce=True,
        id='audit_full',
    )
    scheduler.start()
    logger.info(
        f'Планировщик аудита запущен: инкрементально каждые {config.AUDIT_INCREMENTAL_MINUTES} мин, '
        f'полный скан в {config.AUDIT_FULL_HOUR}:00'
    )
    return scheduler
