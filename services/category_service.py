from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import Category, ApplicableTo, OperationType


async def get_active_categories(
    session: AsyncSession,
    op_type: Optional[OperationType] = None,
) -> list[Category]:
    """Возвращает активные категории, опционально фильтруя по типу операции."""
    query = select(Category).where(Category.is_active == True).order_by(Category.name)

    if op_type is not None:
        applicable_values = [ApplicableTo.BOTH]
        if op_type == OperationType.INCOME:
            applicable_values.append(ApplicableTo.INCOME)
        else:
            applicable_values.append(ApplicableTo.EXPENSE)
        query = query.where(Category.applicable_to.in_(applicable_values))

    result = await session.execute(query)
    return list(result.scalars().all())


async def create_category(
    session: AsyncSession,
    name: str,
    applicable_to: ApplicableTo,
    group: str = '',
    sort_order: int = 0,
    moysklad_expense_item_href: Optional[str] = None,
) -> Category:
    cat = Category(
        name=name,
        group=group,
        applicable_to=applicable_to,
        is_active=True,
        sort_order=sort_order,
        moysklad_expense_item_href=moysklad_expense_item_href,
    )
    session.add(cat)
    await session.flush()
    await session.refresh(cat)
    return cat


async def get_category_by_id(session: AsyncSession, cat_id: int) -> Optional[Category]:
    result = await session.execute(select(Category).where(Category.id == cat_id))
    return result.scalar_one_or_none()


async def rename_category(
    session: AsyncSession,
    cat_id: int,
    new_name: str,
) -> Optional[Category]:
    """Обновить name локально (MoySklad обновляется в сервисном слое до вызова)"""
    result = await session.execute(select(Category).where(Category.id == cat_id))
    cat = result.scalar_one_or_none()
    if cat is None:
        return None
    cat.name = new_name
    await session.flush()
    return cat


async def sync_expense_categories(
    session: AsyncSession,
    ms_items: list[dict],  # [{id, name, href}, ...]
) -> dict:
    """
    Upsert-логика синхронизации категорий расходов из МойСклад:
    - Есть в МС, нет локально (по href) → создать с applicable_to=EXPENSE
    - Есть в МС и локально → обновить name если изменилось
    - Есть локально с href, нет в МС → is_active=False
    - Локальные без href (INCOME или вручную) — не трогать
    """
    ms_by_href = {item['href']: item for item in ms_items}

    # Загрузить все локальные EXPENSE-категории у которых есть href
    result = await session.execute(
        select(Category).where(
            Category.applicable_to.in_([ApplicableTo.EXPENSE, ApplicableTo.BOTH]),
            Category.moysklad_expense_item_href.isnot(None),
        )
    )
    local_with_href = list(result.scalars().all())
    local_by_href = {cat.moysklad_expense_item_href: cat for cat in local_with_href}

    created = 0
    updated = 0
    deleted = 0

    # Элементы из МС
    for href, ms_item in ms_by_href.items():
        if href in local_by_href:
            cat = local_by_href[href]
            if cat.name != ms_item['name']:
                cat.name = ms_item['name']
                updated += 1
            if not cat.is_active:
                cat.is_active = True
            cat.is_system = ms_item.get('is_system', False)
        else:
            cat = Category(
                name=ms_item['name'],
                group='',
                applicable_to=ApplicableTo.EXPENSE,
                is_active=True,
                is_system=ms_item.get('is_system', False),
                sort_order=0,
                moysklad_expense_item_href=href,
            )
            session.add(cat)
            created += 1

    # Локальные категории с href, которых нет в МС → деактивировать (не удалять, чтобы сохранить group)
    for href, cat in local_by_href.items():
        if href not in ms_by_href and cat.is_active:
            cat.is_active = False
            deleted += 1

    await session.flush()
    return {'created': created, 'updated': updated, 'deleted': deleted}
