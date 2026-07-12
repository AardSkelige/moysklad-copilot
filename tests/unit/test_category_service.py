"""Тесты сервиса категорий"""

import pytest
import pytest_asyncio

from core.database import ApplicableTo, OperationType
from services.category_service import (
    get_active_categories, create_category, get_category_by_id,
    rename_category, sync_expense_categories,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class TestGetActiveCategories:
    """Тесты получения активных категорий"""

    pytestmark = [pytest.mark.asyncio, pytest.mark.unit, pytest.mark.finance]

    async def test_empty(self, test_db):
        cats = await get_active_categories(test_db)
        assert cats == []

    async def test_returns_active(self, test_db, sample_category):
        cats = await get_active_categories(test_db)
        assert len(cats) == 1
        assert cats[0].id == sample_category.id

    async def test_filters_archived(self, test_db, sample_category):
        sample_category.is_active = False
        await test_db.commit()

        cats = await get_active_categories(test_db)
        assert cats == []

    async def test_filter_by_income_type(self, test_db):
        income_cat = await create_category(test_db, 'Доход', ApplicableTo.INCOME, group='Доходы')
        expense_cat = await create_category(test_db, 'Расход', ApplicableTo.EXPENSE, group='Расходы')
        both_cat = await create_category(test_db, 'Общая', ApplicableTo.BOTH, group='Прочее')
        await test_db.commit()

        cats = await get_active_categories(test_db, OperationType.INCOME)
        cat_ids = [c.id for c in cats]
        assert income_cat.id in cat_ids
        assert both_cat.id in cat_ids
        assert expense_cat.id not in cat_ids

    async def test_filter_by_expense_type(self, test_db):
        income_cat = await create_category(test_db, 'Доход', ApplicableTo.INCOME, group='Доходы')
        expense_cat = await create_category(test_db, 'Расход', ApplicableTo.EXPENSE, group='Расходы')
        await test_db.commit()

        cats = await get_active_categories(test_db, OperationType.EXPENSE)
        cat_ids = [c.id for c in cats]
        assert expense_cat.id in cat_ids
        assert income_cat.id not in cat_ids


class TestCreateCategory:
    """Тесты создания категорий"""

    pytestmark = [pytest.mark.asyncio, pytest.mark.unit, pytest.mark.finance]

    async def test_create_basic(self, test_db):
        cat = await create_category(test_db, 'Новая', ApplicableTo.BOTH, group='Группа')
        await test_db.commit()

        assert cat.id is not None
        assert cat.name == 'Новая'
        assert cat.group == 'Группа'
        assert cat.applicable_to == ApplicableTo.BOTH
        assert cat.is_active is True

    async def test_created_is_active(self, test_db):
        cat = await create_category(test_db, 'Тест', ApplicableTo.EXPENSE, group='Группа')
        await test_db.commit()
        assert cat.is_active is True

    async def test_create_with_href(self, test_db):
        href = 'https://api.moysklad.ru/api/remap/1.2/entity/expenseitem/abc-123'
        cat = await create_category(
            test_db, 'Расход', ApplicableTo.EXPENSE,
            moysklad_expense_item_href=href,
        )
        await test_db.commit()
        assert cat.moysklad_expense_item_href == href


class TestRenameCategory:
    """Тесты переименования категорий"""

    pytestmark = [pytest.mark.asyncio, pytest.mark.unit, pytest.mark.finance]

    async def test_rename_updates_name(self, test_db, sample_category):
        cat = await rename_category(test_db, sample_category.id, 'Новое название')
        await test_db.commit()

        assert cat is not None
        assert cat.name == 'Новое название'

    async def test_rename_nonexistent(self, test_db):
        result = await rename_category(test_db, 99999, 'Новое название')
        assert result is None

    async def test_rename_persists(self, test_db, sample_category):
        await rename_category(test_db, sample_category.id, 'Обновлённая')
        await test_db.commit()

        fetched = await get_category_by_id(test_db, sample_category.id)
        assert fetched.name == 'Обновлённая'


class TestSyncExpenseCategories:
    """Тесты синхронизации категорий из МойСклад"""

    pytestmark = [pytest.mark.asyncio, pytest.mark.unit, pytest.mark.finance]

    async def _make_ms_item(self, name: str, uid: str = None) -> dict:
        uid = uid or name.lower().replace(' ', '-')
        return {
            'id': uid,
            'name': name,
            'href': f'https://api.moysklad.ru/api/remap/1.2/entity/expenseitem/{uid}',
        }

    async def test_creates_new_from_ms(self, test_db):
        """Категория есть в МС, нет локально → создаётся"""
        ms_items = [await self._make_ms_item('Логистика', 'log-1')]
        stats = await sync_expense_categories(test_db, ms_items)
        await test_db.commit()

        assert stats['created'] == 1
        cats = await get_active_categories(test_db, OperationType.EXPENSE)
        assert any(c.name == 'Логистика' for c in cats)

    async def test_updates_name_if_changed(self, test_db):
        """Категория есть в МС и локально с href → обновляется имя"""
        href = 'https://api.moysklad.ru/api/remap/1.2/entity/expenseitem/log-1'
        cat = await create_category(
            test_db, 'Старое имя', ApplicableTo.EXPENSE,
            moysklad_expense_item_href=href,
        )
        await test_db.commit()

        ms_items = [{'id': 'log-1', 'name': 'Новое имя', 'href': href}]
        stats = await sync_expense_categories(test_db, ms_items)
        await test_db.commit()

        assert stats['updated'] == 1
        fetched = await get_category_by_id(test_db, cat.id)
        assert fetched.name == 'Новое имя'

    async def test_no_update_if_name_same(self, test_db):
        """Имя не изменилось → updated=0"""
        href = 'https://api.moysklad.ru/api/remap/1.2/entity/expenseitem/log-1'
        await create_category(
            test_db, 'Логистика', ApplicableTo.EXPENSE,
            moysklad_expense_item_href=href,
        )
        await test_db.commit()

        ms_items = [{'id': 'log-1', 'name': 'Логистика', 'href': href}]
        stats = await sync_expense_categories(test_db, ms_items)
        await test_db.commit()

        assert stats['updated'] == 0
        assert stats['created'] == 0

    async def test_deletes_missing_from_ms_without_transactions(self, test_db):
        """Есть локально с href, нет в МС, нет операций → категория удаляется"""
        href = 'https://api.moysklad.ru/api/remap/1.2/entity/expenseitem/old-1'
        cat = await create_category(
            test_db, 'Устаревшая', ApplicableTo.EXPENSE,
            moysklad_expense_item_href=href,
        )
        await test_db.commit()
        cat_id = cat.id

        stats = await sync_expense_categories(test_db, [])  # МС пустой
        await test_db.commit()

        assert stats['deleted'] == 1
        fetched = await get_category_by_id(test_db, cat_id)
        assert fetched is not None
        assert fetched.is_active is False

    async def test_income_categories_untouched(self, test_db):
        """Доходные категории без href не трогаются"""
        income_cat = await create_category(test_db, 'Продажи', ApplicableTo.INCOME)
        await test_db.commit()

        stats = await sync_expense_categories(test_db, [])
        await test_db.commit()

        assert stats['deleted'] == 0
        fetched = await get_category_by_id(test_db, income_cat.id)
        assert fetched.is_active is True

    async def test_reactivates_if_returned_in_ms(self, test_db):
        """Категория была деактивирована, снова появилась в МС → is_active=True"""
        href = 'https://api.moysklad.ru/api/remap/1.2/entity/expenseitem/log-1'
        cat = await create_category(
            test_db, 'Логистика', ApplicableTo.EXPENSE,
            moysklad_expense_item_href=href,
        )
        cat.is_active = False
        await test_db.commit()

        ms_items = [{'id': 'log-1', 'name': 'Логистика', 'href': href}]
        await sync_expense_categories(test_db, ms_items)
        await test_db.commit()

        fetched = await get_category_by_id(test_db, cat.id)
        assert fetched.is_active is True
