"""Прогон аудита: детекторы собирают сигналы → LLM-аналитик выносит вердикт →
новые findings сохраняются и уходят владельцу.

Дедупликация — по fingerprint: одна и та же находка не тревожит повторно.
Вердикт 'ok' от аналитика гасит сигнал (запись остаётся со статусом ignored — видно,
что LLM счёл это нормой). Baseline-режим: самый первый скан не спамит сообщениями.
"""

import json
from datetime import datetime, timedelta

import aiohttp
from sqlalchemy import select

from core import config
from core.database import AuditMute, AuditRun, Finding, FindingStatus
from core.logger import logger
from integrations.moysklad_audit import MoySkladAuditClient
from services.audit.analyst import AuditAnalyst
from services.audit.context import AuditContext
from services.audit.specs import CheckSpec, RawFinding, Severity, fingerprint
from shared import session_scope


class AuditRunner:
    def __init__(self, checks: list[CheckSpec], analyst: AuditAnalyst | None = None):
        self.checks = checks
        self.analyst = analyst

    async def run(self, run_type: str) -> list[Finding]:
        """Выполнить прогон. Возвращает новые findings со статусом NEW (для уведомлений)."""
        started = datetime.now()
        client = MoySkladAuditClient()

        async with session_scope() as db:
            is_baseline = (await db.execute(select(AuditRun.id).limit(1))).first() is None
            since = None if run_type == 'full' else await self._last_success_since(db)
            run = AuditRun(run_type=run_type, started_at=started)
            db.add(run)
            await db.flush()
            run_id = run.id

        new_findings: list[Finding] = []
        seen_fingerprints: set[str] = set()
        error_text = None

        try:
            async with aiohttp.ClientSession() as http_session:
                ctx = AuditContext(client, http_session)
                for check in self.checks:
                    if since is not None and not check.supports_incremental:
                        continue
                    try:
                        raws = await check.detect(ctx, since)
                    except Exception as e:
                        logger.exception(f'[audit] проверка {check.id} упала')
                        error_text = f'{check.id}: {e}'
                        continue
                    for raw in raws:
                        f = await self._process_raw(check, raw, seen_fingerprints)
                        if f is not None:
                            new_findings.append(f)

            if run_type == 'full':
                await self._resolve_vanished(seen_fingerprints)
        except Exception as e:
            logger.exception('[audit] прогон упал')
            error_text = str(e)

        async with session_scope() as db:
            run = await db.get(AuditRun, run_id)
            run.finished_at = datetime.now()
            run.status = 'error' if error_text else 'ok'
            run.error_text = error_text
            run.findings_new = len(new_findings)
            run.api_calls = client.http.request_count

        logger.info(
            f'[audit] {run_type}: новых={len(new_findings)}, '
            f'api={client.http.request_count}, baseline={is_baseline}'
        )
        # В baseline-режиме находки не рассылаются по одной — notifier пришлёт сводку
        for f in new_findings:
            f._is_baseline = is_baseline  # маркер для notifier, в БД не хранится
        return new_findings

    async def _last_success_since(self, db) -> datetime:
        """Нижняя граница инкрементального прогона: последний успешный финиш − 5 минут."""
        row = (await db.execute(
            select(AuditRun.finished_at)
            .where(AuditRun.status == 'ok')
            .order_by(AuditRun.finished_at.desc())
            .limit(1)
        )).first()
        if row and row[0]:
            return row[0] - timedelta(minutes=5)
        return datetime.now() - timedelta(hours=max(config.AUDIT_INCREMENTAL_MINUTES, 60) // 30)

    async def _process_raw(
        self, check: CheckSpec, raw: RawFinding, seen: set[str]
    ) -> Finding | None:
        fp = fingerprint(check.id, raw)
        seen.add(fp)

        retriage_id = None
        stored: dict = {}
        async with session_scope() as db:
            if await self._is_muted(db, check.id, raw.entity_id):
                return None
            existing = (await db.execute(
                select(Finding).where(Finding.fingerprint == fp)
            )).scalar_one_or_none()
            if existing is not None:
                existing.last_seen_at = datetime.now()
                # освежаем «живые» части карточки: ссылку и комментарий документа —
                # владелец мог поправить комментарий после создания находки
                stored = json.loads(existing.payload or '{}')
                changed = False
                if raw.ui_link and stored.get('ui_link') != raw.ui_link:
                    stored['ui_link'] = raw.ui_link
                    changed = True
                fresh_comment = raw.payload.get('description')
                if fresh_comment is not None and stored.get('description') != fresh_comment:
                    stored['description'] = fresh_comment
                    changed = True
                # note/fix_hint изменились (обновили правила проверки) — старый
                # вердикт LLM устарел, переанализируем с новыми подсказками
                if any(raw.payload.get(k) is not None and stored.get(k) != raw.payload.get(k)
                       for k in ('note', 'fix_hint')):
                    stored.update({k: v for k, v in raw.payload.items() if v is not None})
                    retriage_id = existing.id
                    changed = True
                # находка застряла без вердикта (LLM был недоступен при создании) —
                # долечиваем на ближайшем прогоне, иначе владелец видит сырые факты
                elif 'llm' not in stored and check.llm_triage:
                    retriage_id = existing.id
                if changed:
                    existing.payload = json.dumps(stored, ensure_ascii=False, default=str)

        if retriage_id is not None and check.llm_triage and self.analyst is not None:
            verdict = await self.analyst.triage(check.title, stored)
            if verdict is not None:
                async with session_scope() as db:
                    f = await db.get(Finding, retriage_id)
                    if f is not None:
                        p = json.loads(f.payload or '{}')
                        p['llm'] = verdict
                        f.payload = json.dumps(p, ensure_ascii=False, default=str)
                        # свежий вердикт корректирует и статус: если LLM теперь
                        # счёл нормой — убираем из очереди; важность подтягиваем.
                        # ACKNOWLEDGED не трогаем — владелец уже взял в работу.
                        if f.status in (FindingStatus.NEW, FindingStatus.NOTIFIED):
                            if verdict['verdict'] == 'ok':
                                f.status = FindingStatus.IGNORED
                            else:
                                try:
                                    f.severity = Severity(
                                        verdict.get('severity', f.severity)).value
                                except ValueError:
                                    pass
        if existing is not None:
            return None   # уже тревожили — повторно не уведомляем

        # Новый сигнал: нюансные случаи отдаём на суждение LLM-аналитику
        verdict = None
        if check.llm_triage and self.analyst is not None:
            verdict = await self.analyst.triage(check.title, raw.payload)

        status = FindingStatus.NEW
        severity = raw.severity
        payload = dict(raw.payload)
        if raw.ui_link:
            payload['ui_link'] = raw.ui_link
        if verdict is not None:
            payload['llm'] = verdict
            if verdict['verdict'] == 'ok':
                status = FindingStatus.IGNORED   # LLM счёл нормой — храним, но не тревожим
            else:
                try:
                    severity = Severity(verdict.get('severity', severity.value))
                except ValueError:
                    pass

        now = datetime.now()
        async with session_scope() as db:
            finding = Finding(
                check_id=check.id,
                section=check.section.value,
                severity=severity.value,
                fingerprint=fp,
                entity_type=raw.entity_type,
                entity_id=raw.entity_id,
                entity_href=raw.entity_href,
                entity_name=raw.entity_name,
                title=check.title,
                payload=json.dumps(payload, ensure_ascii=False, default=str),
                status=status,
                first_seen_at=now,
                last_seen_at=now,
            )
            db.add(finding)
            await db.flush()
            db.expunge(finding)

        return finding if status == FindingStatus.NEW else None

    @staticmethod
    async def _is_muted(db, check_id: str, entity_id: str) -> bool:
        row = (await db.execute(
            select(AuditMute.id).where(
                AuditMute.check_id == check_id,
                (AuditMute.entity_id == entity_id) | (AuditMute.entity_id.is_(None)),
            ).limit(1)
        )).first()
        return row is not None

    @staticmethod
    async def _resolve_vanished(seen: set[str]):
        """Полный скан: активные findings, не встреченные в этом прогоне, — проблема ушла."""
        active = (FindingStatus.NEW, FindingStatus.NOTIFIED, FindingStatus.ACKNOWLEDGED)
        async with session_scope() as db:
            rows = (await db.execute(
                select(Finding).where(Finding.status.in_(active))
            )).scalars().all()
            for f in rows:
                if f.fingerprint not in seen:
                    f.status = FindingStatus.RESOLVED
