"""Проверки раздела «Деньги»: дубли платежей и контрагентов, платежи без закрывающих документов."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta

from services.audit.context import AuditContext, parse_moment
from services.audit.specs import CheckSpec, RawFinding, Section, Severity

_DUPLICATE_WINDOW_DAYS = 14


def _agent_href(doc: dict) -> str:
    return ((doc.get('agent') or {}).get('meta') or {}).get('href', '').split('?')[0]


def _days_apart(a: dict, b: dict) -> int | None:
    try:
        return abs((parse_moment(a['moment']) - parse_moment(b['moment'])).days)
    except (KeyError, TypeError, ValueError):
        return None


def _is_returned_overpayment(payment: dict, incoming: list, used: set, days: int) -> bool:
    if ((payment.get('expenseItem') or {}).get('name') or '') != 'Возврат':
        return False
    agent = _agent_href(payment)
    if not agent:
        return False
    for p in incoming:
        apart = _days_apart(payment, p)
        if (p.get('id') not in used and _agent_href(p) == agent and p.get('sum') == payment.get('sum')
                and p.get('applicable') is not False and apart is not None and apart <= days):
            used.add(p.get('id'))
            return True
    return False


def _within_window(sorted_docs: list) -> list:
    """Оставить самую большую группу платежей, попарно ближе окна друг к другу."""
    best: list = []
    for i, (_, anchor) in enumerate(sorted_docs):
        try:
            anchor_dt = parse_moment(anchor['moment'])
        except (KeyError, ValueError):
            continue
        cluster = [sorted_docs[i]]
        for entity, d in sorted_docs[i + 1:]:
            try:
                if parse_moment(d['moment']) - anchor_dt <= timedelta(days=_DUPLICATE_WINDOW_DAYS):
                    cluster.append((entity, d))
            except (KeyError, ValueError):
                continue
        if len(cluster) > len(best):
            best = cluster
    return best


class PaymentDuplicateCheck(CheckSpec):
    """Два исходящих платежа одному контрагенту на одну сумму в близкие даты.

    Живой кейс: №00082 и №00086 оба проведены на 21 070 ₽ по заказу 00049.
    LLM решает: дубль или законная повторная оплата одинаковых счетов."""

    id = 'payment_duplicate'
    section = Section.MONEY
    title = 'Возможный дубль платежа'
    default_severity = Severity.CRITICAL
    supports_incremental = False   # дубль ищется по всему окну, не по инкременту

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        payments = []
        for entity in ('paymentout', 'cashout'):
            docs = await ctx.cached_list(
                f'{entity}_3m', entity,
                filters=f'moment>={ctx.scan_since_moment}',
                expand='agent', order='moment,desc',
            )
            for d in docs:
                if d.get('applicable') is False:
                    continue
                payments.append((entity, d))

        groups: dict[tuple, list] = defaultdict(list)
        for entity, d in payments:
            agent_href = ((d.get('agent') or {}).get('meta') or {}).get('href', '')
            groups[(agent_href, d.get('sum', 0))].append((entity, d))

        out = []
        for (agent_href, total), docs in groups.items():
            if len(docs) < 2 or total == 0:
                continue
            docs.sort(key=lambda x: x[1].get('moment', ''))
            # Регулярные платежи (аренда, комиссии банка) идут с месячным шагом —
            # дублем считаем только платежи в пределах _DUPLICATE_WINDOW_DAYS
            docs = _within_window(docs)
            if len(docs) < 2:
                continue
            details = [{
                'entity_type': e,
                'entity_id': d.get('id'),
                'name': d.get('name'),
                'moment': (d.get('moment') or '')[:16],
                'sum_kopecks': d.get('sum', 0),
                'purpose': (d.get('paymentPurpose') or '')[:150],
                'description': (d.get('description') or '')[:150],
            } for e, d in docs]
            first_entity, first = docs[0]
            agent_name = ((first.get('agent') or {}).get('name')
                          if isinstance(first.get('agent'), dict) else '')
            out.append(RawFinding(
                entity_type=first_entity,
                entity_id=first['id'],
                entity_href=first['meta']['href'],
                entity_name=(f'{len(docs)} платежа по {total / 100:,.2f} ₽ — '
                             f'{agent_name or "контрагент не указан"}').replace(',', ' '),
                severity=self.default_severity,
                payload={'agent': agent_name, 'sum_kopecks': total, 'payments': details},
                fingerprint_salt='|'.join(d['name'] for _, d in docs),
            ))
        return out

    def explain(self, payload: dict) -> str:
        n = len(payload.get('payments', []))
        return (f'{n} проведённых платежа одному контрагенту на одинаковую сумму — '
                f'возможен дубль, деньги могли уйти дважды.')


class DuplicateCounterpartyCheck(CheckSpec):
    """Две карточки одного контрагента: одинаковый ИНН или одинаковое имя.

    Документы расходятся по двум карточкам, и баланс контрагента перестаёт
    сходиться в обе стороны. Живой кейс: Озон завели второй раз, и за день
    на новую карточку ушло шесть заказов на 61 600 ₽, пока 190 документов
    висели на основной."""

    id = 'duplicate_counterparty'
    section = Section.MONEY
    title = 'Дубль контрагента в справочнике'
    default_severity = Severity.IMPORTANT
    supports_incremental = False

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        agents = await ctx.cached_list('counterparty_all', 'counterparty', max_rows=2000)
        active = [a for a in agents if not a.get('archived')]

        groups: dict[tuple, list] = defaultdict(list)
        for a in active:
            inn = (a.get('inn') or '').strip()
            key = ('инн', inn) if inn else ('имя', (a.get('name') or '').strip().lower())
            if key[1]:
                groups[key].append(a)

        # сколько документов на каждой карточке — показывает, какая основная
        usage: Counter = Counter()
        for entity in ('supply', 'demand', 'customerorder', 'purchaseorder',
                       'paymentin', 'paymentout', 'cashin', 'cashout'):
            for d in await ctx.cached_list(f'{entity}_balance_full', entity,
                                           expand='agent', order='moment,desc', max_rows=2000):
                href = ((d.get('agent') or {}).get('meta') or {}).get('href', '')
                if href:
                    usage[href.split('/')[-1].split('?')[0]] += 1

        out = []
        for (kind, value), items in groups.items():
            if len(items) < 2:
                continue
            cards = sorted(({
                'name': a.get('name'),
                'inn': (a.get('inn') or '').strip() or None,
                'created': (a.get('created') or '')[:10],
                'documents': usage.get(a['id'], 0),
            } for a in items), key=lambda c: -c['documents'])
            main = items[0]
            out.append(RawFinding(
                entity_type='counterparty',
                entity_id=main['id'],
                entity_href=main['meta']['href'],
                entity_name=f'{main.get("name")} — {len(items)} карточки',
                severity=self.default_severity,
                payload={
                    'match_by': 'ИНН' if kind == 'инн' else 'название',
                    'value': value,
                    'cards': cards,
                    'note': ('Документы одного контрагента разошлись по разным карточкам: '
                             'баланс, взаиморасчёты и история покупок считаются по каждой '
                             'отдельно. Исправление: перенести документы на карточку, где '
                             'их больше, и убрать вторую в архив.'),
                },
                fingerprint_salt='|'.join(sorted(a['id'] for a in items)),
                ui_link=(main.get('meta') or {}).get('uuidHref', ''),
            ))
        return out

    def explain(self, payload: dict) -> str:
        cards = payload.get('cards', [])
        counts = ' и '.join(f'{c["documents"]} док.' for c in cards)
        return (f'Контрагент заведён дважды (совпадает {payload.get("match_by")}): '
                f'{counts}. Баланс и взаиморасчёты разъезжаются.')




class PaymentNoClosingDocsCheck(CheckSpec):
    """Исходящий платёж без привязки и без галки «Без закрывающих документов».

    Такой платёж навсегда повисает в балансе контрагента как «нам должны»
    (живой кейс платежей за доставку: галка выравнивает баланс). LLM по
    назначению отличает оплату услуг (нужна галка) от аванса за товар
    (привязка появится с приёмкой)."""

    id = 'payment_no_closing_docs'
    section = Section.MONEY
    title = 'Платёж без привязки и без галки «Без закрывающих документов»'
    default_severity = Severity.WARNING
    supports_incremental = False

    _GRACE_DAYS = 7   # свежий аванс ещё ждёт свою приёмку — не сигналим
    _REFUND_DAYS = 10

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        incoming = []
        for entity in ('paymentin', 'cashin'):
            incoming += await ctx.cached_list(
                f'{entity}_balance_full', entity, expand='agent', order='moment,desc', max_rows=2000,
            )
        used: set = set()
        out = []
        for entity in ('paymentout', 'cashout'):
            docs = await ctx.cached_list(
                f'{entity}_3m_items', entity,
                filters=f'moment>={ctx.scan_since_moment}',
                expand='agent,expenseItem', order='moment,desc',
            )
            for d in docs:
                if d.get('applicable') is False:
                    continue
                if d.get('operations') or d.get('noClosingDocs'):
                    continue
                if _is_returned_overpayment(d, incoming, used, self._REFUND_DAYS):
                    continue
                try:
                    age_days = (ctx.now - parse_moment(d['moment'])).days
                except (KeyError, ValueError):
                    continue
                if age_days < self._GRACE_DAYS:
                    continue
                out.append(RawFinding(
                    entity_type=entity,
                    entity_id=d['id'],
                    entity_href=d['meta']['href'],
                    entity_name=f'Платёж №{d["name"]} от {d["moment"][:10]}',
                    severity=self.default_severity,
                    payload={
                        'agent': ((d.get('agent') or {}).get('name')
                                  if isinstance(d.get('agent'), dict) else None),
                        'sum_kopecks': d.get('sum', 0),
                        'age_days': age_days,
                        'purpose': (d.get('paymentPurpose') or '')[:200],
                        'description': (d.get('description') or '')[:200],
                        'fix_hint': ('Если это оплата услуг (доставка, аренда, банк) — '
                                     'поставить галку «Без закрывающих документов» '
                                     '(set_no_closing_docs). Если аванс за товар — '
                                     'привязать к заказу/приёмке.'),
                    },
                    fingerprint_salt='',
                ))
        return out

    def explain(self, payload: dict) -> str:
        return (f'Платёж {payload.get("sum_kopecks", 0) / 100:,.2f} ₽ висит '
                f'{payload.get("age_days")} дн. без привязки и без галки «Без закрывающих '
                f'документов» — искажает баланс контрагента.').replace(',', ' ')
