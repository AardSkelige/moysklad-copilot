"""Хэндлеры аудита учёта: кнопки на находках и команды владельца."""

from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from core.database import AuditMute, AuditRun, Finding, FindingStatus
from core.logger import logger
from shared import session_scope
from shared.constants import CallbackData, CallbackPrefix
from shared.filters import IsAuditOwnerFilter

router = Router()
router.message.filter(IsAuditOwnerFilter())
router.callback_query.filter(IsAuditOwnerFilter())

_PAGE_SIZE = 5


def _finding_id_from(data: str, prefix: str) -> int | None:
    try:
        return int(data[len(prefix):])
    except ValueError:
        return None


async def _set_status(finding_id: int, status: FindingStatus) -> Finding | None:
    async with session_scope() as db:
        f = await db.get(Finding, finding_id)
        if f is None:
            return None
        f.status = status
        await db.flush()   # UPDATE обязан уйти в БД ДО expunge, иначе изменение теряется
        db.expunge(f)
        return f


def _was_section_browsing(callback: CallbackQuery) -> bool:
    """Карточка открыта из разбора по категориям? (в клавиатуре была навигация).

    У последней находки категории кнопки «Следующая» нет — только «К категориям»,
    поэтому проверяем оба вида навигации."""
    markup = callback.message.reply_markup
    if not markup:
        return False
    for row in markup.inline_keyboard:
        for btn in row:
            data = btn.callback_data or ''
            if data.startswith(CallbackPrefix.AUDIT_SECTION) or data == CallbackData.AUDIT_LIST:
                return True
    return False


async def _advance_after_decision(callback: CallbackQuery, finding: Finding):
    """После решения в режиме разбора — сразу показать следующую находку категории."""
    if not _was_section_browsing(callback):
        return
    try:
        from services.audit.specs import Section as _S
        section = _S(finding.section)
        await _show_one_finding(callback.message, _SECTIONS.index(section), section, 0)
    except Exception:
        logger.exception('advance after decision failed')
        await _show_sections(callback.message)


@router.callback_query(F.data.startswith(CallbackPrefix.AUDIT_ACK))
async def on_ack(callback: CallbackQuery):
    """Легаси-кнопка «Принято» на старых сообщениях — работает как «Ок»."""
    fid = _finding_id_from(callback.data, CallbackPrefix.AUDIT_ACK)
    f = await _set_status(fid, FindingStatus.IGNORED) if fid else None
    if f is None:
        await callback.answer('Находка не найдена')
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer('✅ Ок, закрыл')
    await _advance_after_decision(callback, f)


@router.callback_query(F.data.startswith(CallbackPrefix.AUDIT_IGNORE))
async def on_ignore(callback: CallbackQuery):
    fid = _finding_id_from(callback.data, CallbackPrefix.AUDIT_IGNORE)
    f = await _set_status(fid, FindingStatus.IGNORED) if fid else None
    if f is None:
        await callback.answer('Находка не найдена')
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer('✅ Ок, закрыл. Напомню, только если что-то изменится')
    await _advance_after_decision(callback, f)


@router.callback_query(F.data.startswith(CallbackPrefix.AUDIT_MUTE))
async def on_mute(callback: CallbackQuery):
    """«Это норм»: мьютим пару (проверка, документ) — повторные сигналы не создаются."""
    fid = _finding_id_from(callback.data, CallbackPrefix.AUDIT_MUTE)
    if fid is None:
        await callback.answer('Находка не найдена')
        return
    async with session_scope() as db:
        f = await db.get(Finding, fid)
        if f is None:
            await callback.answer('Находка не найдена')
            return
        f.status = FindingStatus.IGNORED
        db.add(AuditMute(check_id=f.check_id, entity_id=f.entity_id,
                         created_at=datetime.now()))
        await db.flush()
        db.expunge(f)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer('🔕 Замолкаю по этому документу навсегда')
    await _advance_after_decision(callback, f)


async def _pending_count(db) -> int:
    return (await db.execute(
        select(func.count()).where(
            Finding.status.in_((FindingStatus.NEW, FindingStatus.NOTIFIED)))
    )).scalar_one()


async def _dashboard_text() -> str:
    """Мини-дашборд для меню аудита: последний прогон + неразобранное."""
    async with session_scope() as db:
        last = (await db.execute(
            select(AuditRun).where(AuditRun.status == 'ok')
            .order_by(AuditRun.finished_at.desc()).limit(1)
        )).scalar_one_or_none()
        pending = await _pending_count(db)
        fixed = (await db.execute(
            select(func.count()).where(Finding.status == FindingStatus.FIXED)
        )).scalar_one()
    from core import config
    lines = ['🔍 <b>Аудит учёта МойСклад</b>\n']
    if last:
        lines.append(f'Последняя проверка: {last.finished_at:%d.%m %H:%M} '
                     f'({"полная" if last.run_type == "full" else "быстрая"})')
    else:
        lines.append('Проверок ещё не было — запусти первую.')
    lines.append(f'Неразобранных находок: <b>{pending}</b>')
    if fixed:
        lines.append(f'Исправлено через бота: {fixed}')
    lines.append(f'\nСам проверяю каждые {config.AUDIT_INCREMENTAL_MINUTES} мин, '
                 f'полностью — ежедневно в {config.AUDIT_FULL_HOUR}:00.')
    return '\n'.join(lines)


async def _status_text() -> str:
    async with session_scope() as db:
        runs = (await db.execute(
            select(AuditRun).order_by(AuditRun.started_at.desc()).limit(5)
        )).scalars().all()
        counts = dict((await db.execute(
            select(Finding.status, func.count()).group_by(Finding.status)
        )).all())
    if not runs:
        return 'Проверок ещё не было.'
    lines = ['<b>Последние проверки:</b>']
    for r in runs:
        icon = {'ok': '✅', 'error': '❌'}.get(r.status, '⏳')
        kind = 'полная' if r.run_type == 'full' else 'быстрая'
        lines.append(f'{icon} {r.started_at:%d.%m %H:%M} — {kind}, '
                     f'новых находок: {r.findings_new}')

    pending = (counts.get(FindingStatus.NEW, 0)
               + counts.get(FindingStatus.NOTIFIED, 0))
    closed = (counts.get(FindingStatus.IGNORED, 0)
              + counts.get(FindingStatus.ACKNOWLEDGED, 0))
    lines.append('\n<b>Находки за всё время:</b>')
    lines.append(f'📬 Ждут твоего разбора: <b>{pending}</b>')
    if counts.get(FindingStatus.FIXED):
        lines.append(f'🛠 Исправлено через бота: {counts[FindingStatus.FIXED]}')
    if closed:
        lines.append(f'✅ Закрыто: {closed} (ты нажал «Ок» или ИИ посчитал нормой)')
    if counts.get(FindingStatus.RESOLVED):
        lines.append(f'🌱 Исчезли сами: {counts[FindingStatus.RESOLVED]} '
                     f'(при перепроверке проблемы уже не было)')
    if pending:
        lines.append('\nРазобрать: «📋 Неразобранные находки» или /audit_findings')
    return '\n'.join(lines)


async def _run_full_audit(message: Message):
    from services.audit.scheduler import run_audit
    from shared.keyboards import audit_menu_keyboard
    progress = await message.answer(
        '🔍 Проверяю учёт… Обычно пара минут, но если новых сигналов много '
        '(например, после добавления новых проверок) — до 10–15: каждый сигнал '
        'разбирает ИИ-аналитик. Результат пришлю по готовности.'
    )
    # deliver=False: при ручном запуске владелец рядом — не заваливаем чат
    # отдельными сообщениями, только итог; разбор — по категориям
    new_count = await run_audit(message.bot, 'full', deliver=False)
    try:
        await progress.delete()
    except Exception:
        pass
    async with session_scope() as db:
        pending = await _pending_count(db)
    done = (f'✅ Проверка завершена. Новых находок: <b>{new_count}</b>, '
            f'всего ждут разбора: <b>{pending}</b>.')
    await message.answer(done, reply_markup=audit_menu_keyboard())


# --- меню аудита ---

@router.callback_query(F.data == CallbackData.AUDIT_MENU)
async def on_audit_menu(callback: CallbackQuery):
    from shared.keyboards import audit_menu_keyboard
    await callback.answer()
    await callback.message.answer(await _dashboard_text(),
                                  reply_markup=audit_menu_keyboard())


@router.callback_query(F.data == CallbackData.AUDIT_RUN)
async def on_audit_run(callback: CallbackQuery):
    await callback.answer()
    await _run_full_audit(callback.message)


@router.callback_query(F.data == CallbackData.AUDIT_LIST)
async def on_audit_list(callback: CallbackQuery):
    await callback.answer()
    await _show_sections(callback.message)


@router.callback_query(F.data == CallbackData.AUDIT_STATUS)
async def on_audit_status(callback: CallbackQuery):
    from shared.keyboards import audit_menu_keyboard
    await callback.answer()
    await callback.message.answer(await _status_text(),
                                  reply_markup=audit_menu_keyboard())


# --- команды (дублируют кнопки для тех, кто привык к «/») ---

@router.message(Command('audit'))
async def cmd_audit(message: Message):
    await _run_full_audit(message)


@router.message(Command('audit_status'))
async def cmd_audit_status(message: Message):
    await message.answer(await _status_text())


@router.message(Command('audit_findings'))
async def cmd_audit_findings(message: Message):
    """Разбор неразобранных находок: сначала категории, потом по одной."""
    await _show_sections(message)


_PENDING = (FindingStatus.NEW, FindingStatus.NOTIFIED)
# порядок категорий в меню — как в списке разделов бота
from services.audit.specs import Section  # noqa: E402

_SECTIONS = list(Section)


async def _show_sections(message: Message):
    """Первый уровень: категории с количеством неразобранного."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from shared.keyboards import audit_menu_keyboard
    async with session_scope() as db:
        counts = dict((await db.execute(
            select(Finding.section, func.count())
            .where(Finding.status.in_(_PENDING))
            .group_by(Finding.section)
        )).all())
    if not counts:
        await message.answer('Неразобранных находок нет 🎉',
                             reply_markup=audit_menu_keyboard())
        return
    rows = []
    for idx, section in enumerate(_SECTIONS):
        n = counts.get(section.value, 0)
        if not n:
            continue
        rows.append([InlineKeyboardButton(
            text=f'{section.value} ({n})',
            callback_data=f'{CallbackPrefix.AUDIT_SECTION}{idx}:0',
        )])
    rows.append([InlineKeyboardButton(text='◀️ Назад',
                                      callback_data=CallbackData.AUDIT_MENU)])
    total = sum(counts.values())
    await message.answer(
        f'📋 <b>Неразобранные находки: {total}</b>\n\nВыбери категорию:',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith(CallbackPrefix.AUDIT_SECTION))
async def on_section(callback: CallbackQuery):
    await callback.answer()
    try:
        idx_str, offset_str = callback.data[len(CallbackPrefix.AUDIT_SECTION):].split(':')
        idx, offset = int(idx_str), int(offset_str)
        section = _SECTIONS[idx]
    except (ValueError, IndexError):
        await _show_sections(callback.message)
        return
    await _show_one_finding(callback.message, idx, section, offset)


async def _show_one_finding(message: Message, sec_idx: int, section, offset: int):
    """Второй уровень: находки категории по одной, решаем и идём дальше."""
    from aiogram.types import InlineKeyboardButton

    from services.audit.notifier import finding_keyboard, format_finding
    async with session_scope() as db:
        rows = (await db.execute(
            select(Finding)
            .where(Finding.status.in_(_PENDING), Finding.section == section.value)
            .order_by(Finding.severity, Finding.first_seen_at.desc())
            .offset(offset).limit(1)
        )).scalars().all()
        total = (await db.execute(
            select(func.count())
            .where(Finding.status.in_(_PENDING), Finding.section == section.value)
        )).scalar_one()
        for f in rows:
            db.expunge(f)
    if not rows:
        if offset > 0:
            # дошли до конца — начнём сначала (часть могла закрыться по пути)
            await _show_one_finding(message, sec_idx, section, 0)
        else:
            await message.answer(f'В категории «{section.value}» всё разобрано 🎉')
            await _show_sections(message)
        return
    f = rows[0]
    nav = []
    if total > 1:
        next_offset = offset + 1 if offset + 1 < total else 0
        nav.append([InlineKeyboardButton(
            text=f'⏭ Следующая ({offset + 1} из {total})',
            callback_data=f'{CallbackPrefix.AUDIT_SECTION}{sec_idx}:{next_offset}',
        )])
    nav.append([InlineKeyboardButton(text='📂 К категориям',
                                     callback_data=CallbackData.AUDIT_LIST)])
    await message.answer(format_finding(f),
                         reply_markup=finding_keyboard(f.id, nav_rows=nav),
                         disable_web_page_preview=True)


@router.callback_query(F.data.startswith(CallbackPrefix.AUDIT_PAGE))
async def on_page_legacy(callback: CallbackQuery):
    """Легаси-кнопки старых сообщений — ведут в категории."""
    await callback.answer()
    await _show_sections(callback.message)
