from aiogram import Router

from handlers.finance.menu import router as menu_router
from handlers.finance.add_operation import router as add_router
from handlers.finance.view_operations import router as view_router
from handlers.finance.categories import router as categories_router
from handlers.finance.export import router as export_router

finance_router = Router()
finance_router.include_router(menu_router)
finance_router.include_router(add_router)
finance_router.include_router(view_router)
finance_router.include_router(categories_router)
finance_router.include_router(export_router)

__all__ = ['finance_router']
