from aiogram import Router

from core import config
from handlers.common import router as common_router
from handlers.finance import finance_router

main_router = Router()
main_router.include_router(common_router)
main_router.include_router(finance_router)

if config.AGENT_ENABLED:
    from handlers.production import production_router
    main_router.include_router(production_router)

if config.AUDIT_ENABLED:
    from handlers.audit import audit_router
    main_router.include_router(audit_router)

__all__ = ['main_router']
