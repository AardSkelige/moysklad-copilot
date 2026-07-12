"""Production-агент: оркестрирует цикл LLM ↔ tools для создания ПЗ.

История диалога живёт в FSM data (massive 'messages').
Превью ПЗ возвращается handler-у когда LLM вызывает tool 'prepare_production_task' —
оно тоже сохраняется в FSM, чтобы кнопка [Создать] могла его восстановить.
"""

import json
from dataclasses import asdict
from datetime import date

from services.agent_loop import clean_markdown, run_agent_step
from services.audit.team_context import COMPANY_DESCRIPTION
from services.llm_service import LLMClient
from services.production_task_service import ProductionTaskService


# --- Tool schema для DeepSeek (OpenAI-совместимый формат) ---

TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'find_production_task',
            'description': (
                'Найти существующее производственное задание по запросу пользователя. '
                'Поддерживает: номер ("305", "00305"), относительные ("последнее", "предпоследнее"), '
                'дату ("сегодняшнее", "вчерашнее", "позавчерашнее"). '
                'Возвращает либо одно задание, либо список кандидатов, либо kind=none.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'description': 'Запрос для поиска ПЗ'},
                },
                'required': ['query'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'show_task_rows',
            'description': (
                'Показать текущие позиции существующего ПЗ. Используй перед edit_task '
                'чтобы получить row_id для изменения/удаления конкретной позиции. '
                'Возвращает [{row_id, product_code, product_name, volume}].'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'task_id': {'type': 'string', 'description': 'ID задания из find_production_task'},
                },
                'required': ['task_id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'edit_task',
            'description': (
                'Подготовить превью изменения позиций существующего ПЗ. '
                'Можно добавлять (по коду товара), изменять количество (по row_id из show_task_rows) '
                'и удалять (по row_id). Любой из трёх массивов может быть пустым. '
                'НЕ применяет изменения в МС — возвращает превью для подтверждения кнопкой.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'task_id': {'type': 'string'},
                    'additions': {
                        'type': 'array',
                        'description': 'Новые позиции [{code, quantity}]. Код берётся из search_products.',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'code': {'type': 'string'},
                                'quantity': {'type': 'number'},
                            },
                            'required': ['code', 'quantity'],
                        },
                    },
                    'updates': {
                        'type': 'array',
                        'description': 'Изменение количества [{row_id, new_quantity}].',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'row_id': {'type': 'string'},
                                'new_quantity': {'type': 'number'},
                            },
                            'required': ['row_id', 'new_quantity'],
                        },
                    },
                    'deletions': {
                        'type': 'array',
                        'description': 'Удаление позиций [{row_id}].',
                        'items': {
                            'type': 'object',
                            'properties': {'row_id': {'type': 'string'}},
                            'required': ['row_id'],
                        },
                    },
                },
                'required': ['task_id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'change_task_status',
            'description': (
                'Подготовить смену статуса существующего ПЗ. Сначала найди ПЗ через find_production_task. '
                'НЕ создаёт изменение в МС — возвращает превью, пользователь подтвердит UI-кнопкой.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'task_id': {'type': 'string', 'description': 'ID задания из find_production_task'},
                    'status_name': {'type': 'string', 'description': 'Имя статуса (подготовка/выполнение/наклейки/обклеено/готово). Регистр не важен.'},
                },
                'required': ['task_id', 'status_name'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'search_products',
            'description': (
                'Поиск товаров с техкартой в МойСклад по строке. '
                'Используй когда пользователь сказал ЧТО производить, но не уточнил конкретный товар. '
                'Возвращает список кандидатов с кодом и названием.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                        'description': 'Слово или фраза для поиска, например «шампунь белый» или «основа кондиционера».',
                    },
                },
                'required': ['query'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'prepare_production_task',
            'description': (
                'Финальный шаг. Вызывай ТОЛЬКО когда известно: '
                '(1) конкретный товар (с кодом из search_products), '
                '(2) количество, (3) плановая дата. '
                'Доп.коммент — опционален. '
                'НЕ создаёт ПЗ в МС, только готовит превью. Пользователь подтвердит вручную.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'items': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'code': {'type': 'string', 'description': 'Код товара из search_products (например 2-006)'},
                                'quantity': {'type': 'number', 'description': 'Количество штук (целое или дробное)'},
                            },
                            'required': ['code', 'quantity'],
                        },
                    },
                    'planned_date': {
                        'type': 'string',
                        'description': 'Плановая дата готовности в формате YYYY-MM-DD.',
                    },
                    'comment': {
                        'type': 'string',
                        'description': 'Доп.комментарий от пользователя (например «для выставки в субботу»). Опционально.',
                    },
                },
                'required': ['items', 'planned_date'],
            },
        },
    },
]


def _system_prompt(author_name: str) -> str:
    today = date.today().isoformat()
    weekday = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье'][date.today().weekday()]
    return (
        f'Ты — ассистент по производству для {COMPANY_DESCRIPTION}. '
        f'Помогаешь пользователю «{author_name}» '
        f'работать с производственными заданиями (ПЗ) в МойСклад.\n'
        f'Сегодня {today} ({weekday}).\n\n'
        '== Сценарии ==\n'
        'A. СОЗДАТЬ новое ПЗ — пользователь говорит «сделай N шампуней», «произведи X».\n'
        'B. ИЗМЕНИТЬ СТАТУС существующего ПЗ — «переведи 305 в готово», '
        '«поставь последнему обклеено», «вчерашнее в выполнение». '
        'Сначала find_production_task → потом change_task_status.\n'
        'C. ИЗМЕНИТЬ ПОЗИЦИИ существующего ПЗ — «добавь в 305 ещё 5 шампуней», '
        '«в последнем поменяй кондиционер на 20 шт», «убери из 305 репеллент». '
        'Алгоритм: find_production_task → show_task_rows (получить row_id-ы) → '
        'для добавления нужен код товара (через search_products) → edit_task с тремя массивами. '
        'Каждая правка собирается в один вызов edit_task — даже несколько операций сразу.\n'
        'Если find вернул много кандидатов — задай уточняющий вопрос; '
        'если none — скажи «такое задание не найдено».\n\n'
        '== Для СОЗДАНИЯ нужно собрать ==\n'
        '1. Конкретный товар (search_products).\n'
        '2. Количество штук.\n'
        '3. Плановая дата готовности (формат YYYY-MM-DD).\n'
        '4. Доп.комментарий — ОПЦИОНАЛЬНО, не спрашивай если пользователь сам не упомянул.\n\n'
        '== Правила ==\n'
        '• Если пользователь не сказал что произвести — спроси.\n'
        '• Если не сказал сколько — спроси.\n'
        '• Если не сказал когда — спроси («на какую дату планировать?»).\n'
        '• ВАЖНО: если среди кандидатов поиска есть И полуфабрикат «Основа X», И готовый продукт того же типа, '
        'А пользователь НЕ уточнил какой именно — ОБЯЗАТЕЛЬНО уточни: «Основу или готовый продукт?» '
        'Если пользователь явно сказал «основа X» или «готовый X» — НЕ переспрашивай.\n'
        '• Каждый кандидат search_products помечен флагом is_base.\n'
        '• ПРАВИЛО ТОЧНОГО ВЫБОРА: если пользователь назвал базовое имя без уточнителей '
        '(«основа шампуня», «основа кондиционера», «основа репеллента», «шампунь универсальный»), '
        'а в результатах есть кандидат с буквально совпадающим коротким названием '
        '(например «Основа шампуня 500 мл» при запросе «основа шампуня») — выбирай ЕГО без вопросов, '
        'даже если есть варианты типа «Основа сульфатного шампуня», «Основа бессульфатного шампуня» и т.п. '
        'Пользователь специально не сказал «сульфатный» — значит ему нужен базовый.\n'
        '• Когда несколько кандидатов одного типа БЕЗ явного базового — задай уточняющий вопрос '
        'нумерованным списком с реальными названиями (1., 2., 3.).\n'
        '• Если поиск вернул ровно одного кандидата — бери его сразу без вопросов.\n'
        '• Идиомы количества — НЕ уточняй: «4 пятисотки» = 4 шт по 500 мл, «3 канистры по 3 литра» = 3 шт 3000 мл, '
        '«6 пятилитровок» = 6 шт по 5000 мл. Это очевидно и переспрашивать не надо.\n'
        '• В search_products передавай КОРОТКИЕ ключевые слова без предлогов и числительных. '
        'Хорошо: «основа шампунь», «репеллент 500», «кондиционер сибирский». '
        'Плохо: «основа для шампуня 500 мл», «3 канистры репеллента».\n'
        '• Если поиск ничего не вернул — попробуй ОДИН раз с более коротким запросом '
        '(без указания объёма/количества). Если и так пусто — скажи прямо: '
        '«Такой позиции нет, мы её не производим. Назови другую или отмени.» '
        'НЕ предлагай поискать иначе ещё раз — товара просто нет.\n'
        '• Можешь принимать «сегодня», «завтра», «пятница», «через неделю» — '
        f'но в tool передавай уже YYYY-MM-DD относительно сегодня ({today}).\n'
        '• Будь краток. Один вопрос за раз. Не повторяй уже известное.\n'
        '• Отвечай только на русском.\n'
        '• ВАЖНО: отвечай ПЛОСКИМ ТЕКСТОМ без любого форматирования. '
        'НЕ используй ** для жирного, * для курсива, # для заголовков, ` для кода, [] для ссылок. '
        'Списки — нумерованные «1. … 2. …» через перевод строки. '
        'Бот не парсит Markdown, поэтому форматирование будет отображаться буквально.\n'
        '• Когда ВСЁ собрано — вызови prepare_production_task. После этого '
        'НЕ продолжай диалог — пользователь увидит превью и сам подтвердит кнопкой.\n'
    )


class ProductionAgent:
    """Управляет одним диалогом производства. Состояние хранится в FSM data."""

    def __init__(self):
        self.llm = LLMClient()
        self.svc = ProductionTaskService()

    @staticmethod
    def init_history(author_name: str) -> list[dict]:
        return [{'role': 'system', 'content': _system_prompt(author_name)}]

    def _build_dispatch(self, author_name: str) -> dict:
        """Dispatch-таблица для run_agent_step: имя tool → (json-результат, превью|None)."""
        svc = self.svc
        _PREVIEW_READY = json.dumps({
            'status': 'preview_ready',
            'message': 'Превью показано пользователю, ждём подтверждения',
        }, ensure_ascii=False)

        def _slim(t: dict) -> dict:
            # сокращаем task до полезных полей чтобы не раздувать контекст
            return {
                'id': t.get('id'),
                'name': t.get('name'),
                'description': t.get('description', ''),
                'state_name': (t.get('state', {}) or {}).get('name', ''),
                'moment': t.get('moment', '')[:16],
            }

        async def search_products(args):
            result = await svc.search_products(args.get('query', ''))
            return json.dumps({'found': len(result), 'products': result[:20]},
                              ensure_ascii=False), None

        async def find_production_task(args):
            result = await svc.find_task(args.get('query', ''))
            if result['kind'] == 'one':
                out = {'kind': 'one', 'task': _slim(result['task'])}
            elif result['kind'] == 'many':
                out = {'kind': 'many', 'candidates': [_slim(t) for t in result['candidates']]}
            else:
                out = {'kind': 'none'}
            return json.dumps(out, ensure_ascii=False), None

        async def show_task_rows(args):
            rows = await svc.show_task_rows(args.get('task_id', ''))
            return json.dumps({'rows': rows}, ensure_ascii=False), None

        async def change_task_status(args):
            preview = await svc.prepare_status_change(
                task_id=args.get('task_id', ''),
                status_name=args.get('status_name', ''),
            )
            return _PREVIEW_READY, preview

        async def edit_task(args):
            preview = await svc.prepare_edit(
                task_id=args.get('task_id', ''),
                additions=args.get('additions') or [],
                updates=args.get('updates') or [],
                deletions=args.get('deletions') or [],
            )
            return _PREVIEW_READY, preview

        async def prepare_production_task(args):
            preview = await svc.prepare_preview(
                codes_qty=args.get('items', []),
                planned_date=args.get('planned_date', ''),
                author_name=author_name,
                comment=args.get('comment'),
            )
            return _PREVIEW_READY, preview

        return {
            'search_products': search_products,
            'find_production_task': find_production_task,
            'show_task_rows': show_task_rows,
            'change_task_status': change_task_status,
            'edit_task': edit_task,
            'prepare_production_task': prepare_production_task,
        }

    async def step(self, history: list[dict], user_message: str) -> dict:
        """Один шаг диалога.

        Возвращает один из:
        - {'kind': 'reply', 'text': str, 'history': list}
        - {'kind': 'preview', 'preview': TaskPreview, 'history': list}
        - {'kind': 'error', 'text': str, 'history': list}
        """
        dispatch = self._build_dispatch(_extract_author_name(history))
        return await run_agent_step(self.llm, history, user_message, TOOLS, dispatch)


def _extract_author_name(history: list[dict]) -> str:
    """Извлечь имя автора из системного промпта (там оно встроено)."""
    for msg in history:
        if msg.get('role') != 'system':
            continue
        content = msg.get('content', '')
        # формат: «пользователю «Имя» создать»
        try:
            return content.split('«', 1)[1].split('»', 1)[0]
        except IndexError:
            return 'Авто'
    return 'Авто'


# --- Сериализация TaskPreview для хранения в FSM ---

def preview_to_state(preview) -> dict:
    """Универсальная сериализация: TaskPreview / StatusChangePreview / TaskEditPreview."""
    from services.production_task_service import (
        StatusChangePreview as _Status, TaskEditPreview as _Edit,
    )
    if isinstance(preview, _Status):
        return {'kind': 'change_status', 'data': asdict(preview)}
    if isinstance(preview, _Edit):
        return {'kind': 'edit_task', 'data': asdict(preview)}
    # TaskPreview
    return {
        'kind': 'create_tasks',
        'data': {
            'tasks': [
                {
                    'items': [asdict(i) for i in t.items],
                    'description': t.description,
                    'category': t.category,
                    'is_base': t.is_base,
                    'products_store_id': t.products_store_id,
                }
                for t in preview.tasks
            ],
            'shortages': [asdict(s) for s in preview.shortages],
            'planned_moment': preview.planned_moment,
        },
    }


def preview_from_state(stored: dict):
    """Универсальная десериализация."""
    from services.production_task_service import (
        TaskItem, Shortage, SingleTaskPreview,
        StatusChangePreview as _Status,
        TaskEditPreview as _Edit, EditAddition, EditUpdate, EditDeletion,
    )
    kind = stored.get('kind', 'create_tasks')
    data = stored.get('data', stored)  # обратная совместимость
    if kind == 'change_status':
        return _Status(**data)
    if kind == 'edit_task':
        return _Edit(
            task_id=data['task_id'],
            task_name=data['task_name'],
            description=data['description'],
            additions=[EditAddition(**a) for a in data['additions']],
            updates=[EditUpdate(**u) for u in data['updates']],
            deletions=[EditDeletion(**d) for d in data['deletions']],
        )
    tasks = [
        SingleTaskPreview(
            items=[TaskItem(**i) for i in t['items']],
            description=t['description'],
            category=t['category'],
            is_base=t['is_base'],
            products_store_id=t['products_store_id'],
        )
        for t in data['tasks']
    ]
    return TaskPreview(
        tasks=tasks,
        shortages=[Shortage(**s) for s in data['shortages']],
        planned_moment=data['planned_moment'],
    )
