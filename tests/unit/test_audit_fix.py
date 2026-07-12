"""Тесты Фазы 2: agent_loop, валидация исправлений, FSM диалога фикса."""

import json

import pytest

from services.agent_loop import clean_markdown, run_agent_step
from services.audit.fix_service import (
    FixPreview, fix_preview_from_state, fix_preview_to_state, validate_actions,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit, pytest.mark.audit]


class FakeLLM:
    """Отдаёт заранее заданные ответы по очереди."""

    def __init__(self, responses):
        self.responses = list(responses)

    async def chat(self, history, tools=None):
        return self.responses.pop(0)


def _tool_call(name, args, call_id='tc1'):
    return {'id': call_id, 'name': name, 'arguments': args}


class TestAgentLoop:
    async def test_plain_reply(self):
        llm = FakeLLM([{'content': 'Привет!', 'tool_calls': []}])
        result = await run_agent_step(llm, [], 'привет', [], {})
        assert result['kind'] == 'reply'
        assert result['text'] == 'Привет!'
        # история: user + assistant
        assert [m['role'] for m in result['history']] == ['user', 'assistant']

    async def test_tool_then_reply(self):
        llm = FakeLLM([
            {'content': '', 'tool_calls': [_tool_call('lookup', {'q': 'x'})]},
            {'content': 'Нашёл: 42', 'tool_calls': []},
        ])
        calls = []

        async def lookup(args):
            calls.append(args)
            return json.dumps({'result': 42}), None

        result = await run_agent_step(llm, [], 'найди', [], {'lookup': lookup})
        assert result['kind'] == 'reply'
        assert calls == [{'q': 'x'}]
        roles = [m['role'] for m in result['history']]
        assert roles == ['user', 'assistant', 'tool', 'assistant']

    async def test_preview_interrupts_loop(self):
        llm = FakeLLM([
            {'content': '', 'tool_calls': [_tool_call('prepare', {})]},
        ])

        async def prepare(args):
            return json.dumps({'status': 'preview_ready'}), {'my': 'preview'}

        result = await run_agent_step(llm, [], 'сделай', [], {'prepare': prepare})
        assert result['kind'] == 'preview'
        assert result['preview'] == {'my': 'preview'}

    async def test_unknown_tool_reported_not_crash(self):
        llm = FakeLLM([
            {'content': '', 'tool_calls': [_tool_call('nope', {})]},
            {'content': 'ок', 'tool_calls': []},
        ])
        result = await run_agent_step(llm, [], 'x', [], {})
        assert result['kind'] == 'reply'
        tool_msg = next(m for m in result['history'] if m['role'] == 'tool')
        assert 'Unknown tool' in tool_msg['content']

    async def test_tool_exception_becomes_error_result(self):
        llm = FakeLLM([
            {'content': '', 'tool_calls': [_tool_call('boom', {})]},
            {'content': 'не вышло', 'tool_calls': []},
        ])

        async def boom(args):
            raise RuntimeError('взрыв')

        result = await run_agent_step(llm, [], 'x', [], {'boom': boom})
        tool_msg = next(m for m in result['history'] if m['role'] == 'tool')
        assert 'взрыв' in tool_msg['content']

    async def test_max_iterations_guard(self):
        endless = {'content': '', 'tool_calls': [_tool_call('noop', {})]}
        llm = FakeLLM([endless] * 10)

        async def noop(args):
            return '{}', None

        result = await run_agent_step(llm, [], 'x', [], {'noop': noop}, max_iterations=3)
        assert result['kind'] == 'error'

    async def test_clean_markdown(self):
        assert clean_markdown('**жирный** и `код`') == 'жирный и код'


class TestFixValidation:
    async def test_valid_set_description(self):
        assert validate_actions([{
            'action': 'set_description', 'entity_type': 'supply',
            'entity_id': 'abc', 'text': 'Аня: исправлено',
        }]) is None

    async def test_valid_set_position_price(self):
        assert validate_actions([{
            'action': 'set_position_price', 'entity_type': 'supply',
            'entity_id': 'abc', 'position_id': 'p1', 'price_kopecks': 25000,
        }]) is None

    async def test_rejects_unknown_action(self):
        assert validate_actions([{
            'action': 'drop_database', 'entity_type': 'supply', 'entity_id': 'abc',
        }]) is not None

    async def test_rejects_unknown_entity(self):
        assert validate_actions([{
            'action': 'delete_document', 'entity_type': 'organization', 'entity_id': 'abc',
        }]) is not None

    async def test_rejects_negative_price(self):
        assert validate_actions([{
            'action': 'set_position_price', 'entity_type': 'supply',
            'entity_id': 'abc', 'position_id': 'p1', 'price_kopecks': -100,
        }]) is not None

    async def test_rejects_empty_actions(self):
        assert validate_actions([]) is not None

    async def test_product_description_allowed(self):
        assert validate_actions([{
            'action': 'set_description', 'entity_type': 'product',
            'entity_id': 'abc', 'text': 'Принят бесплатно, себестоимость 0 корректна',
        }]) is None

    async def test_product_delete_rejected(self):
        assert validate_actions([{
            'action': 'delete_document', 'entity_type': 'product', 'entity_id': 'abc',
        }]) is not None

    async def test_preview_roundtrip_through_fsm(self):
        preview = FixPreview(finding_id=7, summary='тест', actions=[
            {'action': 'set_applicable', 'entity_type': 'supply',
             'entity_id': 'abc', 'applicable': True},
        ])
        restored = fix_preview_from_state(fix_preview_to_state(preview))
        assert restored.finding_id == 7
        assert restored.actions == preview.actions

    async def test_preview_message_readable(self):
        preview = FixPreview(finding_id=1, summary='Проставить цену этикетки', actions=[
            {'action': 'set_position_price', 'entity_type': 'supply',
             'entity_id': 'abcdef123456', 'position_id': 'p1', 'price_kopecks': 25000},
        ])
        msg = preview.to_telegram_message()
        assert 'Проставить цену этикетки' in msg
        assert '250.00' in msg.replace(' ', '')
