"""Fix-агент: диалог с владельцем по конкретной находке аудита.

Может читать документ, позиции и историю изменений (audit API), обсуждать
варианты и готовить исправление (prepare_fix) — превью уходит владельцу
на подтверждение, запись в МС только после кнопки [Применить].
"""

import json

import aiohttp

from services.agent_loop import run_agent_step
from services.audit.fix_service import FixPreview, validate_actions
from services.audit.humanize import humanize_money
from services.audit.team_context import SPEAK_RULES, TEAM_CONTEXT
from services.llm_service import LLMClient
from integrations.moysklad_base import new_session

TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'get_document',
            'description': 'Получить основные поля документа МойСклад (суммы, статус, комментарий, даты).',
            'parameters': {
                'type': 'object',
                'properties': {
                    'entity_type': {'type': 'string', 'description': 'Тип: supply, enter, loss, demand, purchaseorder, productiontask, paymentout и т.д.'},
                    'entity_id': {'type': 'string'},
                },
                'required': ['entity_type', 'entity_id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_positions',
            'description': 'Получить позиции документа: id, товар, количество, цена (копейки).',
            'parameters': {
                'type': 'object',
                'properties': {
                    'entity_type': {'type': 'string'},
                    'entity_id': {'type': 'string'},
                },
                'required': ['entity_type', 'entity_id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_audit_events',
            'description': 'История изменений документа: кто, когда и какие поля менял (diff old/new).',
            'parameters': {
                'type': 'object',
                'properties': {
                    'entity_type': {'type': 'string'},
                    'entity_id': {'type': 'string'},
                },
                'required': ['entity_type', 'entity_id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_findings',
            'description': (
                'Список находок аудита из базы бота. Фильтр по статусу: '
                'new/notified (ждут разбора), ignored (скрыто), fixed, acknowledged, resolved.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'status': {'type': 'string', 'description': 'Опционально: фильтр по статусу'},
                    'limit': {'type': 'number', 'description': 'Сколько вернуть (default 15, max 30)'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'close_finding',
            'description': (
                'Закрыть находку аудита по просьбе владельца («закрой её», «разобрались»). '
                'mute=true — если владелец просит больше НИКОГДА не показывать такое '
                'по этому документу.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'finding_id': {'type': 'number', 'description': 'ID находки (из контекста или list_findings)'},
                    'mute': {'type': 'boolean', 'description': 'Замолчать по документу навсегда (default false)'},
                },
                'required': ['finding_id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_audit_stats',
            'description': (
                'Статистика аудита: счётчики находок по статусам, разбивка скрытых '
                '(ИИ посчитал нормой vs скрыл владелец), последние прогоны.'
            ),
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'search_counterparty',
            'description': ('Найти контрагента по названию (например перевозчика: '
                            '«Boxberry», «Почта России»). Возвращает имя и href для '
                            'подстановки в create_payment.'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string', 'description': 'Часть названия контрагента'},
                },
                'required': ['name'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_expense_items',
            'description': ('Справочник статей расходов (имя + href) — для выбора '
                            'expense_item_href в create_payment. Доставка — статья «Логистика».'),
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'prepare_fix',
            'description': (
                'Финальный шаг: подготовить исправление. НЕ применяет изменения — '
                'владелец подтвердит кнопкой. Вызывай только когда владелец согласился '
                'с конкретным вариантом. Допустимые действия: '
                'set_description {entity_type, entity_id, text} — комментарий документа '
                'ИЛИ описание карточки товара (entity_type=product), например пометка '
                '«принят бесплатно, себестоимость 0 корректна»; '
                'set_position_price {entity_type, entity_id, position_id, price_kopecks}; '
                'set_applicable {entity_type, entity_id, applicable}; '
                'set_no_closing_docs {entity_type: paymentout|cashout, entity_id} — '
                'галка «Без закрывающих документов» для платежей за услуги; '
                'delete_document {entity_type, entity_id}; '
                'create_payment {entity_type: paymentout|cashout, agent_href, '
                'sum_kopecks, expense_item_href, purpose, description} — создать '
                'платёж за услуги (доставку): всегда с галкой «Без закрывающих '
                'документов», БЕЗ привязки к документам.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'summary': {'type': 'string', 'description': 'Короткое описание исправления по-русски'},
                    'actions': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'action': {'type': 'string', 'enum': ['set_description', 'set_position_price', 'set_applicable', 'set_no_closing_docs', 'delete_document', 'create_payment']},
                                'entity_type': {'type': 'string'},
                                'entity_id': {'type': 'string', 'description': 'Не нужен для create_payment'},
                                'text': {'type': 'string'},
                                'position_id': {'type': 'string'},
                                'price_kopecks': {'type': 'number'},
                                'applicable': {'type': 'boolean'},
                                'agent_href': {'type': 'string', 'description': 'create_payment: href контрагента-перевозчика'},
                                'sum_kopecks': {'type': 'number', 'description': 'create_payment: сумма в копейках'},
                                'expense_item_href': {'type': 'string', 'description': 'create_payment: href статьи расходов'},
                                'purpose': {'type': 'string', 'description': 'create_payment: назначение платежа'},
                                'description': {'type': 'string', 'description': 'create_payment: комментарий (укажи отгрузку)'},
                            },
                            'required': ['action', 'entity_type'],
                        },
                    },
                },
                'required': ['summary', 'actions'],
            },
        },
    },
]


def _slim_document(d: dict) -> dict:
    attributes = {}
    for a in (d.get('attributes') or []):
        v = a.get('value')
        attributes[a.get('name')] = v.get('name') if isinstance(v, dict) else v
    order_href = ((d.get('customerOrder') or {}).get('meta') or {}).get('href', '')
    return {
        'id': d.get('id'),
        'name': d.get('name'),
        'moment': (d.get('moment') or '')[:16],
        'updated': (d.get('updated') or '')[:16],
        'applicable': d.get('applicable'),
        'sum_kopecks': d.get('sum'),
        'payed_kopecks': d.get('payedSum'),
        'overhead_kopecks': (d.get('overhead') or {}).get('sum'),
        'state': ((d.get('state') or {}).get('name') if isinstance(d.get('state'), dict) else None),
        'agent': ((d.get('agent') or {}).get('name') if isinstance(d.get('agent'), dict) else None),
        'description': (d.get('description') or '')[:400],
        'attributes': attributes or None,       # доп. поля, напр. «Способ доставки»
        'customer_order_id': order_href.rsplit('/', 1)[-1] if order_href else None,
    }


def _general_system_prompt() -> str:
    return (
        f'{TEAM_CONTEXT}\n\n{SPEAK_RULES}\n\n'
        'Ты — ассистент по аудиту учёта МойСклад для владельца компании. '
        'Отвечаешь на вопросы о находках аудита, статистике и учёте. '
        'Можешь смотреть список находок (list_findings), статистику (get_audit_stats), '
        'конкретные документы (get_document, get_positions) и историю их изменений '
        '(get_audit_events). По явному согласию владельца готовишь исправление (prepare_fix) — '
        'оно применится только после его кнопки-подтверждения.\n\n'
        'Правила:\n'
        '• Не выдумывай данные — сначала возьми их через tools.\n'
        '• Суммы и цены в МойСклад — в КОПЕЙКАХ; пользователю показывай в рублях.\n'
        '• Платежи за услуги/доставку НИКОГДА не привязывай к отгрузкам и заказам — '
        'это ломает баланс контрагента (не деньги за товар). Такой платёж создаётся '
        'отдельно (create_payment) с галкой «Без закрывающих документов»: перевозчика '
        'определяй САМ по полю «Способ доставки» отгрузки (псевдонимы контрагентов — '
        'в контексте компании выше), контрагента найди через search_counterparty, '
        'статья расходов — из list_expense_items (доставка → «Логистика»), '
        'в комментарии укажи отгрузку. НЕ спрашивай, кому платили, и не проси '
        'подтвердить перевозчика, если способ доставки в отгрузке указан. '
        'Единственный уточняющий вопрос — одной короткой строкой: '
        '«С расчётного счёта или наличными/картой?» (paymentout или cashout).\n'
        '• «Скрытые» находки (ignored) — это те, что ИИ-аналитик счёл нормой, плюс те, '
        'что владелец скрыл кнопками «Игнорировать»/«Это норм».\n'
        '• Отвечай ПЛОСКИМ ТЕКСТОМ без Markdown (без **, *, #, `). Списки — «1. … 2. …». '
        'Будь краток, по-русски.\n'
    )


def _system_prompt(finding_context: dict) -> str:
    return (
        f'{TEAM_CONTEXT}\n\n{SPEAK_RULES}\n\n'
        'Ты — помощник владельца компании по исправлению ошибок учёта в МойСклад. '
        'Вы обсуждаете ОДНУ конкретную находку аудита (ниже). Владелец решает — ты помогаешь: '
        'отвечаешь на вопросы, смотришь документ и историю изменений, предлагаешь варианты '
        'с последствиями.\n\n'
        f'НАХОДКА:\n'
        f'{json.dumps(humanize_money(finding_context, keep_raw=True), ensure_ascii=False, default=str)}\n\n'
        'Правила:\n'
        '• Сначала пойми ситуацию (get_document / get_positions / get_audit_events), потом советуй.\n'
        '• НЕ утверждай того, чего не видел в результатах tools (например, что документ '
        'удалён или поля нет). Если данных нет — так и скажи: «в данных не вижу».\n'
        '• В карточке находки рядом стоят две формы одной суммы: «price» — уже в рублях '
        '(её и показывай), «price_kopecks» — сырое значение API для действий. '
        'Из tools суммы приходят в КОПЕЙКАХ — их дели на 100 перед показом.\n'
        '• Цена и себестоимость — за ОДНУ единицу измерения товара (uom: г, мл, шт). '
        'Не пересчитывай в килограммы и литры и не подставляй единицу от себя.\n'
        '• Платежи за услуги/доставку НИКОГДА не привязывай к отгрузкам и заказам — '
        'это ломает баланс контрагента (не деньги за товар). Такой платёж создаётся '
        'отдельно (create_payment) с галкой «Без закрывающих документов»: перевозчика '
        'определяй САМ по полю «Способ доставки» отгрузки (псевдонимы контрагентов — '
        'в контексте компании выше), контрагента найди через search_counterparty, '
        'статья расходов — из list_expense_items (доставка → «Логистика»), '
        'в комментарии укажи отгрузку. НЕ спрашивай, кому платили, и не проси '
        'подтвердить перевозчика, если способ доставки в отгрузке указан. '
        'Единственный уточняющий вопрос — одной короткой строкой: '
        '«С расчётного счёта или наличными/картой?» (paymentout или cashout).\n'
        '• prepare_fix вызывай ТОЛЬКО после явного согласия владельца на конкретный вариант. '
        'После prepare_fix не продолжай диалог — владелец подтвердит кнопкой.\n'
        '• Если исправление не выражается допустимыми действиями — честно скажи, '
        'что это надо сделать руками в МойСклад, и объясни как.\n'
        '• Отвечай ПЛОСКИМ ТЕКСТОМ без Markdown (без **, *, #, `). Списки — «1. … 2. …». '
        'Будь краток, по-русски.\n'
    )


class FixAgent:
    """Диалог по одной находке. История живёт в FSM data."""

    def __init__(self):
        self.llm = LLMClient()

    @staticmethod
    def init_history(finding_context: dict) -> list[dict]:
        return [{'role': 'system', 'content': _system_prompt(finding_context)}]

    @staticmethod
    def init_general_history() -> list[dict]:
        """Общий режим: вопросы по аудиту без привязки к конкретной находке."""
        return [{'role': 'system', 'content': _general_system_prompt()}]

    def _build_dispatch(self, finding_id: int | None) -> dict:
        from integrations.moysklad_audit import MoySkladAuditClient
        from services.audit.checks.cross import _slim_audit_events
        client = MoySkladAuditClient()

        async def get_document(args):
            async with new_session() as s:
                d = await client.http.get(
                    s, f'/entity/{args["entity_type"]}/{args["entity_id"]}', 'expand=agent,state')
            if d is None:
                return json.dumps({'error': 'Документ не найден'}, ensure_ascii=False), None
            return json.dumps(_slim_document(d), ensure_ascii=False), None

        async def get_positions(args):
            async with new_session() as s:
                rows = await client.get_positions(s, args['entity_type'], args['entity_id'])
            slim = [{
                'position_id': p.get('id'),
                'product': (p.get('assortment') or {}).get('name', '?'),
                'quantity': p.get('quantity'),
                'price_kopecks': p.get('price'),
            } for p in rows[:50]]
            return json.dumps({'positions': slim}, ensure_ascii=False), None

        async def get_audit_events(args):
            async with new_session() as s:
                events = await client.entity_audit_events(s, args['entity_type'], args['entity_id'])
            return json.dumps({'events': _slim_audit_events(events, limit=8)},
                              ensure_ascii=False), None

        async def list_findings(args):
            from sqlalchemy import select

            from core.database import Finding, FindingStatus
            from shared import session_scope
            limit = min(int(args.get('limit') or 15), 30)
            async with session_scope() as db:
                q = select(Finding).order_by(Finding.last_seen_at.desc()).limit(limit)
                status = args.get('status')
                if status:
                    try:
                        q = q.where(Finding.status == FindingStatus(status))
                    except ValueError:
                        pass
                rows = (await db.execute(q)).scalars().all()
                out = [{
                    'finding_id': f.id,
                    'check': f.title,
                    'section': f.section,
                    'severity': f.severity,
                    'status': f.status.value,
                    'entity_type': f.entity_type,
                    'entity_id': f.entity_id,
                    'entity_name': f.entity_name,
                    'llm_verdict': (json.loads(f.payload or '{}').get('llm') or {}).get('verdict'),
                } for f in rows]
            return json.dumps({'findings': out}, ensure_ascii=False), None

        async def close_finding(args):
            from datetime import datetime

            from core.database import AuditMute, Finding, FindingStatus
            from shared import session_scope
            fid = int(args.get('finding_id') or 0)
            mute = bool(args.get('mute'))
            async with session_scope() as db:
                f = await db.get(Finding, fid)
                if f is None:
                    return json.dumps({'error': 'Находка не найдена'}, ensure_ascii=False), None
                f.status = FindingStatus.IGNORED
                if mute:
                    db.add(AuditMute(check_id=f.check_id, entity_id=f.entity_id,
                                     created_at=datetime.now()))
                name = f.entity_name
            return json.dumps({
                'status': 'closed',
                'entity_name': name,
                'muted_forever': mute,
            }, ensure_ascii=False), None

        async def get_audit_stats(args):
            from sqlalchemy import func, select

            from core.database import AuditRun, Finding, FindingStatus
            from shared import session_scope
            async with session_scope() as db:
                counts = dict((await db.execute(
                    select(Finding.status, func.count()).group_by(Finding.status)
                )).all())
                ignored = (await db.execute(
                    select(Finding).where(Finding.status == FindingStatus.IGNORED)
                )).scalars().all()
                ai_norm = sum(
                    1 for f in ignored
                    if (json.loads(f.payload or '{}').get('llm') or {}).get('verdict') == 'ok')
                by_check: dict[str, int] = {}
                for f in ignored:
                    by_check[f.title] = by_check.get(f.title, 0) + 1
                runs = (await db.execute(
                    select(AuditRun).order_by(AuditRun.started_at.desc()).limit(5)
                )).scalars().all()
            return json.dumps({
                'counts_by_status': {s.value: n for s, n in counts.items()},
                'ignored_breakdown': {
                    'ai_marked_as_norm': ai_norm,
                    'hidden_by_owner': len(ignored) - ai_norm,
                    'by_check': by_check,
                },
                'recent_runs': [{
                    'type': r.run_type, 'started': str(r.started_at)[:16],
                    'status': r.status, 'new_findings': r.findings_new,
                } for r in runs],
            }, ensure_ascii=False), None

        async def search_counterparty(args):
            name = (args.get('name') or '').strip()
            if not name:
                return json.dumps({'error': 'Пустое название'}, ensure_ascii=False), None
            async with new_session() as s:
                rows = await client.list_entities(
                    s, 'counterparty', filters=f'name~{name}', max_rows=8)
            out = [{'name': r.get('name'), 'agent_href': (r.get('meta') or {}).get('href')}
                   for r in rows]
            return json.dumps({'counterparties': out}, ensure_ascii=False), None

        async def list_expense_items(args):
            async with new_session() as s:
                rows = await client.list_entities(s, 'expenseitem', max_rows=50)
            out = [{'name': r.get('name'),
                    'expense_item_href': (r.get('meta') or {}).get('href')}
                   for r in rows]
            return json.dumps({'expense_items': out}, ensure_ascii=False), None

        async def prepare_fix(args):
            actions = args.get('actions') or []
            error = validate_actions(actions)
            if error:
                return json.dumps({'error': f'Исправление отклонено: {error}'},
                                  ensure_ascii=False), None
            preview = FixPreview(finding_id=finding_id,
                                 summary=args.get('summary', ''), actions=actions)
            return json.dumps({
                'status': 'preview_ready',
                'message': 'Превью исправления показано владельцу, ждём подтверждения',
            }, ensure_ascii=False), preview

        return {
            'get_document': get_document,
            'get_positions': get_positions,
            'get_audit_events': get_audit_events,
            'list_findings': list_findings,
            'close_finding': close_finding,
            'get_audit_stats': get_audit_stats,
            'search_counterparty': search_counterparty,
            'list_expense_items': list_expense_items,
            'prepare_fix': prepare_fix,
        }

    async def step(self, finding_id: int | None, history: list[dict], user_message: str) -> dict:
        dispatch = self._build_dispatch(finding_id)
        return await run_agent_step(self.llm, history, user_message, TOOLS, dispatch)
