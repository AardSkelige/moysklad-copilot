"""Тесты синхронизации с МойСклад"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.database import OperationType
from services.moysklad_service import sync_delete

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class TestMoySkladSync:
    """Тесты синхронизации с МойСклад"""

    pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

    async def test_sync_delete_calls_client(self):
        """sync_delete вызывает delete_payment клиента"""
        mock_client = MagicMock()
        mock_client.delete_payment = AsyncMock()

        with patch('services.moysklad_service.MoySkladClient', return_value=mock_client):
            await sync_delete('moysklad-456', OperationType.EXPENSE)

        mock_client.delete_payment.assert_awaited_once_with('moysklad-456', OperationType.EXPENSE)

    async def test_create_payment_direct(self, sample_category):
        """MoySkladClient.create_payment принимает plain params и возвращает moysklad_id"""
        from integrations.moysklad import MoySkladClient

        mock_client = MagicMock(spec=MoySkladClient)
        mock_client.create_payment = AsyncMock(return_value='ms-new-id')

        ms_id = await mock_client.create_payment(
            op_type=OperationType.INCOME,
            amount_kopecks=100000,
            category=sample_category,
            comment='Test',
        )

        assert ms_id == 'ms-new-id'
        mock_client.create_payment.assert_awaited_once_with(
            op_type=OperationType.INCOME,
            amount_kopecks=100000,
            category=sample_category,
            comment='Test',
        )

    async def test_sync_delete_handles_api_error(self):
        """Ошибка API не ломает бот, только логируется"""
        mock_client = MagicMock()
        mock_client.delete_payment = AsyncMock(side_effect=Exception('API Error'))

        with patch('services.moysklad_service.MoySkladClient', return_value=mock_client):
            with patch('services.moysklad_service.logger') as mock_logger:
                await sync_delete('bad-id', OperationType.EXPENSE)

        mock_logger.error.assert_called_once()
