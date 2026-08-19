"""Применение исправлений в МойСклад — единственная точка записи модуля аудита.

Агент готовит FixPreview (список действий из жёсткого whitelist), владелец
подтверждает кнопкой, ErrorFixService применяет. Никаких записей вне этого пути.
"""

from dataclasses import dataclass, field

import aiohttp

from core import config
from core.logger import logger
from integrations.moysklad_audit import MoySkladAuditClient
from integrations.moysklad_base import new_session

# Действия, которые агент вправе предложить. Всё остальное отклоняется на валидации.
ALLOWED_ACTIONS = {
    'set_description',       # {entity_type, entity_id, text}
    'set_position_price',    # {entity_type, entity_id, position_id, price_kopecks}
    'set_applicable',        # {entity_type, entity_id, applicable: bool} — провести/распровести
    'set_no_closing_docs',   # {entity_type: paymentout|cashout, entity_id} — галка «Без закрывающих документов»
    'delete_document',       # {entity_type, entity_id}
    # Создать платёж за услуги (доставка и т.п.): всегда noClosingDocs=true,
    # БЕЗ привязок к документам — иначе ломается баланс контрагента.
    # {entity_type: paymentout|cashout, agent_href, sum_kopecks,
    #  expense_item_href, purpose?, description?}
    'create_payment',
}

ALLOWED_ENTITIES = {
    'supply', 'purchaseorder', 'enter', 'loss', 'move', 'inventory',
    'demand', 'customerorder', 'salesreturn', 'productiontask',
    'paymentout', 'paymentin', 'cashout', 'cashin',
    'product',   # только set_description — заметки в карточке товара
}

_ACTION_LABELS = {
    'set_description': '✏️ Изменить комментарий',
    'set_position_price': '💰 Изменить цену позиции',
    'set_applicable': '📌 Провести/распровести',
    'set_no_closing_docs': '🏷 Галка «Без закрывающих документов»',
    'delete_document': '🗑 Удалить документ',
    'create_payment': '➕ Создать платёж (без закрывающих документов, без привязок)',
}


@dataclass
class FixPreview:
    finding_id: int
    summary: str                       # человекочитаемое описание от агента
    actions: list[dict] = field(default_factory=list)

    def to_telegram_message(self) -> str:
        lines = ['🛠 <b>Предлагаемое исправление</b>\n', self.summary, '']
        for a in self.actions:
            label = _ACTION_LABELS.get(a.get('action'), a.get('action'))
            target = f'{a.get("entity_type")}/{a.get("entity_id", "")[:8]}…'
            detail = ''
            if a.get('action') == 'set_description':
                detail = f': «{(a.get("text") or "")[:80]}»'
            elif a.get('action') == 'set_position_price':
                detail = f': {a.get("price_kopecks", 0) / 100:,.2f} ₽'.replace(',', ' ')
            elif a.get('action') == 'set_applicable':
                detail = ': провести' if a.get('applicable') else ': распровести'
            elif a.get('action') == 'create_payment':
                target = 'Платёж' if a.get('entity_type') == 'paymentout' else 'РКО'
                amount = f'{a.get("sum_kopecks", 0) / 100:,.2f}'.replace(',', ' ')
                detail = f': {amount} ₽ — «{(a.get("purpose") or "")[:80]}»'
            lines.append(f'• {label} ({target}){detail}')
        return '\n'.join(lines)


def validate_actions(actions: list[dict]) -> str | None:
    """None если всё чисто, иначе текст ошибки."""
    if not actions:
        return 'Пустой список действий'
    for a in actions:
        if a.get('action') not in ALLOWED_ACTIONS:
            return f'Недопустимое действие: {a.get("action")}'
        if a.get('entity_type') not in ALLOWED_ENTITIES:
            return f'Недопустимый тип сущности: {a.get("entity_type")}'
        if a['action'] == 'create_payment':
            if a['entity_type'] not in ('paymentout', 'cashout'):
                return 'create_payment создаёт только исходящий платёж или РКО'
            s = a.get('sum_kopecks')
            if not isinstance(s, (int, float)) or s <= 0 or int(s) != s:
                return 'create_payment: сумма должна быть целым числом копеек > 0'
            for key in ('agent_href', 'expense_item_href'):
                href = a.get(key) or ''
                if not href.startswith(f'{config.MOYSKLAD_BASE_URL}/entity/'):
                    return f'create_payment: некорректный {key}'
            continue  # entity_id не нужен — документ создаётся
        if not a.get('entity_id'):
            return 'Не указан entity_id'
        if a['action'] == 'set_description' and not (a.get('text') or '').strip():
            return 'Пустой текст комментария'
        if a['action'] == 'set_position_price':
            if not a.get('position_id'):
                return 'Не указан position_id'
            if not isinstance(a.get('price_kopecks'), (int, float)) or a['price_kopecks'] < 0:
                return 'Некорректная цена'
        if a['action'] == 'set_applicable' and not isinstance(a.get('applicable'), bool):
            return 'applicable должен быть true/false'
        if a['action'] == 'set_no_closing_docs' and a['entity_type'] not in ('paymentout', 'cashout'):
            return 'Галка «Без закрывающих документов» есть только у исходящих платежей и РКО'
        if a['entity_type'] == 'product' and a['action'] != 'set_description':
            return 'Для карточки товара разрешено только изменение описания'
    return None


_RU_ENTITY = {
    'supply': 'Приёмка', 'purchaseorder': 'Заказ поставщику',
    'enter': 'Оприходование', 'loss': 'Списание', 'move': 'Перемещение',
    'inventory': 'Инвентаризация', 'demand': 'Отгрузка',
    'customerorder': 'Заказ покупателя', 'salesreturn': 'Возврат покупателя',
    'productiontask': 'ПЗ', 'paymentout': 'Платёж', 'paymentin': 'Платёж',
    'cashout': 'РКО', 'cashin': 'ПКО', 'product': 'Товар',
}


class ErrorFixService:
    def __init__(self, client: MoySkladAuditClient | None = None):
        self.client = client or MoySkladAuditClient()

    async def _doc_label(self, session, entity: str, entity_id: str) -> str:
        """Человекочитаемое имя: «Приёмка №00058», а не «enter 667c0abd»."""
        label = _RU_ENTITY.get(entity, entity)
        try:
            d = await self.client.http.get(session, f'/entity/{entity}/{entity_id}')
            if d and d.get('name'):
                num = d['name'] if entity == 'product' else f'№{d["name"]}'
                return f'{label} {num}'
        except Exception:
            pass
        return label

    async def apply(self, preview: FixPreview) -> list[str]:
        """Применить действия по порядку. Возвращает список строк-результатов."""
        error = validate_actions(preview.actions)
        if error:
            raise ValueError(error)
        results = []
        async with new_session() as session:
            for a in preview.actions:
                action = a['action']
                if action == 'create_payment':
                    results.append(await self._create_payment(session, a))
                    logger.info(f'[fix] create_payment {a["entity_type"]} '
                                f'{a["sum_kopecks"]} коп.')
                    continue
                entity, entity_id = a['entity_type'], a['entity_id']
                label = await self._doc_label(session, entity, entity_id)
                if action == 'set_description':
                    await self.client.update_entity(session, entity, entity_id,
                                                    {'description': a['text']})
                    results.append(f'{label}: комментарий обновлён')
                elif action == 'set_position_price':
                    await self.client.update_position(session, entity, entity_id,
                                                      a['position_id'],
                                                      {'price': a['price_kopecks']})
                    price = f'{a["price_kopecks"] / 100:,.2f}'.replace(',', ' ')
                    results.append(f'{label}: цена позиции → {price} ₽')
                elif action == 'set_no_closing_docs':
                    await self.client.update_entity(session, entity, entity_id,
                                                    {'noClosingDocs': True})
                    results.append(f'{label}: поставлена галка «Без закрывающих документов»')
                elif action == 'set_applicable':
                    await self.client.update_entity(session, entity, entity_id,
                                                    {'applicable': a['applicable']})
                    verb = 'проведён' if a['applicable'] else 'распроведён'
                    results.append(f'{label}: {verb}')
                elif action == 'delete_document':
                    await self.client.delete_entity(session, entity, entity_id)
                    results.append(f'{label}: удалён')
                logger.info(f'[fix] {action} {entity}/{entity_id}')
        return results

    async def _create_payment(self, session, a: dict) -> str:
        """Платёж за услуги: noClosingDocs, проведён, без привязок к документам."""
        def _meta(href: str, mtype: str) -> dict:
            return {'meta': {'href': href, 'type': mtype, 'mediaType': 'application/json'}}

        payload = {
            'organization': _meta(config.MOYSKLAD_ORGANIZATION_HREF, 'organization'),
            'agent': _meta(a['agent_href'], 'counterparty'),
            'expenseItem': _meta(a['expense_item_href'], 'expenseitem'),
            'sum': int(a['sum_kopecks']),
            'noClosingDocs': True,
            'applicable': True,
        }
        if (a.get('purpose') or '').strip():
            payload['paymentPurpose'] = a['purpose'].strip()
        if (a.get('description') or '').strip():
            payload['description'] = a['description'].strip()
        created = await self.client.create_entity(session, a['entity_type'], payload)
        label = _RU_ENTITY.get(a['entity_type'], a['entity_type'])
        amount = f'{a["sum_kopecks"] / 100:,.2f}'.replace(',', ' ')
        ui_link = ((created or {}).get('meta') or {}).get('uuidHref', '')
        return (f'{label} №{(created or {}).get("name", "?")} создан: {amount} ₽, '
                f'галка «Без закрывающих документов», без привязок'
                + (f'\n{ui_link}' if ui_link else ''))


def fix_preview_to_state(preview: FixPreview) -> dict:
    return {'finding_id': preview.finding_id, 'summary': preview.summary,
            'actions': preview.actions}


def fix_preview_from_state(stored: dict) -> FixPreview:
    return FixPreview(finding_id=stored['finding_id'], summary=stored['summary'],
                      actions=stored['actions'])
