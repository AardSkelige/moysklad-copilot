from aiogram.filters import Filter
from aiogram.types import Message, CallbackQuery

from core import config


class IsAdminFilter(Filter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user.id in config.FINANCE_ADMIN_IDS


class IsProductionUserFilter(Filter):
    """Доступ к функциям производственного агента."""
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user.id in config.PRODUCTION_USERS


class IsAuditOwnerFilter(Filter):
    """Уведомления и управление аудитом учёта — только владелец."""
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user.id == config.AUDIT_OWNER_TELEGRAM_ID


class IsBotUserFilter(Filter):
    """Любой авторизованный пользователь — финансовый админ ИЛИ производственный.
    Используется в общих хэндлерах (/start, главное меню)."""
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        uid = event.from_user.id
        return uid in config.FINANCE_ADMIN_IDS or uid in config.PRODUCTION_USERS
