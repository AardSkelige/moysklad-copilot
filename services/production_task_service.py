"""Бизнес-логика создания производственных заданий.

Используется LLM-агентом через function-calling tools.
Все методы возвращают чистые данные (dict/dataclass) пригодные для отправки в LLM.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from core import config
from core.logger import logger
from integrations.moysklad_production import MoySkladProductionClient


@dataclass
class TaskItem:
    code: str
    name: str
    quantity: float
    plan_id: str


@dataclass
class Shortage:
    code: str
    name: str
    needed: float
    in_stock: float

    @property
    def deficit(self) -> float:
        return self.needed - self.in_stock


@dataclass
class SingleTaskPreview:
    """Превью одного ПЗ — одна категория, один склад выпуска."""
    items: list[TaskItem]
    description: str
    category: str
    is_base: bool
    products_store_id: str


@dataclass
class EditAddition:
    plan_id: str
    product_name: str
    volume: float


@dataclass
class EditUpdate:
    row_id: str
    product_name: str
    old_volume: float
    new_volume: float


@dataclass
class EditDeletion:
    row_id: str
    product_name: str
    old_volume: float


@dataclass
class TaskEditPreview:
    """Превью изменений позиций в существующем ПЗ."""
    task_id: str
    task_name: str
    description: str
    additions: list[EditAddition]
    updates: list[EditUpdate]
    deletions: list[EditDeletion]

    @staticmethod
    def _fmt(q: float) -> str:
        return str(int(q)) if q == int(q) else f'{q:g}'

    def to_telegram_message(self) -> str:
        lines = [
            '✏️ <b>Изменить ПЗ</b>',
            '',
            f'<b>ПЗ № {self.task_name}</b> · «{self.description}»',
        ]
        if self.additions:
            lines.append('')
            lines.append('➕ <b>Добавить:</b>')
            for a in self.additions:
                lines.append(f'   • {a.product_name} — {self._fmt(a.volume)} шт')
        if self.updates:
            lines.append('')
            lines.append('🔄 <b>Изменить количество:</b>')
            for u in self.updates:
                lines.append(
                    f'   • {u.product_name}: {self._fmt(u.old_volume)} → '
                    f'<b>{self._fmt(u.new_volume)}</b> шт'
                )
        if self.deletions:
            lines.append('')
            lines.append('🗑 <b>Удалить:</b>')
            for d in self.deletions:
                lines.append(f'   • {d.product_name} (было {self._fmt(d.old_volume)} шт)')

        if not (self.additions or self.updates or self.deletions):
            lines.append('')
            lines.append('(нет изменений)')
        return '\n'.join(lines)


@dataclass
class StatusChangePreview:
    """Превью смены статуса существующего ПЗ."""
    task_id: str
    task_name: str               # '00305'
    description: str             # 'Имя. Комментарий заказавшего'
    current_state_name: str
    new_state_id: str
    new_state_name: str

    def to_telegram_message(self) -> str:
        lines = [
            '🔄 <b>Сменить статус</b>',
            '',
            f'<b>ПЗ № {self.task_name}</b> · «{self.description}»',
            f'Сейчас: <b>{self.current_state_name}</b>',
            f'Будет: <b>{self.new_state_name}</b>',
        ]
        return '\n'.join(lines)


@dataclass
class TaskPreview:
    """Один или несколько ПЗ, которые будут созданы из единого запроса пользователя.
    Позиции автоматически группируются сервисом по категории (Основа Х / Шампуни / ...)."""
    tasks: list[SingleTaskPreview]
    shortages: list[Shortage]          # общий дефицит по всем ПЗ суммарно
    planned_moment: str                # 'YYYY-MM-DD HH:MM:SS' — общий для всех

    @property
    def has_shortages(self) -> bool:
        return bool(self.shortages)

    @staticmethod
    def _fmt_qty(q: float) -> str:
        return str(int(q)) if q == int(q) else f'{q:g}'

    def to_telegram_message(self) -> str:
        """HTML-сообщение для подтверждения в Telegram."""
        n = len(self.tasks)
        if n == 1:
            lines = ['<b>Создам 1 производственное задание:</b>', '']
        else:
            lines = [f'<b>Создам {n} производственных заданий:</b>', '']

        for i, t in enumerate(self.tasks, 1):
            store = 'Производство' if t.is_base else 'Готовая продукция'
            lines.append(f'📋 <b>Задание {i}</b> · «{t.description}»')
            for it in t.items:
                lines.append(f'   • {it.name} — <b>{self._fmt_qty(it.quantity)}</b> шт')
            lines.append(f'   Склад выпуска: {store}')
            lines.append('')

        lines.append(f'<b>Плановая дата (на всех):</b> {self.planned_moment[:10]}')

        if self.shortages:
            lines.append('')
            lines.append('⚠️ <b>Не хватает сырья (суммарно по всем заданиям):</b>')
            for s in self.shortages:
                deficit = int(s.deficit) if s.deficit == int(s.deficit) else round(s.deficit, 2)
                lines.append(f'• {s.name} — не хватает <b>{deficit}</b>')

        lines.append('')
        lines.append('Создаются как <b>черновики</b> (не проведены).')
        return '\n'.join(lines)


# --- Категоризация ---

_BASE_KEYS = [
    ('Основа шампуня', 'Основа шампуня'),
    ('Основа кондиционера', 'Основа кондиционера'),
    ('Основа репеллент', 'Основа репеллента'),
]
_FINAL_KEYS = [
    ('Шампунь', 'Шампуни'),
    ('Кондиционер', 'Кондиционеры'),
    ('Репеллент', 'Репелленты'),
]


def categorize_item(item: TaskItem) -> tuple[str, bool]:
    """Категория для одной позиции: (label, is_base)."""
    name = item.name
    for key, label in _BASE_KEYS:
        if key in name:
            return label, True
    for key, label in _FINAL_KEYS:
        if key in name and 'Основа' not in name:
            return label, False
    return 'Производство', False


def group_items_by_category(items: list[TaskItem]) -> list[tuple[str, bool, list[TaskItem]]]:
    """Сгруппировать позиции по категории. Возвращает [(category, is_base, items)]
    в стабильном порядке: основы сначала, потом готовка."""
    groups: dict[tuple[str, bool], list[TaskItem]] = defaultdict(list)
    for it in items:
        key = categorize_item(it)
        groups[key].append(it)
    # стабильная сортировка: сначала основы (is_base=True), потом по алфавиту названия категории
    ordered = sorted(groups.items(), key=lambda kv: (not kv[0][1], kv[0][0]))
    return [(cat, is_base, lst) for (cat, is_base), lst in ordered]


def build_description(author_name: str, category: str, comment: Optional[str] = None) -> str:
    """Однострочное описание ПЗ: «Имя. Категория[. Доп.коммент]»."""
    parts = [author_name, category]
    if comment and comment.strip():
        parts.append(comment.strip())
    return '. '.join(parts)


# --- Главный сервис ---

class ProductionTaskService:
    # Кеш статусов в рамках жизни процесса: name_lower → state_id
    _states_cache: Optional[dict[str, str]] = None

    def __init__(self):
        self.client = MoySkladProductionClient()

    async def _states(self) -> dict[str, str]:
        if ProductionTaskService._states_cache is None:
            ProductionTaskService._states_cache = await self.client.get_task_states_map()
        return ProductionTaskService._states_cache

    async def find_task(self, query: str) -> dict:
        """Найти ПЗ по запросу.

        Возвращает {'kind': 'one', 'task': dict} либо {'kind': 'many', 'candidates': [dict]}
        либо {'kind': 'none'}.
        """
        from datetime import date as _date, timedelta as _td
        q = query.strip().lower()

        # Конкретный номер
        digits = ''.join(c for c in q if c.isdigit())
        if digits and digits == q.replace(' ', ''):
            task = await self.client.find_task_by_name(digits)
            return {'kind': 'one', 'task': task} if task else {'kind': 'none'}

        # «последнее», «предпоследнее»
        if 'последн' in q or 'недавн' in q:
            recent = await self.client.find_recent_tasks(limit=5)
            if not recent:
                return {'kind': 'none'}
            if 'предпоследн' in q and len(recent) >= 2:
                return {'kind': 'one', 'task': recent[1]}
            return {'kind': 'one', 'task': recent[0]}

        # «сегодняшнее», «вчерашнее»
        today = _date.today()
        if 'сегодн' in q:
            d = today
        elif 'вчер' in q:
            d = today - _td(days=1)
        elif 'позавчер' in q:
            d = today - _td(days=2)
        else:
            d = None

        if d is not None:
            mf = d.strftime('%Y-%m-%d 00:00:00')
            mt = d.strftime('%Y-%m-%d 23:59:59')
            tasks = await self.client.find_tasks_by_date_range(mf, mt, limit=10)
            if not tasks:
                return {'kind': 'none'}
            if len(tasks) == 1:
                return {'kind': 'one', 'task': tasks[0]}
            return {'kind': 'many', 'candidates': tasks}

        return {'kind': 'none'}

    async def prepare_status_change(self, task_id: str, status_name: str) -> StatusChangePreview:
        """Подготовить превью смены статуса. Бросает ValueError если статус неизвестен."""
        states = await self._states()
        target_lower = status_name.strip().lower()

        # точное совпадение
        new_state_id = states.get(target_lower)
        # fuzzy: substring match
        if not new_state_id:
            matches = [(n, sid) for n, sid in states.items() if target_lower in n]
            if len(matches) == 1:
                new_state_name_lc, new_state_id = matches[0]
            elif len(matches) > 1:
                names = ', '.join(n for n, _ in matches)
                raise ValueError(f'Несколько статусов совпали с «{status_name}»: {names}. Уточни.')
            else:
                avail = ', '.join(states.keys())
                raise ValueError(f'Нет статуса «{status_name}». Доступные: {avail}.')

        # Восстановим имя нового статуса в исходном регистре (по id)
        new_state_name = next((k for k, v in states.items() if v == new_state_id), target_lower)

        # Получим текущее ПЗ для отображения текущего статуса/описания
        task = await self.client.get_task_by_id(task_id)

        return StatusChangePreview(
            task_id=task_id,
            task_name=task.get('name', '?'),
            description=task.get('description', '') or '(без описания)',
            current_state_name=(task.get('state', {}) or {}).get('name', '?'),
            new_state_id=new_state_id,
            new_state_name=new_state_name,
        )

    async def apply_status_change(self, preview: StatusChangePreview) -> dict:
        return await self.client.update_task_state(preview.task_id, preview.new_state_id)

    # === Редактирование позиций ===

    async def show_task_rows(self, task_id: str) -> list[dict]:
        """Текущие позиции ПЗ для LLM. row_id нужен чтобы ссылаться на конкретную строку."""
        return await self.client.get_task_rows(task_id)

    async def prepare_edit(
        self,
        task_id: str,
        additions: list[dict],   # [{code, quantity}]
        updates: list[dict],     # [{row_id, new_quantity}]
        deletions: list[dict],   # [{row_id}]
    ) -> TaskEditPreview:
        """Собрать превью изменений ПЗ. Бросает ValueError при невалидных входах."""
        task = await self.client.get_task_by_id(task_id)
        current_rows = await self.client.get_task_rows(task_id)
        rows_by_id = {r['row_id']: r for r in current_rows}

        # Дополнения — резолвим коды в plan_id
        add_objs: list[EditAddition] = []
        if additions:
            plans = await self.client.build_plans_map()
            for a in additions:
                code = (a.get('code') or '').strip()
                qty = float(a.get('quantity', 0))
                if qty <= 0:
                    raise ValueError(f'Количество > 0 ожидается, получено {qty} для кода {code}')
                entry = plans.get(code)
                if not entry:
                    raise ValueError(f'У товара «{code}» нет техкарты или он исключён.')
                add_objs.append(EditAddition(
                    plan_id=entry['plan_id'],
                    product_name=entry['product_name'],
                    volume=qty,
                ))

        # Изменения количества
        upd_objs: list[EditUpdate] = []
        for u in updates or []:
            row_id = u.get('row_id', '')
            new_qty = float(u.get('new_quantity', 0))
            if new_qty <= 0:
                raise ValueError(f'Новое количество > 0 ожидается (row_id={row_id})')
            row = rows_by_id.get(row_id)
            if not row:
                raise ValueError(f'Позиция row_id={row_id} не найдена в ПЗ')
            upd_objs.append(EditUpdate(
                row_id=row_id,
                product_name=row['product_name'] or row['plan_name'],
                old_volume=float(row['volume']),
                new_volume=new_qty,
            ))

        # Удаления
        del_objs: list[EditDeletion] = []
        for d in deletions or []:
            row_id = d.get('row_id', '')
            row = rows_by_id.get(row_id)
            if not row:
                raise ValueError(f'Позиция row_id={row_id} не найдена в ПЗ')
            del_objs.append(EditDeletion(
                row_id=row_id,
                product_name=row['product_name'] or row['plan_name'],
                old_volume=float(row['volume']),
            ))

        return TaskEditPreview(
            task_id=task_id,
            task_name=task.get('name', '?'),
            description=task.get('description', '') or '(без описания)',
            additions=add_objs,
            updates=upd_objs,
            deletions=del_objs,
        )

    async def apply_edits(self, preview: TaskEditPreview) -> dict:
        """Применить изменения: сначала удаления, потом обновления, потом добавления.
        Возвращает сводку выполненных операций."""
        deleted = updated = added = 0
        for d in preview.deletions:
            await self.client.delete_task_row(preview.task_id, d.row_id)
            deleted += 1
        for u in preview.updates:
            await self.client.update_task_row_volume(preview.task_id, u.row_id, u.new_volume)
            updated += 1
        for a in preview.additions:
            await self.client.add_task_row(preview.task_id, a.plan_id, a.volume)
            added += 1
        logger.info(f"Edit ПЗ {preview.task_name}: -{deleted} +{added} ~{updated}")
        return {'deleted': deleted, 'updated': updated, 'added': added}

    async def search_products(self, query: str) -> list[dict]:
        """Поиск товаров с техкартой по запросу пользователя.
        Возвращает кандидатов для уточнения LLM-ом."""
        return await self.client.search_products_with_plans(query)

    async def _resolve_codes(self, codes_qty: list[dict]) -> list[TaskItem]:
        """codes_qty: [{code, quantity}] → [TaskItem]. Ошибка если код без техкарты."""
        plans = await self.client.build_plans_map()
        out, missing = [], []
        for it in codes_qty:
            code, qty = it['code'], float(it['quantity'])
            entry = plans.get(code)
            if not entry:
                missing.append(code)
                continue
            out.append(TaskItem(
                code=code, name=entry['product_name'],
                quantity=qty, plan_id=entry['plan_id'],
            ))
        if missing:
            raise ValueError(f'У товаров нет техкарты: {", ".join(missing)}')
        return out

    async def _compute_shortages(self, items: list[TaskItem]) -> list[Shortage]:
        plan_volumes: dict[str, float] = {}
        for it in items:
            plan_volumes[it.plan_id] = plan_volumes.get(it.plan_id, 0.0) + it.quantity

        materials = await self.client.plan_materials_sum(plan_volumes)
        stock = await self.client.stock_at_production()

        shortages = []
        for mid, info in materials.items():
            in_stock = stock.get(mid, 0)
            if in_stock < info['qty_needed']:
                shortages.append(Shortage(
                    code=info['code'], name=info['name'],
                    needed=info['qty_needed'], in_stock=in_stock,
                ))
        shortages.sort(key=lambda s: s.code)
        return shortages

    async def prepare_preview(
        self,
        codes_qty: list[dict],
        planned_date: str,            # 'YYYY-MM-DD'
        author_name: str,
        comment: Optional[str] = None,
    ) -> TaskPreview:
        """Финальный шаг агента: построить превью.

        Позиции автоматически группируются по категории (Основа Х, Шампуни, ...).
        Если групп несколько — создастся несколько ПЗ. Дефицит сырья общий (суммарный)."""
        all_items = await self._resolve_codes(codes_qty)
        if not all_items:
            raise ValueError('Нет ни одной позиции для производства.')

        # Время по умолчанию — конец рабочего дня (18:00)
        try:
            d = datetime.strptime(planned_date, '%Y-%m-%d')
        except ValueError:
            raise ValueError(f'Неверный формат даты: «{planned_date}», ожидаю YYYY-MM-DD')
        planned_moment = d.replace(hour=18, minute=0, second=0).strftime('%Y-%m-%d %H:%M:%S')

        # Группировка по категориям
        groups = group_items_by_category(all_items)

        tasks: list[SingleTaskPreview] = []
        for category, is_base, items in groups:
            description = build_description(author_name, category, comment)
            products_store_id = (
                config.MOYSKLAD_STORE_MATERIALS_ID if is_base
                else config.MOYSKLAD_STORE_PRODUCTS_ID
            )
            tasks.append(SingleTaskPreview(
                items=items,
                description=description,
                category=category,
                is_base=is_base,
                products_store_id=products_store_id,
            ))

        # Общий дефицит сырья по всем ПЗ суммарно
        shortages = await self._compute_shortages(all_items)

        return TaskPreview(
            tasks=tasks,
            shortages=shortages,
            planned_moment=planned_moment,
        )

    async def create_from_preview(self, preview: TaskPreview) -> list[dict]:
        """Создать N ПЗ как черновиков (один на каждое sub-task). Возвращает список ответов МС."""
        results = []
        for t in preview.tasks:
            plan_volumes: dict[str, float] = {}
            for it in t.items:
                plan_volumes[it.plan_id] = plan_volumes.get(it.plan_id, 0.0) + it.quantity
            res = await self.client.create_production_task(
                plan_volumes=plan_volumes,
                description=t.description,
                products_store_id=t.products_store_id,
                delivery_planned_moment=preview.planned_moment,
                applicable=False,
                reserve=True,
            )
            logger.info(f"ПЗ {res.get('name')} создано · {t.description}")
            results.append(res)
        return results
