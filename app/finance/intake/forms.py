"""Шесть форм строки: что значит одна строка чужой таблицы.

Любая таблица учёта, которую компания приносит, - это строки одного из шести
смыслов. Форма говорит, какие колонки у такой строки бывают и без каких она
не форма. Каталоги здесь служат распознаванию (`recognize`): какой из шести
смыслов у листа. Читает строки дальше разбор формы со своим правилом привязки
колонок - реестр, выписка, сводная таблица.

Написания колонок - открытый список. Новое написание - строка здесь (или в
словаре компании), а не правка разбора.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from app.finance.layout import Column, norm

PARTICIPANT = "participant"
CONTRACT = "contract"
CONTRACT_MONTH = "contract_month"
PAYMENT = "payment"
DOCUMENT = "document"
BALANCE = "balance"


@dataclass(frozen=True)
class Field:
    """Колонка формы: ключ, подпись и написания шапки. `money` - деньги: спор за неё - отказ."""

    key: str
    title: str
    names: tuple[str, ...]
    money: bool = False


@dataclass(frozen=True)
class Form:
    key: str
    #: Как форма называется человеку: «строка = договор за месяц».
    title: str
    #: Где такая строка встречается - подсказка в вопросе «что значит строка».
    examples: str
    fields: tuple[Field, ...]
    #: Без чего это не форма: в каждой группе узнана хотя бы одна колонка.
    needs: tuple[tuple[str, ...], ...]
    #: Слова в имени файла, листа или в названии над шапкой, которыми таблица
    #: сама говорит, что она такое: «Оборотно-сальдовая ведомость», «Выписка».
    words: tuple[str, ...] = ()
    #: Слова, которыми таблица говорит, что она НЕ эта форма.
    not_words: tuple[str, ...] = ()

    def field(self, key: str) -> Field | None:
        return next((item for item in self.fields if item.key == key), None)


def _f(key: str, title: str, *names: str, money: bool = False) -> Field:
    return Field(key, title, tuple(dict.fromkeys(norm(name) for name in (title, *names) if name)), money)


def from_columns(columns: Iterable[Column], money: Iterable[str] = ()) -> tuple[Field, ...]:
    money = set(money)
    return tuple(_f(column.key, column.title, *column.names, money=column.key in money) for column in columns)


# ── Участник ─────────────────────────────────────────────────────────────────

PARTICIPANT_FIELDS: tuple[Field, ...] = (
    _f("name", "Наименование", "наименование контрагента", "контрагент", "организация", "клиент", "фио", "ф.и.о."),
    _f("full_name", "Полное наименование", "полное наименование контрагента", "полное название"),
    _f("bin", "ИИН/БИН", "бин/иин", "бин", "иин", "бин контрагента", "иин контрагента", "рнн/бин"),
    _f("kind", "Вид контрагента", "юр./физ. лицо", "юридическое/физическое лицо", "тип контрагента"),
    _f("address", "Адрес", "юридический адрес", "фактический адрес"),
    _f("phone", "Телефон", "телефоны", "контактный телефон"),
    _f("email", "Эл. почта", "email", "e-mail", "почта"),
    _f("code", "Код", "код контрагента"),
)

# ── Договор ──────────────────────────────────────────────────────────────────
#
# Каталог распознавания - системные поля реестра (`contracts/fields.py`) с
# написаниями. Разбор реестра берёт поля компании, со своими названиями.


def contract_fields(extra: Sequence[Any] = ()) -> tuple[Field, ...]:
    """Поля реестра как колонки формы «договор»; `extra` - поля компании (`key`, `title`, `names`)."""
    from app.finance.contracts.fields import COMPUTED_FIELDS, SYSTEM_FIELDS

    out: dict[str, Field] = {}
    for item in (*SYSTEM_FIELDS, *extra):
        key = getattr(item, "key")
        if key in COMPUTED_FIELDS:
            continue
        names = tuple(getattr(item, "names", ()) or ())
        known = out.get(key)
        merged = (*(known.names if known else ()), *names)
        out[key] = _f(key, getattr(item, "title"), *merged, money=getattr(item, "type", "") == "money")
    out["party_bin"] = _f("party_bin", "БИН/ИИН стороны", "бин/иин", "иин/бин", "бин", "иин", "бин клиента", "иин клиента")
    return tuple(out.values())


# ── Договор за месяц ─────────────────────────────────────────────────────────


def contract_month_fields() -> tuple[Field, ...]:
    """Колонки сводной таблицы (`core/book.COLUMNS`) и общие написания помесячных таблиц."""
    from app.finance.core.book import COLUMNS

    money = {
        "amount", "opening_acts", "paid", "pay1_sum", "pay2_sum", "pay3_sum", "act_sum", "closing_acts",
        "opening_accruals", "closing_accruals", "refund_sum",
    }
    base = {item.key: item for item in from_columns(COLUMNS, money)}
    generic = (
        _f("customer", "Заказчик", "клиент", "арендатор", "контрагент", "покупатель"),
        _f("number", "№ Договора", "договор", "договор №", "номер договора"),
        _f("own", "Наше юрлицо", "наша фирма", "наше тоо", "исполнитель", "арендодатель"),
        _f("accrued", "Начислено", "к оплате", "итого к оплате", "сумма к оплате", "начисление", "сумма начисления", money=True),
        _f("fixed", "Аренда", "абонентская плата", "сумма в месяц", money=True),
        _f("utilities", "Коммунальные", "коммунальные услуги", "возмещение коммунальных", money=True),
        _f("paid", "Оплачено", "оплата", "сумма оплаты", "поступило", "сумма факт поступ.", money=True),
        _f("debt", "Долг", "задолженность", "остаток долга", money=True),
        _f("month", "Месяц", "период", "месяц начисления", "техн. 1"),
        _f("note", "Примечание", "комментарий", "доп. коммент."),
        # Колонка денег без слов, чьи они: «Сумма». Одна - сумма месяца; две
        # одинаковые - отказ читать: какая из них аренда, а какая коммунальные,
        # по названию не понять (полигон, Ф5).
        _f("sum", "Сумма", money=True),
    )
    for item in generic:
        # Подпись - общими словами, а не шапкой сводной BBC («Наша Фирма»,
        # «Сумма Факт Поступ.»): её видит любая компания (R14).
        known = base.get(item.key)
        names = (*(known.names if known else ()), *item.names)
        base[item.key] = Field(item.key, item.title, tuple(dict.fromkeys(names)), item.money or bool(known and known.money))
    return tuple(base.values())


# ── Платёж ───────────────────────────────────────────────────────────────────


def payment_fields() -> tuple[Field, ...]:
    from app.finance.importing import COLUMNS

    return from_columns(COLUMNS, ("amount", "amount_income", "amount_expense"))


# ── Документ ─────────────────────────────────────────────────────────────────

DOCUMENT_FIELDS: tuple[Field, ...] = (
    _f("date", "Дата", "дата документа"),
    _f("number", "Номер", "№", "номер документа", "№ документа"),
    _f("kind", "Вид документа", "тип документа", "вид операции"),
    _f("organization", "Организация", "наша организация"),
    _f("counterparty", "Контрагент", "покупатель", "получатель"),
    _f("contract", "Договор", "договор контрагента"),
    _f("amount", "Сумма", "сумма документа", "сумма с ндс", money=True),
    _f("vat", "Сумма НДС", "ндс", money=True),
    _f("currency", "Валюта", "валюта документа"),
    _f("content", "Содержание", "комментарий", "назначение"),
    _f("signed", "Подписан", "подписан покупателем", "акт подписан"),
    _f("esf", "ЭСФ", "статус эсф", "номер эсф"),
    _f("basis", "Основание", "документ-основание"),
)

# ── Остаток на дату ──────────────────────────────────────────────────────────
#
# Оборотно-сальдовая ведомость 1С: шапка в две строки («Сальдо на начало
# периода» над «Дебет | Кредит»), строки - счёт, контрагенты, договоры
# уровнями отступа. И «Сальдо Начало» / «Долг на 01.06» в таблицах.

BALANCE_FIELDS: tuple[Field, ...] = (
    _f("account", "Счет", "счёт", "субконто", "контрагенты", "контрагент", "договоры"),
    _f("opening_debit", "Сальдо на начало периода Дебет", "сальдо на начало дебет", "начальное сальдо дебет",
       "сальдо на начало периода дт", money=True),
    _f("opening_credit", "Сальдо на начало периода Кредит", "сальдо на начало кредит", "начальное сальдо кредит",
       "сальдо на начало периода кт", money=True),
    _f("turnover_debit", "Обороты за период Дебет", "оборот дебет", "обороты дебет", "обороты за период дт", money=True),
    _f("turnover_credit", "Обороты за период Кредит", "оборот кредит", "обороты кредит", "обороты за период кт", money=True),
    _f("closing_debit", "Сальдо на конец периода Дебет", "сальдо на конец дебет", "конечное сальдо дебет",
       "сальдо на конец периода дт", money=True),
    _f("closing_credit", "Сальдо на конец периода Кредит", "сальдо на конец кредит", "конечное сальдо кредит",
       "сальдо на конец периода кт", money=True),
    _f("opening", "Сальдо на начало", "входящее сальдо", "сальдо начало", "долг на начало", money=True),
    _f("closing", "Сальдо на конец", "исходящее сальдо", "сальдо конец", "долг на конец", money=True),
)

MONTH_WORDS = (
    "январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
    "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def forms(contract_extra: Sequence[Any] = ()) -> tuple[Form, ...]:
    """Шесть форм с каталогами; `contract_extra` - поля реестра компании."""
    return (
        Form(
            PARTICIPANT, "участник", "список клиентов, контрагенты 1С",
            PARTICIPANT_FIELDS, needs=(("name",), ("bin", "full_name", "kind", "address", "phone", "email")),
            words=("контрагент", "клиенты", "справочник", "покупатели", "поставщики"),
        ),
        Form(
            CONTRACT, "договор", "реестр договоров, договоры 1С, реестр офиса",
            contract_fields(contract_extra),
            needs=(("number", "customer", "executor"), ("signed_at", "amount", "status", "subject", "type", "people", "end_date", "department")),
            words=("реестр", "договоры", "договор"),
        ),
        Form(
            CONTRACT_MONTH, "договор за месяц", "сводная таблица, помесячные листы юрлиц, начисления аренды",
            contract_month_fields(),
            needs=(("customer", "number"), ("accrued", "fixed", "paid", "debt", "month", "month_no", "period_from", "bill_state", "act_state")),
            words=("начислени", "сводка", "за месяц", *MONTH_WORDS),
        ),
        Form(
            PAYMENT, "платёж", "выписка банка, журнал денег, поступления 1С",
            payment_fields(), needs=(("paid_at",), ("amount", "amount_income", "amount_expense")),
            words=("выписка", "журнал", "платеж", "платёж", "поступлени", "движени", "банк", "kaspi", "halyk"),
            not_words=("реализаци", "счета на оплату", "счет на оплату", "оборотно-сальдов"),
        ),
        Form(
            DOCUMENT, "документ", "счета, АВР, ЭСФ из 1С и таблиц",
            DOCUMENT_FIELDS, needs=(("number",), ("organization", "contract", "kind", "signed", "esf", "basis")),
            words=("реализаци", "счета на оплату", "счет на оплату", "акт", "авр", "счет-фактур", "счёт-фактур", "эсф", "документ"),
        ),
        Form(
            BALANCE, "остаток на дату", "ОСВ по 1210, «Сальдо Начало» таблицы, долги на дату",
            BALANCE_FIELDS,
            needs=(("opening_debit", "opening_credit", "closing_debit", "closing_credit", "opening", "closing"),),
            words=("оборотно-сальдов", "осв", "сальдо", "долги на"),
        ),
    )


__all__ = [
    "BALANCE", "CONTRACT", "CONTRACT_MONTH", "DOCUMENT", "Field", "Form", "PARTICIPANT", "PAYMENT",
    "contract_fields", "contract_month_fields", "forms", "from_columns", "payment_fields",
]
