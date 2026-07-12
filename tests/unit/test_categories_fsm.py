"""Тесты FSM-флоу управления категориями"""

import pytest
from unittest.mock import patch, AsyncMock

from core.database import ApplicableTo
from services.category_service import create_category
from tests.helpers import MockFSMContext, make_mock_callback, make_mock_message, mock_session_scope_with

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class TestCategoriesHandler:
    """Тесты хэндлера управления категориями"""

    pytestmark = [pytest.mark.asyncio, pytest.mark.unit, pytest.mark.fsm]

    async def test_show_categories_menu(self, test_db):
        """show_categories показывает меню и очищает FSM"""
        from handlers.finance.categories import show_categories
        from shared.constants import CallbackData

        state = MockFSMContext()
        callback = make_mock_callback(data=CallbackData.FINANCE_CATEGORIES)

        await show_categories(callback, state)

        callback.message.answer.assert_awaited_once()
        assert await state.get_state() is None

    async def test_add_category_sets_waiting_name(self, test_db):
        """CAT_ADD переводит FSM в waiting_name"""
        from handlers.finance.categories import start_add_category
        from shared.constants import CallbackData
        from shared.states import AddCategoryState

        state = MockFSMContext()
        callback = make_mock_callback(data=CallbackData.CAT_ADD)

        await start_add_category(callback, state)

        assert await state.get_state() == 'AddCategoryState:waiting_name'

    async def test_process_category_name_too_short(self, test_db):
        """Короткое название не меняет состояние"""
        from handlers.finance.categories import process_category_name
        from shared.states import AddCategoryState

        state = MockFSMContext()
        await state.set_state(AddCategoryState.waiting_name)

        message = make_mock_message(text='A')
        await process_category_name(message, state)

        assert await state.get_state() == 'AddCategoryState:waiting_name'

    async def test_process_category_name_success(self, test_db):
        """Создание категории: успех — FSM очищен, категория сохранена"""
        from handlers.finance.categories import process_category_name
        from services.category_service import get_active_categories
        from shared.states import AddCategoryState

        state = MockFSMContext()
        await state.set_state(AddCategoryState.waiting_name)

        message = make_mock_message(text='МС категория')
        fake_href = 'https://api.moysklad.ru/api/remap/1.2/entity/expenseitem/test-id'

        with patch('handlers.finance.categories.session_scope', lambda: mock_session_scope_with(test_db)), \
             patch('handlers.finance.categories.MoySkladClient') as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.create_expense_item = AsyncMock(return_value=fake_href)

            await process_category_name(message, state)

        assert await state.get_state() is None
        cats = await get_active_categories(test_db)
        created = next((c for c in cats if c.name == 'МС категория'), None)
        assert created is not None
        assert created.moysklad_expense_item_href == fake_href

    async def test_process_category_name_moysklad_error(self, test_db):
        """Создание категории: ошибка МойСклад → FSM очищается, сообщение об ошибке"""
        from handlers.finance.categories import process_category_name
        from services.category_service import get_active_categories
        from shared.states import AddCategoryState

        state = MockFSMContext()
        await state.set_state(AddCategoryState.waiting_name)

        message = make_mock_message(text='Проблемная категория')

        with patch('handlers.finance.categories.MoySkladClient') as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.create_expense_item = AsyncMock(side_effect=Exception('API error'))

            await process_category_name(message, state)

        assert await state.get_state() is None
        # Категория не создана
        cats = await get_active_categories(test_db)
        assert not any(c.name == 'Проблемная категория' for c in cats)


class TestRenameCategoryFSM:
    """Тесты FSM-флоу переименования категорий"""

    pytestmark = [pytest.mark.asyncio, pytest.mark.unit, pytest.mark.fsm]

    async def test_show_rename_list_with_categories(self, test_db):
        """CAT_RENAME_LIST с категориями переводит FSM в waiting_select"""
        from handlers.finance.categories import show_rename_list
        from shared.constants import CallbackData
        from shared.states import RenameCategoryState

        # Создаём EXPENSE-категорию
        await create_category(test_db, 'Расход', ApplicableTo.EXPENSE)
        await test_db.commit()

        state = MockFSMContext()
        callback = make_mock_callback(data=CallbackData.CAT_RENAME_LIST)

        with patch('handlers.finance.categories.session_scope', lambda: mock_session_scope_with(test_db)):
            await show_rename_list(callback, state)

        assert await state.get_state() == 'RenameCategoryState:waiting_select'

    async def test_show_rename_list_empty(self, test_db):
        """CAT_RENAME_LIST без категорий — сообщение, FSM не меняется"""
        from handlers.finance.categories import show_rename_list
        from shared.constants import CallbackData

        state = MockFSMContext()
        callback = make_mock_callback(data=CallbackData.CAT_RENAME_LIST)

        with patch('handlers.finance.categories.session_scope', lambda: mock_session_scope_with(test_db)):
            await show_rename_list(callback, state)

        assert await state.get_state() is None
        callback.message.answer.assert_awaited_once()

    async def test_rename_category_selected_sets_state(self, test_db, sample_category):
        """Выбор категории переводит в waiting_new_name и сохраняет cat_id"""
        from handlers.finance.categories import rename_category_selected
        from shared.constants import CallbackPrefix
        from shared.states import RenameCategoryState

        state = MockFSMContext()
        await state.set_state(RenameCategoryState.waiting_select)

        callback = make_mock_callback(data=f'{CallbackPrefix.RENAME_CATEGORY}{sample_category.id}')
        await rename_category_selected(callback, state)

        assert await state.get_state() == 'RenameCategoryState:waiting_new_name'
        data = await state.get_data()
        assert data['cat_id'] == sample_category.id

    async def test_process_rename_too_short(self, test_db, sample_category):
        """Короткое имя оставляет FSM в waiting_new_name"""
        from handlers.finance.categories import process_rename_new_name
        from shared.states import RenameCategoryState

        state = MockFSMContext(initial_data={'cat_id': sample_category.id})
        await state.set_state(RenameCategoryState.waiting_new_name)

        message = make_mock_message(text='A')
        await process_rename_new_name(message, state)

        assert await state.get_state() == 'RenameCategoryState:waiting_new_name'

    async def test_process_rename_success(self, test_db, sample_category):
        """Переименование с МойСклад обновляет и МС и локально"""
        from handlers.finance.categories import process_rename_new_name
        from services.category_service import get_category_by_id
        from shared.states import RenameCategoryState

        sample_category.moysklad_expense_item_href = (
            'https://api.moysklad.ru/api/remap/1.2/entity/expenseitem/test-id'
        )
        await test_db.commit()

        state = MockFSMContext(initial_data={'cat_id': sample_category.id})
        await state.set_state(RenameCategoryState.waiting_new_name)

        message = make_mock_message(text='МС Переименованная')

        with patch('handlers.finance.categories.session_scope', lambda: mock_session_scope_with(test_db)), \
             patch('handlers.finance.categories.MoySkladClient') as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.update_expense_item = AsyncMock()

            await process_rename_new_name(message, state)

        assert await state.get_state() is None
        fetched = await get_category_by_id(test_db, sample_category.id)
        assert fetched.name == 'МС Переименованная'

    async def test_process_rename_no_href_renames_locally(self, test_db, sample_category):
        """Переименование без href в МС — только локальное обновление"""
        from handlers.finance.categories import process_rename_new_name
        from services.category_service import get_category_by_id
        from shared.states import RenameCategoryState

        # Категория без href
        assert sample_category.moysklad_expense_item_href is None

        state = MockFSMContext(initial_data={'cat_id': sample_category.id})
        await state.set_state(RenameCategoryState.waiting_new_name)

        message = make_mock_message(text='Переименованная')

        with patch('handlers.finance.categories.session_scope', lambda: mock_session_scope_with(test_db)):
            await process_rename_new_name(message, state)

        assert await state.get_state() is None
        assert await state.get_data() == {}

        fetched = await get_category_by_id(test_db, sample_category.id)
        assert fetched.name == 'Переименованная'

    async def test_process_rename_moysklad_error(self, test_db, sample_category):
        """Ошибка МойСклад при переименовании → FSM очищается, имя не меняется"""
        from handlers.finance.categories import process_rename_new_name
        from services.category_service import get_category_by_id
        from shared.states import RenameCategoryState

        sample_category.moysklad_expense_item_href = (
            'https://api.moysklad.ru/api/remap/1.2/entity/expenseitem/test-id'
        )
        original_name = sample_category.name
        await test_db.commit()

        state = MockFSMContext(initial_data={'cat_id': sample_category.id})
        await state.set_state(RenameCategoryState.waiting_new_name)

        message = make_mock_message(text='Проблемное имя')

        with patch('handlers.finance.categories.session_scope', lambda: mock_session_scope_with(test_db)), \
             patch('handlers.finance.categories.MoySkladClient') as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.update_expense_item = AsyncMock(side_effect=Exception('API error'))

            await process_rename_new_name(message, state)

        assert await state.get_state() is None
        fetched = await get_category_by_id(test_db, sample_category.id)
        assert fetched.name == original_name
