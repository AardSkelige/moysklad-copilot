"""Тесты FSM-флоу добавления операции"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from core.database import OperationType, DocumentSubtype
from tests.helpers import MockFSMContext, make_mock_callback, make_mock_message, mock_session_scope_with

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class TestAddOperationFSM:
    """Тесты FSM-флоу добавления операции"""

    pytestmark = [pytest.mark.asyncio, pytest.mark.unit, pytest.mark.fsm]

    async def test_start_sets_waiting_type_state(self, test_db, sample_category):
        from handlers.finance.add_operation import start_add_operation

        state = MockFSMContext()
        callback = make_mock_callback(data='finance_add')

        await start_add_operation(callback, state)

        assert await state.get_state() == 'AddOperationState:waiting_type'
        callback.answer.assert_awaited_once()

    async def test_process_type_income(self, test_db, sample_category):
        from handlers.finance.add_operation import process_type
        from shared.states import AddOperationState

        state = MockFSMContext()
        await state.set_state(AddOperationState.waiting_type)

        callback = make_mock_callback(data='type_income')

        with patch('handlers.finance.add_operation.session_scope', lambda: mock_session_scope_with(test_db)):
            await process_type(callback, state)

        data = await state.get_data()
        assert data['op_type'] == OperationType.INCOME.value
        assert await state.get_state() == 'AddOperationState:waiting_subtype'

    async def test_process_type_expense(self, test_db, sample_category):
        from handlers.finance.add_operation import process_type
        from shared.states import AddOperationState

        state = MockFSMContext()
        await state.set_state(AddOperationState.waiting_type)

        callback = make_mock_callback(data='type_expense')

        with patch('handlers.finance.add_operation.session_scope', lambda: mock_session_scope_with(test_db)):
            await process_type(callback, state)

        data = await state.get_data()
        assert data['op_type'] == OperationType.EXPENSE.value

    async def test_process_category(self, test_db, sample_category):
        from handlers.finance.add_operation import process_category
        from shared.constants import CallbackPrefix
        from shared.states import AddOperationState

        state = MockFSMContext(initial_data={'op_type': OperationType.EXPENSE.value})
        await state.set_state(AddOperationState.waiting_category)

        callback = make_mock_callback(data=f'{CallbackPrefix.SELECT_CATEGORY}{sample_category.id}')

        with patch('handlers.finance.add_operation.session_scope', lambda: mock_session_scope_with(test_db)):
            await process_category(callback, state)

        data = await state.get_data()
        assert data['category_id'] == sample_category.id
        assert await state.get_state() == 'AddOperationState:waiting_amount'

    async def test_process_amount_valid(self, test_db, sample_category):
        from handlers.finance.add_operation import process_amount
        from shared.states import AddOperationState

        state = MockFSMContext(initial_data={
            'op_type': OperationType.EXPENSE.value,
            'category_name': 'Тест',
        })
        await state.set_state(AddOperationState.waiting_amount)

        message = make_mock_message(text='1500.50')

        await process_amount(message, state)

        data = await state.get_data()
        assert data['amount_kopecks'] == 150050
        assert await state.get_state() == 'AddOperationState:waiting_comment'

    async def test_process_amount_invalid_keeps_state(self, test_db):
        from handlers.finance.add_operation import process_amount
        from shared.states import AddOperationState

        state = MockFSMContext()
        await state.set_state(AddOperationState.waiting_amount)

        message = make_mock_message(text='abc')

        await process_amount(message, state)

        # Состояние не изменилось
        assert await state.get_state() == 'AddOperationState:waiting_amount'

    async def test_skip_comment(self, test_db):
        from handlers.finance.add_operation import skip_comment
        from shared.states import AddOperationState

        state = MockFSMContext(initial_data={
            'op_type': OperationType.EXPENSE.value,
            'category_name': 'Тест',
            'amount_kopecks': 150050,
        })
        await state.set_state(AddOperationState.waiting_comment)

        callback = make_mock_callback(data='skip_comment')

        await skip_comment(callback, state)

        data = await state.get_data()
        assert data.get('comment') is None
        assert await state.get_state() == 'AddOperationState:waiting_confirm'

    async def test_confirm_save_clears_state(self, test_db, sample_category):
        from handlers.finance.add_operation import confirm_save
        from shared.states import AddOperationState

        state = MockFSMContext(initial_data={
            'op_type': OperationType.EXPENSE.value,
            'doc_subtype': DocumentSubtype.PAYMENT_OUT.value,
            'category_id': sample_category.id,
            'category_name': 'Тест',
            'amount_kopecks': 150050,
            'comment': None,
        })
        await state.set_state(AddOperationState.waiting_confirm)

        callback = make_mock_callback(data='confirm_save')

        mock_client = MagicMock()
        mock_client.create_payment = AsyncMock(return_value='ms-test-id')

        with patch('handlers.finance.add_operation.session_scope', lambda: mock_session_scope_with(test_db)):
            with patch('handlers.finance.add_operation.MoySkladClient', return_value=mock_client):
                await confirm_save(callback, state)

        # FSM очищен
        assert await state.get_state() is None
        data = await state.get_data()
        assert data == {}

    async def test_cancel_clears_state(self, test_db):
        from handlers.finance.add_operation import confirm_cancel
        from shared.states import AddOperationState

        state = MockFSMContext(initial_data={'op_type': OperationType.EXPENSE.value})
        await state.set_state(AddOperationState.waiting_type)

        callback = make_mock_callback(data='confirm_cancel')

        await confirm_cancel(callback, state)

        # FSM очищен
        assert await state.get_state() is None
        data = await state.get_data()
        assert data == {}
