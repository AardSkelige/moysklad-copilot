from aiogram import Router

from handlers.audit.comments import router as comments_router
from handlers.audit.dialog import router as dialog_router
from handlers.audit.findings import router as findings_router

audit_router = Router()
audit_router.include_router(comments_router)
audit_router.include_router(dialog_router)
audit_router.include_router(findings_router)

__all__ = ['audit_router']
