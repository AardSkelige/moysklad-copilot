"""МойСклад API клиент для синхронизации платежей"""

import aiohttp
import ssl
from datetime import datetime
from typing import Optional

from core import config
from core.database import OperationType, DocumentSubtype, Category
from core.logger import logger
from integrations.moysklad_base import new_session


class MoySkladClient:
    """HTTP клиент для работы с API МойСклад"""

    def __init__(self):
        self.base_url = config.MOYSKLAD_BASE_URL
        self.token = config.MOYSKLAD_TOKEN
        self.organization_href = config.MOYSKLAD_ORGANIZATION_HREF
        self.agent_href = config.MOYSKLAD_AGENT_HREF

    def _get_headers(self) -> dict:
        """Возвращает заголовки для запросов"""
        return {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
            'Accept-Encoding': 'gzip'
        }

    def _get_ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _get_endpoint(self, subtype: DocumentSubtype) -> str:
        """Возвращает endpoint по подтипу документа"""
        return f'{self.base_url}/entity/{subtype.value}'

    def _build_payment_data(
        self,
        op_type: OperationType,
        subtype: DocumentSubtype,
        amount_kopecks: int,
        category: Optional[Category],
        comment: Optional[str] = None,
        moment: Optional[datetime] = None,
    ) -> dict:
        """Формирует данные для создания платежа"""
        # Формат даты для МойСклад: "2016-07-01 16:19:00"
        moment_str = (moment or datetime.now()).strftime('%Y-%m-%d %H:%M:%S')

        data = {
            'organization': {
                'meta': {
                    'href': self.organization_href,
                    'type': 'organization',
                    'mediaType': 'application/json'
                }
            },
            'agent': {
                'meta': {
                    'href': self.agent_href,
                    'type': 'counterparty',
                    'mediaType': 'application/json'
                }
            },
            'sum': amount_kopecks,
            'moment': moment_str,
            'applicable': True
        }

        # Назначение платежа
        if category:
            group_part = f' ({category.group})' if category.group else ''
            data['paymentPurpose'] = f'Telegram | {category.name}{group_part}'
        else:
            data['paymentPurpose'] = 'Telegram'

        # Комментарий пользователя в поле description
        if comment:
            data['description'] = comment

        # Статья расходов и noClosingDocs — только для расходных документов
        if subtype in (DocumentSubtype.PAYMENT_OUT, DocumentSubtype.CASH_OUT):
            if not category or not category.moysklad_expense_item_href:
                raise Exception('Category must have moysklad_expense_item_href for expense payment')

            data['expenseItem'] = {
                'meta': {
                    'href': category.moysklad_expense_item_href,
                    'type': 'expenseitem',
                    'mediaType': 'application/json'
                }
            }
            data['noClosingDocs'] = True

        return data

    async def create_payment(
        self,
        op_type: OperationType,
        subtype: DocumentSubtype,
        amount_kopecks: int,
        category: Optional[Category],
        comment: Optional[str] = None,
        moment: Optional[datetime] = None,
    ) -> str:
        """
        Создает платеж в МойСклад.
        Возвращает moysklad_id созданного платежа.
        """
        endpoint = self._get_endpoint(subtype)
        data = self._build_payment_data(op_type, subtype, amount_kopecks, category, comment, moment)

        ssl_context = self._get_ssl_context()

        async with new_session() as session:
            async with session.post(
                endpoint,
                headers=self._get_headers(),
                json=data,
                ssl=ssl_context
            ) as response:
                if response.status not in (200, 201):
                    error_text = await response.text()
                    raise Exception(f'МойСклад API error: {response.status}, {error_text}')

                result = await response.json()
                moysklad_id = result.get('id')

                if not moysklad_id:
                    raise Exception('МойСклад API did not return payment ID')

                logger.info(f'Created payment in MoySklad: {moysklad_id}')
                return moysklad_id

    async def delete_payment(self, moysklad_id: str, op_type: OperationType):
        """Удаляет платеж из МойСклад"""
        endpoint = f'{self._get_endpoint(op_type)}/{moysklad_id}'

        ssl_context = self._get_ssl_context()

        async with new_session() as session:
            async with session.delete(
                endpoint,
                headers=self._get_headers(),
                ssl=ssl_context
            ) as response:
                if response.status not in (200, 204):
                    error_text = await response.text()
                    raise Exception(f'МойСклад API error: {response.status}, {error_text}')

                logger.info(f'Deleted payment from MoySklad: {moysklad_id}')

    async def fetch_expense_items(self) -> list[dict]:
        """
        GET /entity/expenseitem — возвращает все статьи расходов.
        Каждый элемент: {id, name, href}
        """
        endpoint = f'{self.base_url}/entity/expenseitem'

        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        async with new_session() as session:
            async with session.get(
                endpoint,
                headers=self._get_headers(),
                ssl=ssl_context
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f'МойСклад API error: {response.status}, {error_text}')

                result = await response.json()
                rows = result.get('rows', [])
                return [
                    {
                        'id': item['id'],
                        'name': item['name'],
                        'href': item['meta']['href'],
                        'is_system': 'accountId' not in item,
                    }
                    for item in rows
                ]

    async def update_expense_item(self, href: str, new_name: str) -> None:
        """PUT href — переименовать статью расходов в МойСклад"""
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        async with new_session() as session:
            async with session.put(
                href,
                headers=self._get_headers(),
                json={'name': new_name},
                ssl=ssl_context
            ) as response:
                if response.status not in (200, 201):
                    error_text = await response.text()
                    raise Exception(f'МойСклад API error: {response.status}, {error_text}')

                logger.info(f'Renamed expense item in MoySklad: {href} -> {new_name}')

    async def find_expense_item_by_name(self, name: str) -> Optional[str]:
        """
        Ищет статью расходов по имени.
        Возвращает href если найдена, None если нет.
        """
        endpoint = f'{self.base_url}/entity/expenseitem'
        params = {'search': name}

        ssl_context = self._get_ssl_context()

        async with new_session() as session:
            async with session.get(
                endpoint,
                headers=self._get_headers(),
                params=params,
                ssl=ssl_context
            ) as response:
                if response.status != 200:
                    return None

                result = await response.json()
                rows = result.get('rows', [])

                # Ищем точное совпадение по имени
                for item in rows:
                    if item.get('name') == name:
                        return item['meta']['href']

                return None

    async def create_expense_item(self, name: str, description: str = '') -> str:
        """
        Создает статью расходов в МойСклад.
        Возвращает href созданной статьи расходов.
        """
        endpoint = f'{self.base_url}/entity/expenseitem'

        data = {
            'name': name,
            'description': description or f'Статья расходов для категории "{name}"'
        }

        ssl_context = self._get_ssl_context()

        async with new_session() as session:
            async with session.post(
                endpoint,
                headers=self._get_headers(),
                json=data,
                ssl=ssl_context
            ) as response:
                if response.status not in (200, 201):
                    error_text = await response.text()
                    raise Exception(f'МойСклад API error: {response.status}, {error_text}')

                result = await response.json()
                expense_item_href = result['meta']['href']

                logger.info(f'Created expense item in MoySklad: {name} -> {expense_item_href}')
                return expense_item_href

    def _parse_moment(self, s: str) -> datetime:
        """Парсит строку даты МойСклад в datetime"""
        for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return datetime.now()

    async def fetch_payments(self, limit: int = 100) -> list[dict]:
        """
        GET /entity/paymentin + /entity/paymentout (order=moment,desc)
        Возвращает объединённый список, отсортированный по дате убывания.
        Каждый элемент: {ms_id, href, op_type, amount_kopecks, moment, comment, expense_item_href}
        """
        ssl_context = self._get_ssl_context()
        params = {'order': 'moment,desc', 'limit': limit}
        results = []

        for entity_type, op_type in [
            ('paymentin', OperationType.INCOME),
            ('cashin', OperationType.INCOME),
            ('paymentout', OperationType.EXPENSE),
            ('cashout', OperationType.EXPENSE),
        ]:
            endpoint = f'{self.base_url}/entity/{entity_type}'
            async with new_session() as session:
                async with session.get(
                    endpoint,
                    headers=self._get_headers(),
                    params=params,
                    ssl=ssl_context,
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        raise Exception(f'МойСклад API error {entity_type}: {response.status}, {error_text}')
                    data = await response.json()

            for item in data.get('rows', []):
                expense_item_href = None
                if op_type == OperationType.EXPENSE:
                    expense_meta = item.get('expenseItem', {}).get('meta', {})
                    expense_item_href = expense_meta.get('href')

                results.append({
                    'ms_id': item['id'],
                    'href': item['meta']['href'],
                    'op_type': op_type,
                    'amount_kopecks': item.get('sum', 0),
                    'moment': self._parse_moment(item.get('moment', '')),
                    'comment': item.get('paymentPurpose', ''),
                    'expense_item_href': expense_item_href,
                })

        results.sort(key=lambda p: p['moment'], reverse=True)
        return results

    async def update_payment_fields(self, href: str, fields: dict) -> None:
        """PUT href — обновить указанные поля платежа"""
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        async with new_session() as session:
            async with session.put(
                href,
                headers=self._get_headers(),
                json=fields,
                ssl=ssl_context,
            ) as response:
                if response.status not in (200, 201):
                    error_text = await response.text()
                    raise Exception(f'МойСклад API error: {response.status}, {error_text}')

                logger.info(f'Updated payment fields in MoySklad: {href}')

    async def delete_payment_by_href(self, href: str) -> None:
        """DELETE по полному href платежа"""
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        async with new_session() as session:
            async with session.delete(
                href,
                headers=self._get_headers(),
                ssl=ssl_context,
            ) as response:
                if response.status not in (200, 204):
                    error_text = await response.text()
                    raise Exception(f'МойСклад API error: {response.status}, {error_text}')

                logger.info(f'Deleted payment from MoySklad by href: {href}')
