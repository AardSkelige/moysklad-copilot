"""Проверки раздела «Продажи»: отгрузки с нулевой суммой."""

import re
from datetime import datetime, timedelta

from core import config
from services.audit.context import (
    AuditContext,
    delivery_method,
    format_moment,
    is_consolidated_carrier,
    is_marketplace,
    parse_moment,
)
from services.audit.specs import CheckSpec, RawFinding, Section, Severity

# «Доставка по отгрузке № 00148», «Приёмка № 00078» — номер документа в назначении платежа
_DOC_NUMBER_RE = re.compile(r'№\s*0*(\d+)')
# упоминание другого документа: номер тот же, а платёж не про нашу отгрузку
_OTHER_DOC_WORDS = ('приёмк', 'приемк', 'заказ поставщику', 'счёт поставщика', 'оприходован')


def _payment_text(payment: dict) -> str:
    return f'{payment.get("paymentPurpose") or ""} {payment.get("description") or ""}'


def mentions_document(text: str, doc_number: str) -> bool:
    """Платёж ссылается на документ его номером — так связь оформлена по стандарту.

    Привязка платежа за доставку к отгрузке ломает баланс контрагента, поэтому
    единственная законная связь — текстовая, и искать её надо в назначении."""
    if not text.strip() or not doc_number:
        return False
    if any(w in text.lower() for w in _OTHER_DOC_WORDS):
        return False
    target = doc_number.lstrip('0')
    return any(n == target for n in _DOC_NUMBER_RE.findall(text))


class DemandZeroCheck(CheckSpec):
    """Проведённая отгрузка на 0 ₽. Бывает намеренно (подарок, комиссия) —
    тогда причина обязана читаться из комментария отгрузки или связанного заказа
    (по стандарту причины живут в заказе); LLM отличает по ним."""

    id = 'demand_zero'
    section = Section.SALES
    title = 'Отгрузка с нулевой суммой'
    default_severity = Severity.IMPORTANT

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        filters = (f'updated>={format_moment(since)}' if since
                   else f'moment>={ctx.scan_since_moment}')
        docs = await ctx.client.list_entities(
            ctx.session, 'demand',
            filters=filters + ';applicable=true',
            expand='agent,customerOrder',
            order='moment,desc',
        )
        out = []
        for d in docs:
            if d.get('sum', 0) != 0:
                continue
            order = d.get('customerOrder') or {}
            out.append(RawFinding(
                entity_type='demand',
                entity_id=d['id'],
                entity_href=d['meta']['href'],
                entity_name=f'Отгрузка №{d["name"]} от {d["moment"][:10]}',
                severity=self.default_severity,
                payload={
                    'moment': (d.get('moment') or '')[:16],
                    'agent': ((d.get('agent') or {}).get('name')
                              if isinstance(d.get('agent'), dict) else None),
                    'description': (d.get('description') or '')[:300],
                    'customer_order': (f'Заказ покупателя №{order["name"]}'
                                       if order.get('name') else None),
                    'order_comment': (order.get('description') or '')[:300],
                    'shipment_address': (d.get('shipmentAddress') or '')[:100],
                    'note': ('По стандарту компании причины (подарок, комиссия, призы) '
                             'пишутся в комментарии связанного заказа покупателя, '
                             'а комментарий отгрузки — только про накладные расходы. '
                             'Смотри оба комментария.'),
                },
                fingerprint_salt='',
            ))
        return out

    def explain(self, payload: dict) -> str:
        return ('Проведённая отгрузка на 0 ₽. Если это подарок или комиссия — '
                'причина должна быть в комментарии отгрузки или связанного заказа; '
                'если нет — забыли цены.')


class DemandNoOverheadCheck(CheckSpec):
    """Отгрузка без накладных расходов и без причины в комментариях.

    Стандарт владельца: в отгрузке указываются накладные расходы (доставка),
    либо причина их отсутствия читается из комментария отгрузки или связанного
    заказа (самовывоз, маркетплейс забирает сам, доставка за счёт покупателя).
    LLM решает по обоим комментариям и контрагенту."""

    id = 'demand_no_overhead'
    section = Section.SALES
    title = 'Отгрузка без накладных расходов'
    default_severity = Severity.WARNING

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        filters = (f'updated>={format_moment(since)}' if since
                   else f'moment>={ctx.scan_since_moment}')
        docs = await ctx.client.list_entities(
            ctx.session, 'demand',
            filters=filters + ';applicable=true',
            expand='agent,customerOrder',
            order='moment,desc',
        )
        out = []
        grace = ctx.now - timedelta(hours=config.AUDIT_DEMAND_GRACE_HOURS)
        for d in docs:
            if (d.get('overhead') or {}).get('sum', 0) > 0:
                continue
            agent_name = ((d.get('agent') or {}).get('name')
                          if isinstance(d.get('agent'), dict) else None)
            if is_marketplace(agent_name):
                # товар отдают в ПВЗ, дальше везёт маркетплейс — накладных расходов
                # тут не бывает вовсе, спрашивать про них незачем
                continue
            # накладные расходы и комментарий часто вносят на следующий день
            try:
                if parse_moment(d['moment']) > grace:
                    continue
            except (KeyError, ValueError):
                pass
            order = d.get('customerOrder') or {}
            out.append(RawFinding(
                entity_type='demand',
                entity_id=d['id'],
                entity_href=d['meta']['href'],
                entity_name=f'Отгрузка №{d["name"]} от {d["moment"][:10]}',
                severity=self.default_severity,
                payload={
                    'moment': (d.get('moment') or '')[:16],
                    'agent': agent_name,
                    'sum_kopecks': d.get('sum', 0),
                    'description': (d.get('description') or '')[:300],
                    'customer_order': (f'Заказ покупателя №{order["name"]}'
                                       if order.get('name') else None),
                    'order_comment': (order.get('description') or '')[:300],
                    'note': ('Накладные расходы отгрузки = доставка до покупателя. '
                             'Их отсутствие — норма, если из комментариев отгрузки/заказа '
                             'или контрагента ясно почему: самовывоз, маркетплейс '
                             'забирает сам, доставка за счёт '
                             'покупателя отдельным платежом. По стандарту причина обычно '
                             'написана в комментарии связанного заказа. Если причины '
                             'не видно нигде — доставку, вероятно, забыли учесть, '
                             'и прибыль по отгрузке завышена.'),
                },
                fingerprint_salt='',
                ui_link=(d.get('meta') or {}).get('uuidHref', ''),
            ))
        return out

    def explain(self, payload: dict) -> str:
        return ('Отгрузка без накладных расходов и без причины в комментарии — '
                'если доставка была, её стоимость потеряна и прибыль завышена.')


class DemandOverheadPaymentCheck(CheckSpec):
    """Накладные расходы отгрузки без парного платежа на ту же сумму.

    Стандарт владельца: сумма накладных расходов отгрузки (доставка) должна
    существовать отдельным исходящим платежом/РКО с галкой «Без закрывающих
    документов» и БЕЗ привязки к отгрузке/заказу (привязка ломает баланс
    контрагента — это не деньги за товар); связь — по сумме и комментарию.

    Исключение — перевозчики со сводным счётом за период (СДЭК, Яндекс, Озон):
    платежа на сумму конкретной отгрузки у них не бывает, такие отгрузки не сигналим.
    Связь платежа с документом ищем и по номеру в назначении: суммы расходятся,
    когда счёт перевозчика отличается от накладных расходов в отгрузке."""

    id = 'demand_overhead_payment'
    section = Section.SALES
    title = 'Накладные расходы отгрузки без парного платежа'
    default_severity = Severity.WARNING
    supports_incremental = False   # сверка сумм по всему окну

    _MATCH_WINDOW_DAYS = 21

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        demands = await ctx.client.list_entities(
            ctx.session, 'demand',
            filters=f'moment>={ctx.scan_since_moment};applicable=true',
            expand='agent',
            order='moment,desc',
        )
        payments = []
        for entity in ('paymentout', 'cashout'):
            for p in await ctx.cached_list(
                    f'{entity}_3m', entity,
                    filters=f'moment>={ctx.scan_since_moment}',
                    expand='agent', order='moment,desc'):
                if p.get('applicable') is False:
                    continue
                payments.append(p)

        out = []
        grace = ctx.now - timedelta(hours=config.AUDIT_DEMAND_GRACE_HOURS)
        for d in demands:
            overhead = (d.get('overhead') or {}).get('sum', 0)
            if overhead <= 0:
                continue
            delivery = delivery_method(d)
            if is_consolidated_carrier(delivery):
                # сводный счёт за период — платежа под конкретную отгрузку не бывает
                continue
            try:
                d_moment = parse_moment(d['moment'])
            except (KeyError, ValueError):
                continue
            # платёж за доставку часто проводят на следующий день — сутки не сигналим
            if d_moment > grace:
                continue
            matched = None
            for p in payments:
                # пара — либо совпадение суммы, либо прямая ссылка на документ
                # в назначении платежа («Доставка по отгрузке № 00148»):
                # стоимость доставки в отгрузке и в счёте перевозчика часто расходятся
                if (p.get('sum', 0) != overhead
                        and not mentions_document(_payment_text(p), d.get('name', ''))):
                    continue
                try:
                    gap = abs((parse_moment(p['moment']) - d_moment).days)
                except (KeyError, ValueError):
                    continue
                if gap <= self._MATCH_WINDOW_DAYS:
                    matched = p
                    break
            if matched is not None:
                continue
            out.append(RawFinding(
                entity_type='demand',
                entity_id=d['id'],
                entity_href=d['meta']['href'],
                entity_name=f'Отгрузка №{d["name"]} от {d["moment"][:10]}',
                severity=self.default_severity,
                payload={
                    'moment': (d.get('moment') or '')[:16],
                    'agent': ((d.get('agent') or {}).get('name')
                              if isinstance(d.get('agent'), dict) else None),
                    'overhead_kopecks': overhead,
                    'delivery_method': delivery,
                    'description': (d.get('description') or '')[:300],
                    'note': (f'В отгрузке №{d["name"]} указаны накладные расходы '
                             f'{overhead / 100:.2f} ₽'
                             + (f' (способ доставки: {delivery})' if delivery else '')
                             + f', но исходящего платежа/РКО на эту сумму '
                             f'(±{self._MATCH_WINDOW_DAYS} дн.) не найдено.'),
                    'fix_hint': ('Сами накладные расходы отгрузки в баланс контрагента '
                                 'НЕ входят: они распределяются на себестоимость позиций. '
                                 'Риск другой — расход на доставку не проведён в Деньгах, '
                                 'поэтому прибыль завышена. '
                                 'Стандарт: платёж за доставку — ОТДЕЛЬНЫЙ исходящий '
                                 'платёж или РКО с галкой «Без закрывающих документов», '
                                 'БЕЗ привязки к отгрузке или заказу (привязка сломала бы '
                                 'баланс контрагента — это не деньги за товар). '
                                 'Если оплата была — создать такой платёж на перевозчика '
                                 '(create_payment, статья расходов «Логистика»), связь '
                                 'с отгрузкой только через комментарий. НИКОГДА не '
                                 'предлагай привязывать платёж за доставку к документам. '
                                 'Если сумма накладных ошибочна — убрать её из отгрузки.'),
                },
                fingerprint_salt=str(overhead),
                ui_link=(d.get('meta') or {}).get('uuidHref', ''),
            ))
        return out

    def explain(self, payload: dict) -> str:
        return (f'Накладные расходы {payload.get("overhead_kopecks", 0) / 100:,.2f} ₽ '
                f'без парного платёжного документа — деньги за доставку не отражены '
                f'в Деньгах.').replace(',', ' ')
