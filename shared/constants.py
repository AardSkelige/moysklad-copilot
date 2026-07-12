class CallbackData:
    # Главное меню
    MAIN_MENU = 'main_menu'
    FINANCE_MENU = 'finance_menu'
    FINANCE_ADD = 'finance_add'
    FINANCE_LIST = 'finance_list'
    FINANCE_CATEGORIES = 'finance_categories'
    FINANCE_EXPORT = 'finance_export'

    # Тип операции
    TYPE_INCOME = 'type_income'
    TYPE_EXPENSE = 'type_expense'

    # Подтип документа
    SUBTYPE_PAYMENT_IN = 'subtype_paymentin'
    SUBTYPE_CASH_IN = 'subtype_cashin'
    SUBTYPE_PAYMENT_OUT = 'subtype_paymentout'
    SUBTYPE_CASH_OUT = 'subtype_cashout'

    # Подтверждение операции
    CONFIRM_SAVE = 'confirm_save'
    CONFIRM_CANCEL = 'confirm_cancel'

    # Подтверждение удаления
    DELETE_CANCEL = 'delete_cancel'

    # Категории — главное меню управления
    CAT_ADD = 'cat_add'
    CAT_RENAME_LIST = 'cat_rename_list'
    CAT_SYNC = 'cat_sync'
    CAT_DELETE_INFO = 'cat_delete_info'
    CAT_BACK = 'cat_back'

    # Пропустить комментарий
    SKIP_COMMENT = 'skip_comment'

    # MoySklad-First операции
    MS_DELETE_CANCEL = 'ms_del_cancel'
    MS_EDIT_CANCEL = 'ms_edit_cancel'
    MS_EDIT_AMOUNT = 'ms_edit_amount'
    MS_EDIT_COMMENT = 'ms_edit_comment'
    MS_EDIT_CATEGORY = 'ms_edit_category'
    MS_BACK_LIST = 'ms_back_list'

    # Производственный агент
    PRODUCTION_ENTRY = 'prod_entry'
    PRODUCTION_CONFIRM = 'prod_confirm'
    PRODUCTION_CANCEL = 'prod_cancel'

    # Аудит: диалог исправления
    AUDIT_FIX_APPLY = 'aud_fix_apply'
    AUDIT_FIX_CANCEL = 'aud_fix_cancel'
    AUDIT_EXIT_DIALOG = 'aud_exit'

    # Аудит: меню
    AUDIT_MENU = 'aud_menu'
    AUDIT_RUN = 'aud_run'
    AUDIT_LIST = 'aud_list'
    AUDIT_STATUS = 'aud_status'

    # Аудит: ревью комментариев
    AUDIT_COMMENTS = 'aud_cmt'
    AUDIT_COMMENTS_30D = 'aud_cmt_30'
    AUDIT_COMMENTS_ALL = 'aud_cmt_all'
    AUDIT_COMMENTS_FIN = 'aud_cmt_fin'
    AUDIT_COMMENT_APPLY = 'aud_cmt_ok'
    AUDIT_COMMENT_SKIP = 'aud_cmt_skip'
    AUDIT_COMMENT_STOP = 'aud_cmt_stop'


class CallbackPrefix:
    # Аудит учёта
    AUDIT_ACK = 'aud_ack:'
    AUDIT_IGNORE = 'aud_ign:'
    AUDIT_MUTE = 'aud_mute:'
    AUDIT_PAGE = 'aud_page:'
    AUDIT_TALK = 'aud_talk:'
    AUDIT_SECTION = 'aud_sec:'   # aud_sec:<индекс категории>:<offset>
    AUDIT_COMMENTS_GO = 'aud_cmt_go:'   # aud_cmt_go:<doc|fin>:<дней|0=вся история>

    SELECT_CATEGORY = 'cat_sel:'
    LIST_PAGE = 'list_page:'
    DELETE_CONFIRM = 'delete_confirm:'
    RENAME_CATEGORY = 'rename_cat:'
    MS_VIEW = 'ms_view:'
    MS_EDIT = 'ms_edit:'
    MS_DELETE = 'ms_del:'
    MS_DELETE_CONFIRM = 'ms_del_ok:'
    MS_EDIT_CAT = 'ms_edit_cat:'


class ButtonLabel:
    FINANCE = '💰 Финансы'
    ADD_OPERATION = '➕ Добавить операцию'
    VIEW_OPERATIONS = '📋 Последние операции'
    CATEGORIES = '📁 Управление категориями'
    EXPORT = '📥 Экспорт Excel'
    PRODUCTION = '🏭 Производство'
    AUDIT = '🔍 Аудит учёта'
    AUDIT_RUN = '▶️ Запустить проверку'
    AUDIT_LIST = '📋 Неразобранные находки'
    AUDIT_STATUS = '📊 Статус прогонов'
    AUDIT_COMMENTS = '💬 Ревью комментариев'

    INCOME = '➕ Доход'
    EXPENSE = '➖ Расход'

    PAYMENT_IN = '🏦 Входящий платёж'
    CASH_IN = '💵 Приходный ордер'
    PAYMENT_OUT = '🏦 Исходящий платёж'
    CASH_OUT = '💵 Расходный ордер'

    SAVE = '✅ Сохранить'
    CANCEL = '❌ Отмена'
    SKIP = '⏭ Без комментария'

    EDIT = '✏️ Редактировать'
    DELETE = '🗑 Удалить'

    PREV = '← Назад'
    NEXT = 'Вперёд →'

    ADD_CATEGORY = '➕ Добавить категорию расходов'
    RENAME_CATEGORY = '✏️ Переименовать категорию расходов'
    SYNC_CATEGORIES = '🔄 Синхронизировать с МойСклад'
    DELETE_CATEGORY_INFO = 'ℹ️ Как удалить категорию'
    BACK = '◀️ Назад'
