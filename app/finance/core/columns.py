"""Колонки таблиц компании «строка = договор за месяц» - ключи ядра.

Сводная таблица, вкладки таблиц юрлиц и шаблон счетов - таблицы одной формы:
строка - договор за месяц. Ядро знает смыслы их колонок (ключ: наше юрлицо,
заказчик, № договора, сумма АВР, ЭСФ отправлен…), подпись общими словами и
общие написания отрасли (`packs`). Как колонки названы у конкретной компании -
её словарь (`core/settings.book_columns`): написания на ключ ядра, первое -
как колонку зовёт сама компания. Модуль без зависимостей от читателей: его
берут и ядро (`core/book.py`), и «Оплачено (сводка)», «Счета», таблицы юрлиц,
шаблон счетов.

Позиций колонок у ядра нет: колонка находится по названию, а одинаковых
названий в таких таблицах не бывает - спорное совпадение остаётся отказом
(`app.finance.layout`).
"""
from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from app.finance import packs
from app.finance.layout import Column


def _column(key: str, title: str, *, required: bool = False, strict: bool = True) -> Column:
    """Колонка ядра: ключ, подпись общими словами, общие написания отрасли."""
    return Column(key, title, tuple(packs.industry().month_names.get(key, ())), required=required, strict=strict)


#: Колонки таблицы «строка = договор за месяц» - ключи ядра. Подпись и
#: написания - общими словами отрасли; шапка таблицы конкретной компании -
#: её словарь (`core/settings.book_columns`, первое написание - как колонку
#: зовёт сама компания). Денежные и датные - только по точному названию:
#: «Сумма АВР», «Сумма оплаты 1» и «Сумма договора» начинаются одинаково, и
#: «похожее» прочитало бы соседнюю.
COLUMNS: tuple[Column, ...] = (
    _column("month_no", "Номер месяца"),
    _column("month", "Месяц строки"),
    _column("own", "Наше юрлицо", required=True, strict=False),
    _column("kind", "Вид услуги"),
    _column("staff", "Наш сотрудник"),
    _column("customer", "Заказчик", required=True, strict=False),
    _column("bank_name", "Имя клиента в банке"),
    _column("onec_name", "Имя клиента в 1С"),
    _column("bill_day", "Число выставления счёта"),
    _column("amount", "Сумма договора"),
    _column("department", "Отдел"),
    _column("subject", "Предмет договора"),
    _column("signed_at", "Дата заключения договора"),
    _column("number", "№ договора", required=True, strict=False),
    _column("amendments", "Доп. соглашения"),
    _column("period_from", "Период с"),
    _column("period_to", "Период по"),
    _column("note", "Комментарий к договору"),
    _column("opening_acts", "Сальдо на начало по АВР"),
    _column("bill_state", "Состояние счёта"),
    _column("bill_period", "Счёт за период"),
    _column("bill_no", "№ счёта"),
    _column("bill_date", "Дата счёта"),
    _column("paid_state", "Отметка об оплате"),
    _column("paid", "Поступило за месяц"),
    _column("pay1_date", "Дата оплаты 1"),
    _column("pay1_sum", "Сумма оплаты 1"),
    _column("pay2_date", "Дата оплаты 2"),
    _column("pay2_sum", "Сумма оплаты 2"),
    _column("pay3_date", "Дата оплаты 3"),
    _column("pay3_sum", "Сумма оплаты 3"),
    _column("act_state", "АВР выписан"),
    _column("act_sum", "Сумма АВР"),
    _column("act_no", "№ АВР"),
    _column("act_date", "Дата АВР"),
    _column("accepted_state", "АВР принят клиентом"),
    _column("accepted_date", "Дата принятия АВР"),
    _column("esf_state", "ЭСФ отправлен"),
    _column("esf_date", "Дата ЭСФ"),
    _column("comment", "Комментарий"),
    _column("closing_acts", "Сальдо на конец по АВР"),
    _column("status", "Статус"),
    _column("opening_accruals", "Сальдо на начало по начислениям"),
    _column("closing_accruals", "Сальдо на конец по начислениям"),
    _column("refund_sum", "Сумма возврата"),
    _column("refund_date", "Дата возврата"),
)

BY_KEY = {item.key: item for item in COLUMNS}


def column(key: str, *, core: str | None = None, required: bool = False, strict: bool = True) -> Column:
    """Колонка читателя: подпись и общие написания - у колонки ядра `core` (по умолчанию тот же ключ)."""
    base = BY_KEY[core or key]
    return Column(key, base.title, base.names, required=required, strict=strict)


def signature(extra: dict[str, list[str]] | None) -> str:
    """Подпись словаря колонок: другая - читать таблицу заново."""
    return "|".join(f"{key}={','.join(sorted(names))}" for key, names in sorted((extra or {}).items()))


def with_company_names(
    columns: Sequence[Column], extra: dict[str, list[str]] | None, keys: dict[str, str] | None = None
) -> tuple[Column, ...]:
    """Колонки любого читателя таблиц компании с её написаниями (словарь `core/settings.book_columns`).

    Словарь один на компанию и ведётся ключами ядра; `keys` - ключ читателя →
    ключ ядра, где они разные (у «Счетов» «bill» - это «Счет», в ядре `bill_state`).
    Так своё название колонки, записанное раз, понимают ядро, «Счета», сводка
    оплат, таблицы юрлиц и шаблон счетов. Первое написание компании - и
    подпись колонки: так её зовут в отказах и в шапке шаблона.
    """
    if not extra:
        return tuple(columns)
    keys = keys or {}
    out = []
    for column in columns:
        own = [name for name in extra.get(keys.get(column.key, column.key), ()) if str(name).strip()]
        if not own:
            out.append(column)
            continue
        names = column.names + tuple(name for name in own if name not in column.names)
        out.append(replace(column, title=" ".join(own[0].split()), names=names))
    return tuple(out)


__all__ = ["BY_KEY", "COLUMNS", "column", "signature", "with_company_names"]
