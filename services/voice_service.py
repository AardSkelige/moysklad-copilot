"""Расшифровка голосовых сообщений через Groq Whisper.

Groq возвращает текст в обычном HTTP-вызове (multipart/form-data).
Используем официальный async SDK groq.
"""

import io

from groq import AsyncGroq

from core import config
from core.logger import logger


class VoiceService:
    def __init__(self):
        self.client = AsyncGroq(api_key=config.GROQ_API_KEY)

    async def transcribe(self, audio_bytes: bytes, filename: str = 'voice.ogg') -> str:
        """Принимает байты голосового файла (Telegram даёт .ogg), возвращает текст на русском."""
        if not audio_bytes:
            return ''
        result = await self.client.audio.transcriptions.create(
            file=(filename, io.BytesIO(audio_bytes)),
            model=config.GROQ_WHISPER_MODEL,
            language='ru',
            response_format='text',
        )
        # SDK возвращает либо str (при response_format='text'), либо объект с .text
        if isinstance(result, str):
            text = result
        else:
            text = getattr(result, 'text', '') or ''
        text = text.strip()
        logger.info(f"[voice] transcribed: {text[:120]!r}")
        return text
