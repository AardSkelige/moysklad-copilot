from aiogram.fsm.state import State, StatesGroup


class AddOperationState(StatesGroup):
    waiting_type = State()
    waiting_subtype = State()
    waiting_category = State()
    waiting_amount = State()
    waiting_comment = State()
    waiting_confirm = State()


class EditOperationState(StatesGroup):
    waiting_field = State()
    waiting_category = State()
    waiting_amount = State()
    waiting_comment = State()


class AddCategoryState(StatesGroup):
    waiting_name = State()


class RenameCategoryState(StatesGroup):
    waiting_select = State()    # выбор категории из списка
    waiting_new_name = State()  # ввод нового имени


class EditMSOperationState(StatesGroup):
    waiting_field = State()
    waiting_category = State()
    waiting_amount = State()
    waiting_comment = State()


class ProductionState(StatesGroup):
    waiting_input = State()      # ждём текст или голос с командой
    confirming = State()         # показали превью ПЗ, ждём [Создать] / [Отмена]


class AuditState(StatesGroup):
    discussing = State()          # диалог с fix-агентом по конкретной находке
    confirming_fix = State()      # показали превью исправления, ждём [Применить] / [Отмена]
    reviewing_comments = State()  # ревью комментариев: карточки [Заменить] / [Оставить]
