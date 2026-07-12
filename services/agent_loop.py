"""Общий цикл LLM ↔ tools для диалоговых агентов (производство, аудит).

Агент описывает свои инструменты dispatch-таблицей {имя: async handler}.
Handler возвращает (json-строка результата, превью|None): превью прерывает цикл
и уходит пользователю на подтверждение кнопкой — мутирующие действия
никогда не выполняются внутри цикла.
"""

import json
import re
from typing import Awaitable, Callable

from core.logger import logger

# handler(args) -> (tool_result_json, preview | None)
ToolHandler = Callable[[dict], Awaitable[tuple[str, object]]]

_MD_BOLD = re.compile(r'\*\*(.+?)\*\*', re.DOTALL)
_MD_ITALIC = re.compile(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', re.DOTALL)
_MD_HEADER = re.compile(r'^#{1,6}\s+', re.MULTILINE)
_MD_INLINE_CODE = re.compile(r'`([^`]+)`')


def clean_markdown(text: str) -> str:
    """Снять Markdown-форматирование если LLM проигнорировала промпт."""
    if not text:
        return text
    text = _MD_BOLD.sub(r'\1', text)
    text = _MD_ITALIC.sub(r'\1', text)
    text = _MD_HEADER.sub('', text)
    text = _MD_INLINE_CODE.sub(r'\1', text)
    return text


async def run_agent_step(
    llm,
    history: list[dict],
    user_message: str,
    tools_schema: list[dict],
    dispatch: dict[str, ToolHandler],
    max_iterations: int = 8,
) -> dict:
    """Один шаг диалога.

    Возвращает один из:
    - {'kind': 'reply', 'text': str, 'history': list}
    - {'kind': 'preview', 'preview': object, 'history': list}
    - {'kind': 'error', 'text': str, 'history': list}
    """
    history = list(history)
    history.append({'role': 'user', 'content': user_message})

    for _ in range(max_iterations):
        resp = await llm.chat(history, tools=tools_schema)

        if resp['tool_calls']:
            history.append({
                'role': 'assistant',
                'content': resp['content'] or '',
                'tool_calls': [
                    {
                        'id': tc['id'],
                        'type': 'function',
                        'function': {'name': tc['name'],
                                     'arguments': json.dumps(tc['arguments'], ensure_ascii=False)},
                    }
                    for tc in resp['tool_calls']
                ],
            })

            preview_to_return = None
            for tc in resp['tool_calls']:
                name, args = tc['name'], tc['arguments']
                logger.info(f'[agent] tool={name} args={args}')
                handler = dispatch.get(name)
                try:
                    if handler is None:
                        tool_result = json.dumps({'error': f'Unknown tool: {name}'}, ensure_ascii=False)
                    else:
                        tool_result, preview = await handler(args)
                        if preview is not None:
                            preview_to_return = preview
                except Exception as e:
                    logger.exception(f'[agent] tool {name} failed')
                    tool_result = json.dumps({'error': str(e)}, ensure_ascii=False)

                history.append({
                    'role': 'tool',
                    'tool_call_id': tc['id'],
                    'content': tool_result,
                })

            if preview_to_return is not None:
                return {'kind': 'preview', 'preview': preview_to_return, 'history': history}
            continue

        text = (resp['content'] or '').strip()
        history.append({'role': 'assistant', 'content': text})
        if not text:
            return {'kind': 'error', 'text': 'LLM вернула пустой ответ.', 'history': history}
        return {'kind': 'reply', 'text': clean_markdown(text), 'history': history}

    return {'kind': 'error', 'text': 'Слишком много итераций. Попробуй заново.', 'history': history}
