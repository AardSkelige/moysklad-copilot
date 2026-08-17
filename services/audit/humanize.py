"""Перевод фактов из «языка API» в человеческий — перед отправкой в LLM.

МойСклад отдаёт все деньги в копейках, и детекторы кладут их в payload как есть
(`price_kopecks: 45`). Промптовое правило «дели на 100» модель соблюдает не
всегда: в находке по Лауроилглутамату 45 копеек превратились в «45,00 ₽/кг» —
ошибка в 100 раз, на которой строился весь вердикт.

Арифметику LLM доверять нельзя, поэтому конвертируем детерминированно: ключ
`*_kopecks` становится ключом без суффикса с готовой строкой «0,45 ₽».
"""


def format_rub(kopecks: float | int | None) -> str:
    """Копейки -> «1 850,00 ₽» (формат из SPEAK_RULES)."""
    if kopecks is None:
        return '—'
    return f'{kopecks / 100:,.2f} ₽'.replace(',', ' ').replace('.', ',')


def humanize_money(data, *, keep_raw: bool = False):
    """Копия данных, где каждый ключ `X_kopecks` заменён на `X` со строкой в рублях.

    keep_raw=True оставляет и исходный ключ: агент исправлений строит действия
    в копейках (set_position_price), ему нужно исходное число.
    """
    if isinstance(data, list):
        return [humanize_money(v, keep_raw=keep_raw) for v in data]
    if not isinstance(data, dict):
        return data

    out: dict = {}
    for key, value in data.items():
        if key.endswith('_kopecks') and isinstance(value, (int, float)):
            plain = key[: -len('_kopecks')]
            # ключ занят реальным значением — не затираем, добавляем с суффиксом
            out[plain if plain not in data else f'{plain}_rub'] = format_rub(value)
            if keep_raw:
                out[key] = value
        else:
            out[key] = humanize_money(value, keep_raw=keep_raw)
    return out
