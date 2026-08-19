"""Доставка находок владельцу в Telegram.

Формат сообщения: что не так / почему (от LLM-аналитика) / варианты с последствиями,
плюс кнопки [Принято] [Игнорировать] [Это норм — не показывать]. Baseline-режим —
одна сводка вместо потока сообщений.
"""

import json
from html import escape

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from core import config
from core.database import Finding, FindingStatus
from core.logger import logger
from services.audit.specs import SEVERITY_EMOJI, Severity, web_link
from shared import session_scope
from shared.constants import CallbackPrefix


def finding_keyboard(finding_id: int, nav_rows: list | None = None) -> InlineKeyboardMarkup:
    """Две кнопки-решения: «Ок» закрывает этот случай (при изменении сигнала бот
    напомнит снова), «Больше не показывать» замолкает по документу навсегда.
    nav_rows — дополнительные ряды навигации (следующая/к категориям)."""
    rows = [
        [
            InlineKeyboardButton(text='💬 Обсудить и исправить',
                                 callback_data=f'{CallbackPrefix.AUDIT_TALK}{finding_id}'),
        ],
        [
            InlineKeyboardButton(text='✅ Ок', callback_data=f'{CallbackPrefix.AUDIT_IGNORE}{finding_id}'),
            InlineKeyboardButton(text='🔕 Больше не показывать',
                                 callback_data=f'{CallbackPrefix.AUDIT_MUTE}{finding_id}'),
        ],
    ]
    if nav_rows:
        rows.extend(nav_rows)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _payload_comment(payload: dict) -> str:
    """Комментарий документа из payload — детекторы кладут его по-разному."""
    return (payload.get('description')
            or (payload.get('doc') or {}).get('description')
            or '').strip()


def format_finding(finding: Finding) -> str:
    payload = json.loads(finding.payload or '{}')
    emoji = SEVERITY_EMOJI.get(Severity(finding.severity), '🟡')
    link = payload.get('ui_link') or web_link(finding.entity_type, finding.entity_id)
    header = (
        f'{emoji} <b>{finding.section} · {finding.title}</b>\n'
        f'<a href="{link}">{finding.entity_name or finding.entity_id}</a>\n'
    )
    comment = _payload_comment(payload)
    header += (f'💬 <i>{comment[:250]}</i>\n\n' if comment else '\n')
    llm = payload.get('llm')
    if llm:
        body = llm.get('explanation', '')
        suggestions = llm.get('suggestions') or []
        if suggestions:
            body += '\n\n<b>Варианты решения:</b>\n' + '\n'.join(
                f'{i}. {s}' for i, s in enumerate(suggestions, 1)
            )
        if llm.get('verdict') == 'unclear':
            body += '\n\n❓ <i>Аналитик не уверен — нужно твоё решение.</i>'
        return header + body
    # Фолбэк без вердикта LLM (аналитик был недоступен при создании находки):
    # человеческое объяснение проверки, а не сырой JSON.
    from services.audit.checks import registry_by_id
    check = registry_by_id().get(finding.check_id)
    if check is not None:
        return header + check.explain(payload)
    return header + escape(finding.title or 'Требуется проверка вручную.')


class AuditNotifier:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.owner_id = config.AUDIT_OWNER_TELEGRAM_ID

    async def deliver(self, findings: list[Finding]):
        if not findings:
            return
        is_baseline = getattr(findings[0], '_is_baseline', False)
        if is_baseline or len(findings) > config.AUDIT_MAX_MESSAGES_PER_RUN:
            await self._send_digest(findings, is_baseline)
            # Критичные шлём по одному даже при переполнении
            critical = [f for f in findings if f.severity == Severity.CRITICAL.value]
            for f in critical[:config.AUDIT_MAX_MESSAGES_PER_RUN]:
                await self._send_one(f)
        else:
            for f in findings:
                await self._send_one(f)

    async def _send_one(self, finding: Finding):
        try:
            msg = await self.bot.send_message(
                self.owner_id,
                format_finding(finding),
                reply_markup=finding_keyboard(finding.id),
                disable_web_page_preview=True,
            )
            async with session_scope() as db:
                row = await db.get(Finding, finding.id)
                if row is not None:
                    row.status = FindingStatus.NOTIFIED
                    row.tg_message_id = msg.message_id
        except Exception as e:
            logger.warning(f'[audit] не смог отправить finding {finding.id}: {e}')

    async def _send_digest(self, findings: list[Finding], is_baseline: bool):
        by_section: dict[str, dict[str, int]] = {}
        for f in findings:
            sec = by_section.setdefault(f.section, {})
            sec[f.severity] = sec.get(f.severity, 0) + 1
        lines = []
        for section, sevs in sorted(by_section.items()):
            parts = ', '.join(
                f'{SEVERITY_EMOJI.get(Severity(s), "")} {n}' for s, n in sorted(sevs.items())
            )
            lines.append(f'• {section}: {parts}')
        title = ('🧾 <b>Первичный аудит учёта</b> — вот что накопилось.\n'
                 if is_baseline else '🧾 <b>Аудит: новые находки</b>\n')
        text = (
            f'{title}\n' + '\n'.join(lines) +
            f'\n\nВсего: {len(findings)}. Разбор по одной: /audit_findings'
        )
        try:
            await self.bot.send_message(self.owner_id, text)
            async with session_scope() as db:
                for f in findings:
                    row = await db.get(Finding, f.id)
                    if row is not None and row.status == FindingStatus.NEW:
                        row.status = FindingStatus.NOTIFIED
        except Exception as e:
            logger.warning(f'[audit] не смог отправить дайджест: {e}')

    async def send_daily_summary(self, new_count: int):
        """Утренняя сводка после ночного полного скана — даже если новых нет."""
        from sqlalchemy import func, select
        async with session_scope() as db:
            pending = (await db.execute(
                select(func.count()).where(Finding.status.in_(
                    (FindingStatus.NEW, FindingStatus.NOTIFIED)))
            )).scalar_one()
        lines = ['🌅 <b>Ночная проверка учёта завершена</b>\n']
        lines.append('Новых проблем не найдено 🎉' if new_count == 0
                     else f'Новых находок: <b>{new_count}</b> (прислал выше)')
        if pending:
            lines.append(f'Ждут разбора: {pending} — /audit_findings')
        # счётчик «закрылись сами» из сводки убран: владельцу нечего с ним делать,
        # а число росло из прогона в прогон и выглядело как незакрытый долг
        try:
            await self.bot.send_message(self.owner_id, '\n'.join(lines))
        except Exception as e:
            logger.warning(f'[audit] сводка не отправилась: {e}')

    async def notify_error(self, text: str):
        try:
            await self.bot.send_message(self.owner_id, f'⚠️ Аудит: прогон завершился с ошибкой:\n<pre>{text[:500]}</pre>')
        except Exception:
            pass
