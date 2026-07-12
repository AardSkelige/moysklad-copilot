"""Тесты AuditRunner: дедупликация, mute, вердикты LLM-аналитика, resolved."""

from contextlib import asynccontextmanager
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.database import AuditMute, AuditRun, Base, Finding, FindingStatus
from services.audit.specs import CheckSpec, RawFinding, Section, Severity

pytestmark = [pytest.mark.asyncio, pytest.mark.unit, pytest.mark.audit]


class StubCheck(CheckSpec):
    id = 'stub_check'
    section = Section.CROSS
    title = 'Стаб-проверка'
    default_severity = Severity.IMPORTANT
    llm_triage = True

    def __init__(self, raws):
        self.raws = raws

    async def detect(self, ctx, since):
        return list(self.raws)

    def explain(self, payload):
        return 'stub'


class StubAnalyst:
    """Аналитик с фиксированным вердиктом."""

    def __init__(self, verdict='problem'):
        self.verdict = verdict
        self.calls = 0

    async def triage(self, check_title, facts):
        self.calls += 1
        return {
            'verdict': self.verdict,
            'severity': 'critical',
            'explanation': 'тестовое объяснение',
            'suggestions': ['Вариант: тест. Последствие: тест.'],
        }


def _raw(entity_id='doc1', salt=''):
    return RawFinding(
        entity_type='supply', entity_id=entity_id,
        entity_href=f'https://api.moysklad.ru/x/{entity_id}',
        entity_name=f'Документ {entity_id}',
        severity=Severity.IMPORTANT,
        payload={'k': 'v'},
        fingerprint_salt=salt,
    )


@pytest_asyncio.fixture
async def audit_env(monkeypatch):
    """In-memory БД + подмена session_scope в runner. Возвращает (runner_module, session_factory)."""
    engine = create_async_engine('sqlite+aiosqlite:///:memory:', echo=False)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def scope():
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    import services.audit.runner as runner_module
    monkeypatch.setattr(runner_module, 'session_scope', scope)
    yield runner_module, factory
    await engine.dispose()


async def _run(runner_module, checks, analyst=None, run_type='full'):
    runner = runner_module.AuditRunner(checks, analyst=analyst)
    return await runner.run(run_type)


class TestRunnerDedup:
    async def test_new_finding_created_and_returned(self, audit_env):
        runner_module, factory = audit_env
        new = await _run(runner_module, [StubCheck([_raw()])], StubAnalyst())
        assert len(new) == 1
        async with factory() as s:
            rows = (await s.execute(select(Finding))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == FindingStatus.NEW
        assert rows[0].severity == 'critical'   # severity поднята аналитиком

    async def test_same_fingerprint_not_repeated(self, audit_env):
        runner_module, factory = audit_env
        analyst = StubAnalyst()
        assert len(await _run(runner_module, [StubCheck([_raw()])], analyst)) == 1
        assert len(await _run(runner_module, [StubCheck([_raw()])], analyst)) == 0
        assert analyst.calls == 1   # LLM не дёргается по известной находке
        async with factory() as s:
            rows = (await s.execute(select(Finding))).scalars().all()
        assert len(rows) == 1

    async def test_mute_suppresses_signal(self, audit_env):
        runner_module, factory = audit_env
        async with factory() as s:
            s.add(AuditMute(check_id='stub_check', entity_id='doc1',
                            created_at=datetime.now()))
            await s.commit()
        assert await _run(runner_module, [StubCheck([_raw('doc1')])], StubAnalyst()) == []
        # другой документ той же проверки — не замьючен
        assert len(await _run(runner_module, [StubCheck([_raw('doc2')])], StubAnalyst())) == 1

    async def test_global_mute_by_check(self, audit_env):
        runner_module, factory = audit_env
        async with factory() as s:
            s.add(AuditMute(check_id='stub_check', entity_id=None,
                            created_at=datetime.now()))
            await s.commit()
        assert await _run(runner_module, [StubCheck([_raw('doc1')])], StubAnalyst()) == []


class TestRunnerAnalyst:
    async def test_ok_verdict_stored_ignored_not_notified(self, audit_env):
        runner_module, factory = audit_env
        new = await _run(runner_module, [StubCheck([_raw()])], StubAnalyst('ok'))
        assert new == []   # норма — владельца не тревожим
        async with factory() as s:
            row = (await s.execute(select(Finding))).scalar_one()
        assert row.status == FindingStatus.IGNORED   # но запись хранится

    async def test_no_analyst_fallback_still_creates(self, audit_env):
        runner_module, factory = audit_env
        new = await _run(runner_module, [StubCheck([_raw()])], analyst=None)
        assert len(new) == 1

    async def test_changed_fix_hint_triggers_retriage(self, audit_env):
        # Обновили правила проверки (note/fix_hint) — старый вердикт LLM
        # переанализируется, но владельца повторно не тревожим
        import json as _json
        runner_module, factory = audit_env
        analyst = StubAnalyst()

        old = _raw()
        old.payload = {'note': 'факты', 'fix_hint': 'старый стандарт'}
        assert len(await _run(runner_module, [StubCheck([old])], analyst)) == 1
        assert analyst.calls == 1

        fresh = _raw()
        fresh.payload = {'note': 'факты', 'fix_hint': 'НОВЫЙ стандарт: не привязывать'}
        assert await _run(runner_module, [StubCheck([fresh])], analyst) == []
        assert analyst.calls == 2   # переанализ случился

        async with factory() as s:
            row = (await s.execute(select(Finding))).scalar_one()
        payload = _json.loads(row.payload)
        assert payload['fix_hint'] == 'НОВЫЙ стандарт: не привязывать'
        assert payload['llm']['explanation'] == 'тестовое объяснение'

    async def test_unchanged_hint_no_retriage(self, audit_env):
        runner_module, factory = audit_env
        analyst = StubAnalyst()
        raw = _raw()
        raw.payload = {'note': 'факты', 'fix_hint': 'стандарт'}
        await _run(runner_module, [StubCheck([raw])], analyst)
        same = _raw()
        same.payload = {'note': 'факты', 'fix_hint': 'стандарт'}
        await _run(runner_module, [StubCheck([same])], analyst)
        assert analyst.calls == 1   # без изменений LLM не дёргаем


class TestRunnerLifecycle:
    async def test_resolved_when_vanished_on_full_scan(self, audit_env):
        runner_module, factory = audit_env
        await _run(runner_module, [StubCheck([_raw('doc1')])], StubAnalyst())
        # проблема исчезла — полный скан без сигналов
        await _run(runner_module, [StubCheck([])], StubAnalyst())
        async with factory() as s:
            row = (await s.execute(select(Finding))).scalar_one()
        assert row.status == FindingStatus.RESOLVED

    async def test_run_recorded(self, audit_env):
        runner_module, factory = audit_env
        await _run(runner_module, [StubCheck([_raw()])], StubAnalyst())
        async with factory() as s:
            run = (await s.execute(select(AuditRun))).scalar_one()
        assert run.status == 'ok'
        assert run.findings_new == 1

    async def test_baseline_marker_on_first_run(self, audit_env):
        runner_module, factory = audit_env
        new = await _run(runner_module, [StubCheck([_raw()])], StubAnalyst())
        assert getattr(new[0], '_is_baseline', False) is True
        new2 = await _run(runner_module, [StubCheck([_raw('doc2')])], StubAnalyst())
        assert getattr(new2[0], '_is_baseline', True) is False


class TestRunAuditDelivery:
    """Ручной запуск (deliver=False) сохраняет находки молча, без сообщений."""

    async def test_manual_run_does_not_deliver(self, monkeypatch):
        from services.audit import scheduler as sched
        notifier_calls = []

        class FakeRunner:
            def __init__(self, *a, **kw): ...
            async def run(self, run_type):
                return ['f1', 'f2', 'f3']

        class FakeNotifier:
            def __init__(self, bot): ...
            async def deliver(self, findings):
                notifier_calls.append('deliver')
            async def send_daily_summary(self, n):
                notifier_calls.append('summary')
            async def notify_error(self, text):
                notifier_calls.append('error')

        monkeypatch.setattr(sched, 'AuditRunner', FakeRunner)
        monkeypatch.setattr(sched, 'AuditNotifier', FakeNotifier)
        monkeypatch.setattr(sched.config, 'DEEPSEEK_API_KEY', '', raising=False)

        assert await sched.run_audit(None, 'full', deliver=False) == 3
        assert notifier_calls == []

        assert await sched.run_audit(None, 'full') == 3
        assert notifier_calls == ['deliver']
