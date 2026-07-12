# Индекс документации МойСклад API 1.2

Полная документация находится в `docs/moysklad-api-docs/md/`

## Ключевые разделы для проекта

### Платежи и финансы
- [Входящие платежи (paymentin)](./moysklad-api-docs/md/documents/_payment_in.md)
- [Исходящие платежи (paymentout)](./moysklad-api-docs/md/documents/_payment_out.md)
- [Статьи расходов (expenseitem)](./moysklad-api-docs/md/dictionaries/_expenseitem.md)
- [Предоплаты (prepayment)](./moysklad-api-docs/md/documents/_prepayment.md)
- [Возврат предоплат](./moysklad-api-docs/md/documents/_prepayment_return.md)

### Общие разделы
- [Общие сведения (_general.md)](./moysklad-api-docs/md/_general.md) - аутентификация, форматы, пагинация
- [Ошибки (_errors.md)](./moysklad-api-docs/md/_errors.md)
- [Асинхронный обмен (_async.md)](./moysklad-api-docs/md/_async.md)
- [Ограничения (_restrictions.md)](./moysklad-api-docs/md/_restrictions.md)

### Справочники
- [Организации](./moysklad-api-docs/md/dictionaries/_organization.md)
- [Контрагенты](./moysklad-api-docs/md/dictionaries/_counterparty.md)
- [Валюты](./moysklad-api-docs/md/dictionaries/_currency.md)
- [Проекты](./moysklad-api-docs/md/dictionaries/_project.md)
- [Договоры](./moysklad-api-docs/md/dictionaries/_contract.md)

### Аудит и журналы
- [Аудит (audit)](./moysklad-api-docs/md/audit/_audit.md)
- [История изменений](./moysklad-api-docs/md/changelog/_changelog.md)

## Как использовать

Просто используйте Read tool для чтения нужных файлов:
```
Read docs/moysklad-api-docs/md/documents/_payment_in.md
```

Или для поиска по всей документации используйте Grep:
```
Grep pattern="paymentin" path="docs/moysklad-api-docs/md"
```

## Обновление документации

Чтобы обновить документацию до последней версии:
```bash
cd docs/moysklad-api-docs && git pull
```
