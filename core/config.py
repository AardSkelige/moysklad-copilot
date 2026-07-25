import os
from dotenv import load_dotenv
from core.logger import logger

load_dotenv()

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

# Поддержка нескольких администраторов через запятую
admin_ids_str = os.getenv('FINANCE_ADMIN_TELEGRAM_ID', '')
FINANCE_ADMIN_IDS = [int(id.strip()) for id in admin_ids_str.split(',') if id.strip()]
# Для обратной совместимости
FINANCE_ADMIN_ID = FINANCE_ADMIN_IDS[0] if FINANCE_ADMIN_IDS else 0

DATABASE_URL = 'sqlite+aiosqlite:///data/finance.db'

MOYSKLAD_TOKEN = os.getenv('MOYSKLAD_TOKEN', '')
MOYSKLAD_ORGANIZATION_HREF = os.getenv('MOYSKLAD_ORGANIZATION_HREF', '')
MOYSKLAD_AGENT_HREF = os.getenv('MOYSKLAD_AGENT_HREF', '')
MOYSKLAD_BASE_URL = 'https://api.moysklad.ru/api/remap/1.2'

# === AI агент (производство) ===
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '')
DEEPSEEK_MODEL = 'deepseek-v4-pro'
DEEPSEEK_BASE_URL = 'https://api.deepseek.com'

GROQ_API_KEY = os.getenv('GROQ_API_KEY', '')
GROQ_WHISPER_MODEL = 'whisper-large-v3'

# Имя-префикс для комментариев операций, создаваемых через финансовый модуль
FINANCE_COMMENT_AUTHOR = os.getenv('FINANCE_COMMENT_AUTHOR', '')

# PRODUCTION_USER_IDS формат: "123456789:Аня,987654321:Катя"
# Парсится в {123456789: "Аня", 987654321: "Катя"}
PRODUCTION_USERS: dict[int, str] = {}
for pair in os.getenv('PRODUCTION_USER_IDS', '').split(','):
    pair = pair.strip()
    if not pair or ':' not in pair:
        continue
    uid, name = pair.split(':', 1)
    try:
        PRODUCTION_USERS[int(uid.strip())] = name.strip()
    except ValueError:
        continue

# Идентификаторы организации / складов / состояний МС (для создания ПЗ) — из .env
MOYSKLAD_ORG_ID = os.getenv('MOYSKLAD_ORG_ID', '')
MOYSKLAD_STORE_MATERIALS_ID = os.getenv('MOYSKLAD_STORE_MATERIALS_ID', '')   # Производство
MOYSKLAD_STORE_PRODUCTS_ID = os.getenv('MOYSKLAD_STORE_PRODUCTS_ID', '')     # Готовая продукция
MOYSKLAD_PRODUCTION_TASK_STATE_PODGOTOVKA = os.getenv(
    'MOYSKLAD_PRODUCTION_TASK_STATE_PODGOTOVKA', '')

AGENT_ENABLED = bool(DEEPSEEK_API_KEY and GROQ_API_KEY and PRODUCTION_USERS)

# === Аудит учёта МойСклад ===
AUDIT_ENABLED = os.getenv('AUDIT_ENABLED', 'true').lower() in ('1', 'true', 'yes') \
    and bool(MOYSKLAD_TOKEN) and bool(FINANCE_ADMIN_IDS)
AUDIT_OWNER_TELEGRAM_ID = int(os.getenv('AUDIT_OWNER_TELEGRAM_ID', '0')) or FINANCE_ADMIN_ID
AUDIT_INCREMENTAL_MINUTES = int(os.getenv('AUDIT_INCREMENTAL_MINUTES', '90'))
AUDIT_FULL_HOUR = int(os.getenv('AUDIT_FULL_HOUR', '6'))
AUDIT_SCAN_MONTHS = int(os.getenv('AUDIT_SCAN_MONTHS', '3'))
AUDIT_RETRO_EDIT_HOURS = int(os.getenv('AUDIT_RETRO_EDIT_HOURS', '24'))
# Свежим отгрузкам дают сутки: накладные расходы и комментарий часто вносят на следующий день
AUDIT_DEMAND_GRACE_HOURS = int(os.getenv('AUDIT_DEMAND_GRACE_HOURS', '24'))
AUDIT_STALE_DRAFT_DAYS = int(os.getenv('AUDIT_STALE_DRAFT_DAYS', '7'))
AUDIT_STALE_ORDER_DAYS = int(os.getenv('AUDIT_STALE_ORDER_DAYS', '14'))
AUDIT_PRODUCTION_STUCK_DAYS = int(os.getenv('AUDIT_PRODUCTION_STUCK_DAYS', '14'))
AUDIT_MAX_MESSAGES_PER_RUN = int(os.getenv('AUDIT_MAX_MESSAGES_PER_RUN', '10'))
AUDIT_COMMENT_REVIEW_DAYS = int(os.getenv('AUDIT_COMMENT_REVIEW_DAYS', '30'))


def validate_config():
    errors = []

    if not BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN не установлен в .env")

    if not FINANCE_ADMIN_IDS:
        errors.append("FINANCE_ADMIN_TELEGRAM_ID не установлен в .env")

    if errors:
        raise ValueError(
            "\n\nОшибка конфигурации:\n" +
            "\n".join(f"  - {e}" for e in errors) +
            "\n\nПроверьте файл .env и заполните все обязательные поля.\n"
        )

    logger.info(f"Финансовые админы ({len(FINANCE_ADMIN_IDS)}): {FINANCE_ADMIN_IDS}")
    if PRODUCTION_USERS:
        logger.info(f"Пользователи агента производства ({len(PRODUCTION_USERS)}): {PRODUCTION_USERS}")
    if AGENT_ENABLED:
        logger.info("Агент производства включён (DeepSeek + Groq Whisper)")
    else:
        logger.info("Агент производства отключён (не заполнены DEEPSEEK_API_KEY/GROQ_API_KEY/PRODUCTION_USER_IDS)")
    logger.info("МойСклад интеграция включена")
    if AUDIT_ENABLED:
        logger.info(
            f"Аудит учёта включён: владелец {AUDIT_OWNER_TELEGRAM_ID}, "
            f"инкрементально каждые {AUDIT_INCREMENTAL_MINUTES} мин, полный скан в {AUDIT_FULL_HOUR}:00"
        )
    else:
        logger.info("Аудит учёта отключён (AUDIT_ENABLED=false или нет MOYSKLAD_TOKEN)")


validate_config()
