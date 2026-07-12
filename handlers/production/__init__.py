from aiogram import Router

from handlers.production.entry import router as entry_router

production_router = Router()
production_router.include_router(entry_router)

__all__ = ['production_router']
