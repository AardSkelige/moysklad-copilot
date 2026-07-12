"""Тесты просмотра и редактирования операций через МойСклад"""

import pytest
from datetime import datetime
from unittest.mock import patch, AsyncMock, MagicMock

from core.database import OperationType
from tests.helpers import MockFSMContext, make_mock_callback, mock_session_scope_with

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class TestMSViewOperationsHandler:
    """Тесты хэндлеров просмотра и редактирования операций"""

    pytestmark = [pytest.mark.asyncio, pytest.mark.unit, pytest.mark.fsm]

    def _make_fake_payment(self, ms_id: str, op_type=OperationType.INCOME, expense_href=None) -> dict:
        return {
            'ms_id': ms_id,
            'href': f'https://api.moysklad.ru/api/remap/1.2/entity/paymentin/{ms_id}',
            'op_type': op_type,
            'amount_kopecks': 100000,
            'moment': datetime(2024, 1, 15, 14, 30),
            'comment': 'Тест',
            'expense_item_href': expense_href,
        }

    async def test_show_operations_list_ms(self, test_db):
        """Загружаем операции из МС и кэшируем в FSM"""
        from handlers.finance.view_operations import show_operations_list

        fake_payments = [self._make_fake_payment('aaa-111')]
        state = MockFSMContext()
        callback = make_mock_callback(data='finance_list')

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []

        with patch('handlers.finance.view_operations.MoySkladClient') as MockClient:
            MockClient.return_value.fetch_payments = AsyncMock(return_value=fake_payments)
            with patch('handlers.finance.view_operations.session_scope', lambda: mock_session_scope_with(test_db)):
                test_db.execute = AsyncMock(return_value=mock_result)
                await show_operations_list(callback, state)

        callback.message.answer.assert_awaited_once()
        data = await state.get_data()
        assert 'ms_payments' in data
        assert len(data['ms_payments']) == 1

    async def test_ms_view_detail(self):
        """ms_view:{ms_id} → редактирует сообщение, показывая детали"""
        from handlers.finance.view_operations import ms_view_detail
        from shared.constants import CallbackPrefix

        ms_id = 'aaa-view-001'
        fake_payment = self._make_fake_payment(ms_id)
        state = MockFSMContext(initial_data={'ms_payments': [fake_payment], 'ms_category_map': {}})
        callback = make_mock_callback(data=f'{CallbackPrefix.MS_VIEW}{ms_id}')

        await ms_view_detail(callback, state)

        callback.message.edit_text.assert_awaited_once()

    async def test_ms_delete_confirm(self):
        """ms_del:{ms_id} → редактирует сообщение, показывая подтверждение"""
        from handlers.finance.view_operations import ms_delete_operation_confirm
        from shared.constants import CallbackPrefix

        ms_id = 'test-ms-id-123'
        state = MockFSMContext()
        callback = make_mock_callback(data=f'{CallbackPrefix.MS_DELETE}{ms_id}')

        await ms_delete_operation_confirm(callback, state)

        callback.message.edit_text.assert_awaited_once()

    async def test_ms_delete_execute(self):
        """ms_del_ok:{ms_id} → вызывает delete_payment_by_href и убирает из FSM"""
        from handlers.finance.view_operations import ms_delete_execute
        from shared.constants import CallbackPrefix

        ms_id = 'aaaaaaaa-0000-0000-0000-000000000001'
        href = f'https://api.moysklad.ru/api/remap/1.2/entity/paymentin/{ms_id}'
        fake_payment = self._make_fake_payment(ms_id)
        fake_payment['href'] = href

        state = MockFSMContext(initial_data={'ms_payments': [fake_payment]})
        callback = make_mock_callback(data=f'{CallbackPrefix.MS_DELETE_CONFIRM}{ms_id}')

        with patch('handlers.finance.view_operations.MoySkladClient') as MockClient:
            MockClient.return_value.delete_payment_by_href = AsyncMock()
            await ms_delete_execute(callback, state)

        MockClient.return_value.delete_payment_by_href.assert_awaited_once_with(href)
        callback.message.answer.assert_awaited_once()

        data = await state.get_data()
        assert all(p['ms_id'] != ms_id for p in data.get('ms_payments', []))

    async def test_ms_edit_start(self):
        """ms_edit:{ms_id} → редактирует сообщение, показывая форму редактирования"""
        from handlers.finance.view_operations import start_ms_edit
        from shared.constants import CallbackPrefix

        ms_id = 'aaa-edit-001'
        fake_payment = self._make_fake_payment(ms_id, op_type=OperationType.EXPENSE, expense_href='http://href')

        state = MockFSMContext(initial_data={'ms_payments': [fake_payment], 'ms_category_map': {}})
        callback = make_mock_callback(data=f'{CallbackPrefix.MS_EDIT}{ms_id}')

        await start_ms_edit(callback, state)

        callback.message.edit_text.assert_awaited_once()
        fsm_state = await state.get_state()
        assert fsm_state is not None

    async def test_ms_save_amount(self):
        """Ввод суммы → обновляет в МС и очищает FSM"""
        from handlers.finance.view_operations import ms_save_amount
        from tests.helpers import make_mock_message

        ms_id = 'aaa-amount-001'
        href = f'https://api.moysklad.ru/api/remap/1.2/entity/paymentin/{ms_id}'
        fake_payment = self._make_fake_payment(ms_id)
        fake_payment['href'] = href

        state = MockFSMContext(initial_data={
            'ms_edit_id': ms_id,
            'ms_edit_href': href,
            'ms_payments': [fake_payment],
        })
        message = make_mock_message(text='1500.50')

        with patch('handlers.finance.view_operations.MoySkladClient') as MockClient:
            MockClient.return_value.update_payment_fields = AsyncMock()
            await ms_save_amount(message, state)

        MockClient.return_value.update_payment_fields.assert_awaited_once_with(href, {'sum': 150050})
        assert await state.get_state() is None

    async def test_ms_save_amount_invalid(self):
        """Некорректная сумма → FSM остаётся на waiting_amount"""
        from handlers.finance.view_operations import ms_save_amount
        from shared.states import EditMSOperationState
        from tests.helpers import make_mock_message

        state = MockFSMContext(initial_data={'ms_edit_id': 'x', 'ms_edit_href': 'http://href'})
        await state.set_state(EditMSOperationState.waiting_amount)
        message = make_mock_message(text='abc')

        with patch('handlers.finance.view_operations.MoySkladClient') as MockClient:
            MockClient.return_value.update_payment_fields = AsyncMock()
            await ms_save_amount(message, state)

        MockClient.return_value.update_payment_fields.assert_not_awaited()
        # FSM state должен остаться waiting_amount
        fsm_state = await state.get_state()
        assert fsm_state == EditMSOperationState.waiting_amount.state
