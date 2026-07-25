"""Обёртка над DeepSeek API через OpenAI-совместимый SDK."""

import json
from typing import Optional

from openai import AsyncOpenAI

from core import config


class LLMClient:
    """Минимальный async-клиент. Возвращает обычные dict, чтобы хранить историю в FSM."""

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=config.DEEPSEEK_API_KEY,
            base_url=config.DEEPSEEK_BASE_URL,
        )

    async def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.0,
    ) -> dict:
        """Возвращает {'content': str|None, 'tool_calls': [{id, name, arguments(dict)}]}"""
        kwargs = {
            'model': config.DEEPSEEK_MODEL,
            'messages': messages,
            'temperature': temperature,
            # v4 — reasoning-модель. Отключаем thinking, чтобы поведение
            # осталось как у прежней chat-модели (без reasoning-накладных).
            'extra_body': {'thinking': {'type': 'disabled'}},
        }
        if tools:
            kwargs['tools'] = tools
            kwargs['tool_choice'] = 'auto'

        resp = await self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message

        tool_calls = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({
                'id': tc.id,
                'name': tc.function.name,
                'arguments': args,
            })

        return {'content': msg.content, 'tool_calls': tool_calls}
