"""Чтение файла учёта: выписки, книги, выгрузки - во что угодно наоборот.

Зачем этот файл написан заново, а не взят у соседей по рынку
────────────────────────────────────────────────────────────
17 сентября 2026 мы прогнали импорт Finmap двадцатью файлами и записали, что
именно он делает с человеческой неточностью (полный протокол - в
`docs/finmap-audit.md`). Четыре вывода оттуда определили устройство этого
модуля:

1. **Одна плохая строка отменяла весь файл.** Двести строк, испорчена 137-я -
   не завелось ничего, сообщение «В строке 138». Здесь наоборот: хорошие строки
   заводятся, плохие остаются в партии импорта с объяснением и правятся по
   одной. Отказ от импорта целиком - это решение человека, а не парсера.

2. **Лишняя колонка ломала разбор технической ошибкой** («Cannot read
   properties of undefined») без номера строки. Здесь колонки ищутся по
   названиям (тот же механизм, что в `app.finance.layout`), а незнакомая колонка
   не мешает: она попадает в `raw` и показывается как «не использована».

3. **Минус у суммы терялся молча.** «−15 000» приезжало доходом 15 000.
   Здесь знак - это вид операции, и он разбирается явно: минус, скобки,
   «Дт/Кт», две колонки «Приход»/«Расход».

4. **Порядок частей даты угадывался в каждой строке отдельно.** «12/25/2026»
   читалось как 25 декабря, а «08/03/2026» - как 8 марта, в одном и том же
   файле. Половина американской выгрузки уезжает на другой месяц, и никакой
   ошибки при этом нет. Здесь порядок решается **один раз на файл** по всем
   датам сразу, а если однозначного ответа нет - парсер не угадывает, а
   спрашивает (`Preview.question`).

5. **Выписку из банка он читает только в своём формате.** Настоящая выписка
   приходит PDF-ом, и её надо разбирать, а не просить человека переложить
   двести строк в шаблон. Здесь PDF уходит в разбор выписок продукта
   (`app.finance.statements` → `app.services.document_service`), тот же, что
   работает в разделе «Анализ выписок». Выписка таблицей - Excel любого
   года, HTML под видом .xls, выгрузка 1С - читается этим же модулем: формат
   решает содержимое файла (`formats`), реквизиты над таблицей - счёт,
   период, остатки, владелец - читаются как колонки, по подписям, а не по
   месту (`read_requisites`). Шаблона «под банк» нет намеренно: выписка
   нового банка должна читаться в день, когда её принесли.

Правило, из которого всё выведено: **не угадывать там, где ошибка тихая.**
Громкий отказ дороже одной минуты человека. Тихо неверная цифра стоит доверия
ко всему отчёту, и найти её нельзя - она выглядит как цифра.
"""
from __future__ import annotations

import calendar
import hashlib
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Sequence

from app.finance.layout import Column, norm, squash
from app.finance import banks, formats
from app.finance.statements import StatementError, read_statement
from app.finance.intake import header as intake_header
from app.finance.intake import rows as intake_rows
from app.finance.intake import tables as intake_tables
from app.finance.intake.values import (  # noqa: F401 - имена остаются и здесь
    _DATE_PARTS,
    _MONEY_JUNK,
    _MONTH_NAMES,
    _SPACES,
    _WEEKDAY,
    DateReading,
    Money,
    _date_parts,
    decide_date_order,
    parse_date,
    parse_money,
)

log = logging.getLogger(__name__)


class ImportError_(RuntimeError):
    """Файл не читается целиком: не тот формат, нет шапки, пустой."""


# ── Колонки, которые мы понимаем ─────────────────────────────────────────────
#
# `names` перечисляет написания, встречающиеся в выписках казахстанских банков,
# в выгрузках 1С и в книгах, которые ведут руками. Список открытый: новое
# написание - это строка здесь, а не правка парсера.
#
# `hint` не ставится намеренно. У книги, которую ведут руками, «привычной
# позиции» не существует: файл каждый раз другой. Подсказка нужна там, где
# читают один и тот же лист годами (см. `app.bbc`), здесь она врала бы.

COLUMNS: tuple[Column, ...] = (
    Column(
        key="paid_at",
        title="Дата платежа",
        names=(
            "дата платежа", "дата", "дата операции", "дата проводки", "дата документа",
            "дата транзакции", "дата и время", "дата/время", "date", "payment date",
            "дата списания", "дата поступления денег", "дата зачисления",
        ),
        required=False,
    ),
    Column(
        key="accrued_at",
        title="Дата начисления",
        names=("дата начисления", "дата сделки", "дата акта", "дата реализации", "accrual date"),
        required=False,
    ),
    Column(
        key="period_start",
        title="Период начисления (с)",
        names=('период начисления (дата "с")', "период начисления с", "период с", "начало периода"),
        required=False,
    ),
    Column(
        key="period_end",
        title="Период начисления (по)",
        names=('период начисления (дата "по")', "период начисления по", "период по", "конец периода"),
        required=False,
    ),
    Column(
        key="amount",
        title="Сумма",
        names=(
            "сумма", "сумма в валюте счета", "сумма операции", "сумма платежа", "amount",
            "сумма в валюте счёта", "сумма, тенге", "сумма (kzt)",
        ),
        required=False,
    ),
    # «Дебет» - расход, «Кредит» - приход: так пишет банк, глядя на счёт
    # клиента. Написания оборотов («оборот по кредиту», «сумма по дебету») -
    # из выписок Halyk, ЦентрКредит и выгрузок 1С.
    Column(
        key="amount_income",
        title="Приход",
        names=(
            "приход", "поступление", "поступления", "кредит", "credit", "дебет счета", "зачисление", "доход",
            "сумма по кредиту", "оборот по кредиту", "обороты по кредиту", "кредит оборот", "оборот кредит",
            "сумма поступления", "зачислено", "поступило", "credit amount", "money in", "deposits",
        ),
        required=False,
    ),
    Column(
        key="amount_expense",
        title="Расход",
        names=(
            "расход", "списание", "списания", "дебет", "debit", "кредит счета", "выплата",
            "сумма по дебету", "оборот по дебету", "обороты по дебету", "дебет оборот", "оборот дебет",
            "сумма списания", "списано", "debit amount", "money out", "withdrawals",
        ),
        required=False,
    ),
    Column(
        key="currency",
        title="Валюта",
        names=("валюта", "currency", "валюта операции", "валюта счета"),
        required=False,
    ),
    Column(
        key="account_from",
        title="Со счёта",
        names=("со счета", "со счёта", "счет списания", "счёт списания", "account from", "откуда"),
        required=False,
    ),
    Column(
        key="account_to",
        title="На счёт",
        names=("на счет", "на счёт", "счет зачисления", "счёт зачисления", "account to", "куда"),
        required=False,
    ),
    Column(
        key="account",
        title="Счёт",
        names=("счет", "счёт", "счет/остаток", "касса", "account"),
        required=False,
    ),
    Column(
        key="kind",
        title="Тип операции",
        names=(
            "тип", "тип операции", "вид операции", "операция", "type", "подтип",
            "дебет/кредит", "д/к", "дт/кт", "dr/cr", "debit/credit",
        ),
        required=False,
    ),
    Column(
        key="category",
        title="Категория",
        # «Назначение» здесь было и уводило в статью назначение платежа из
        # банковской выгрузки: «Оплата по счёту 311», «Закуп упаковки» - каждая
        # строка заводила свою статью, и отчёт по статьям рассыпался на сотни
        # однодневок без единой ошибки. Назначение - текст, его место в
        # комментарии, откуда статью проставят правила.
        names=("категория", "статья", "статья затрат", "статья доходов", "category"),
        required=False,
    ),
    Column(
        key="subcategory",
        title="Подкатегория",
        names=("подкатегория", "подстатья", "subcategory"),
        required=False,
    ),
    Column(
        key="counterparty",
        title="Контрагент",
        names=(
            "контрагент", "клиент", "поставщик", "плательщик", "получатель", "counterparty",
            "наименование контрагента", "кто заплатил", "кому заплатили",
            # Банковские выписки юрлиц: одна колонка на обе стороны платежа.
            "наименование бенефициара / отправителя денег", "бенефициар / отправитель",
            "бенефициар", "отправитель / получатель", "получатель / отправитель",
            "плательщик / получатель", "корреспондент", "наименование корреспондента",
            "корреспондент наименование", "контрагент наименование",
            "наименование получателя / отправителя", "beneficiary", "payer / payee",
        ),
        required=False,
    ),
    # Реквизиты контрагента. Не справочник и не текст для человека: по ним
    # перевод на свой же счёт отличается от расхода (см. `app.finance.banks`).
    Column(
        key="counterparty_bin",
        title="БИН контрагента",
        names=(
            "бин контрагента", "иин контрагента", "иин/бин контрагента", "бин/иин контрагента",
            "бин корреспондента", "иин/бин корреспондента", "бин/иин корреспондента",
            "корреспондент бин", "корреспондент иин/бин", "корреспондент бин/иин",
            "контрагент бин", "контрагент иин/бин", "иин/бин бенефициара / отправителя денег",
            "иин/бин", "бин/иин", "бин", "иин",
        ),
        required=False,
    ),
    Column(
        key="counterparty_account",
        title="Счёт контрагента",
        names=(
            "иик бенефициара / отправителя денег", "счет контрагента", "счёт контрагента",
            "иик контрагента", "iban контрагента", "счет корреспондента", "счёт корреспондента",
            "иик корреспондента", "корреспондент счет", "корреспондент счёт", "корреспондент иик",
            "контрагент счет", "контрагент счёт", "контрагент иик",
            "счет получателя / отправителя", "счет плательщика / получателя", "counterparty account",
        ),
        required=False,
    ),
    Column(
        key="project",
        title="Проект",
        names=("проект", "направление", "project", "объект"),
        required=False,
    ),
    Column(
        key="subproject",
        title="Подпроект",
        names=("подпроект", "subproject"),
        required=False,
    ),
    Column(key="tags", title="Теги", names=("теги", "тег", "метки", "tags"), required=False),
    Column(
        key="comment",
        title="Комментарий",
        # «Комментарии» - не опечатка «Комментария»: так колонка названа в
        # журналах, которые ведут руками, и без этого написания назначение
        # платежа из такого журнала терялось целиком (нечёткое совпадение
        # «комментари-и» с «комментари-й» не берёт: ни одно не начало другого).
        names=(
            "комментарий", "комментарии", "назначение платежа", "назначение", "описание", "примечание", "comment",
            "детали операции", "содержание операции",
        ),
        required=False,
    ),
    # Номер договора, как его написал источник. Довод разнесения оплаты по
    # договорам: платит нередко не сам клиент (Kaspi Pay от физлица, другое
    # ТОО), и по плательщику оплату не отнести, а по номеру - можно.
    Column(
        key="contract_ref",
        title="№ договора",
        names=(
            "№ дог.", "№ дог", "№ договора", "номер договора", "договор №", "№ контракта", "номер контракта",
            "contract no", "contract number",
        ),
        required=False,
    ),
    # За какой месяц платёж: «ИЮНЬ 2026» в колонке «ОПиУ период» журнала. Это
    # месяц отчёта о прибылях (дата начисления) и подсказка, какой месяц
    # договора оплачен. Не «ДДС Месяц»: тот - месяц движения денег, он и так
    # есть в дате платежа.
    Column(
        key="period_month",
        title="Месяц начисления",
        names=(
            "опиу период", "период опиу", "месяц опиу", "опиу месяц", "месяц начисления", "отчетный месяц",
            "за месяц", "месяц услуги", "период услуги",
        ),
        required=False,
    ),
)

#: Что именно нельзя не знать, чтобы строка стала операцией.
ESSENTIAL = ("paid_at", "amount")

#: Слова итоговой строки - общие для всех входов (`intake.rows`).
TOTAL_WORDS = intake_rows.TOTAL_WORDS

#: Слова, означающие вид операции в колонке «Тип».
KIND_WORDS: dict[str, tuple[str, ...]] = {
    "income": (
        "доход", "поступление", "приход", "income", "зачисление", "кредит", "продажа", "оплата от",
        "пополнение",
    ),
    "expense": (
        "расход", "списание", "выплата", "expense", "дебет", "покупка", "оплата поставщику", "снятие",
    ),
    "transfer": ("перевод", "transfer", "перемещение", "инкассация", "внутренний перевод"),
}

#: Признак стороны одной буквой - колонка «Д/К» в выгрузках банков и 1С.
#: Только целиком: «к» внутри слова ничего не значит.
KIND_CODES: dict[str, str] = {
    "д": "expense", "дт": "expense", "d": "expense", "dr": "expense",
    "к": "income", "кт": "income", "c": "income", "cr": "income",
}


# ── Чтение файла в строки ────────────────────────────────────────────────────


def read_rows(data: bytes, file_name: str) -> list[list[Any]]:
    """Файл → строки. Excel читается с типами ячеек, CSV - как текст.

    Значения ячеек Excel не приводятся к строкам: дата, пришедшая датой, не
    имеет неоднозначности порядка частей, и терять это знание, обратив её в
    текст, - значит создать себе задачу угадывания на пустом месте.

    Читает общий читатель приёмника (`intake.tables`): формат решает
    содержимое, а не расширение - «.xls» из интернет-банка бывает и старым
    Excel, и HTML-таблицей, и XML Excel 2003. Раньше на любой .xls человек
    получал «пересохраните как .xlsx».
    """
    try:
        found = intake_tables.read(data, file_name, detail=False, max_rows=None, hidden_sheets=True)
    except intake_tables.TableError as exc:
        raise ImportError_(str(exc)) from exc
    return _best_table([table.rows for table in found])


def _best_table(tables: Sequence[list[list[Any]]]) -> list[list[Any]]:
    """Таблица, в которой лучше всего видна шапка.

    Берём первую вкладку - как и все импортёры на рынке. Но, в отличие от
    них, если на первой вкладке шапки нет, а на второй есть, читаем вторую:
    выгрузки из банков любят класть титульный лист первым.
    """
    best: list[list[Any]] = []
    best_score = -1
    for table in tables:
        rows = _trim([list(row) for row in table])
        if not rows:
            continue
        try:
            score = _header_score(rows[_guess_header_index(rows)])
        except ImportError_:
            score = 0
        if score > best_score:
            best, best_score = rows, score
    if not best:
        raise ImportError_("В файле нет ни одной заполненной строки")
    return best


def _trim(rows: list[list[Any]]) -> list[list[Any]]:
    """Убрать пустые строки с конца и хвостовые пустые колонки."""
    while rows and not any(_has_value(cell) for cell in rows[-1]):
        rows.pop()
    return rows


_has_value = intake_rows.has_value


# ── Поиск шапки ──────────────────────────────────────────────────────────────


def _header_score(row: Sequence[Any]) -> int:
    """Сколько ячеек строки похожи на известные нам заголовки."""
    score = 0
    for cell in row:
        header = str(cell or "")
        if not squash(header):
            continue
        for column in COLUMNS:
            if column.matches_exact(header) or column.matches_squashed(header):
                score += 2
                break
            if column.matches_loose(header):
                score += 1
                break
    return score


@dataclass(frozen=True)
class Mapping:
    """Какая колонка файла стала каким полем учёта.

    Своя привязка, а не `app.finance.layout.resolve_layout`, и это решение, а не
    дубль. Тот резолвер написан под лист, который читают годами: у колонки есть
    привычная позиция, а спор двух колонок за один заголовок означает, что лист
    подменили, - и отказ читать там правильный.

    Здесь лист каждый раз другой. Заголовок «Дата» законно подходит и под «дату
    платежа», и под «дату начисления», и отказываться читать из-за этого нельзя:
    файл нормальный. Поэтому спор решается порядком объявления в `COLUMNS` -
    сначала то, без чего операции не бывает.

    Что осталось от того резолвера - **запрет угадывать на деньгах**: если на
    денежную колонку претендуют два непохожих заголовка, файл не читается. Цена
    ошибки здесь не «неудобно», а «неверная сумма с уверенным видом».
    """

    columns: dict[str, int]
    how: dict[str, str]
    headers: tuple[str, ...]

    def at(self, key: str) -> int | None:
        return self.columns.get(key)

    def has(self, key: str) -> bool:
        return key in self.columns

    @property
    def used(self) -> set[int]:
        return set(self.columns.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": {
                key: {"index": index, "header": self.headers[index], "how": self.how.get(key, "")}
                for key, index in self.columns.items()
            },
            "width": len(self.headers),
        }


#: Денежные колонки. Для них нечёткое совпадение с несколькими кандидатами -
#: отказ читать файл, а не выбор наугад.
_MONEY_KEYS = frozenset({"amount", "amount_income", "amount_expense"})

#: Насколько заголовок в файле может быть длиннее известного нам названия,
#: чтобы всё ещё считаться им. Три символа - это «Сумма» против «Суммы» и
#: «Дата» против «Даты», то есть падеж и опечатка.
#:
#: Без этого порога нечёткое совпадение хватает лишнее, и это уже случилось:
#: «Номер счёта-фактуры» притянулся к колонке «Счёт» (у той было написание
#: «номер счета»), в него попало «СФ-1024» - и строка легла с замечанием «счёт
#: не найден». Симптом выглядел как ошибка человека, а был нашей жадностью.
_LOOSE_SLACK = 3


def _loose_match(column: Column, header: str) -> bool:
    """Нечёткое совпадение заголовка с названием колонки - с порогом длины."""
    actual = squash(header)
    if len(actual) < 4:
        return False
    for name in column.names:
        wanted = squash(name)
        if len(wanted) < 4:
            continue
        if actual == wanted:
            return True
        if actual.startswith(wanted) and len(actual) - len(wanted) <= _LOOSE_SLACK:
            return True
        if wanted.startswith(actual) and len(wanted) - len(actual) <= _LOOSE_SLACK:
            return True
    return False


def resolve_columns(header: Sequence[Any]) -> Mapping:
    """Привязать заголовки файла к полям учёта.

    Три ступени, от строгой к мягкой, и каждая проходит по всем колонкам в
    порядке объявления: точное совпадение всегда бьёт похожее, а спор за один
    и тот же заголовок решается в пользу того поля, что объявлено раньше.
    """
    headers = [str(value or "") for value in header]
    columns: dict[str, int] = {}
    how: dict[str, str] = {}
    taken: set[int] = set()

    for stage, predicate in (
        ("exact", lambda column, text: column.matches_exact(text)),
        ("squashed", lambda column, text: column.matches_squashed(text)),
        ("loose", _loose_match),
    ):
        for column in COLUMNS:
            if column.key in columns:
                continue
            found = [
                index
                for index, text in enumerate(headers)
                if index not in taken and squash(text) and predicate(column, text)
            ]
            if not found:
                continue
            if len(found) > 1 and stage == "loose" and column.key in _MONEY_KEYS:
                raise ImportError_(
                    f"«{column.title}»: точного заголовка нет, а похожих сразу несколько - "
                    + ", ".join(f"«{headers[index]}»" for index in found)
                    + ". Переименуйте колонку суммы однозначно: читать деньги "
                    "наугад нельзя."
                )
            columns[column.key] = found[0]
            how[column.key] = stage
            taken.add(found[0])

    return Mapping(columns=columns, how=how, headers=tuple(headers))


class HeaderNotFound(ImportError_):
    """В таблице нет строки заголовков - возможно, это не таблица, а выписка
    особого вида, которую знает только её шаблон."""


def _guess_header_index(rows: Sequence[Sequence[Any]], look: int = 40) -> int:
    """Номер строки с шапкой.

    Выгрузки почти всегда начинаются с названия отчёта, периода и реквизитов, и
    шапка таблицы стоит третьей-пятой строкой. Finmap на таком файле падает с
    внутренней ошибкой; здесь шапка ищется как строка с наибольшим числом
    узнанных заголовков. Сорок строк, а не двадцать пять: у выписок юрлиц
    реквизиты над таблицей бывают длиннее двадцати строк.
    """
    best_index, best_score = -1, 0
    for index, row in enumerate(rows[:look]):
        score = _header_score(row)
        if score > best_score:
            best_index, best_score = index, score
    if best_index < 0 or best_score < 2:
        raise HeaderNotFound(
            "Не нашёл строку заголовков. Нужна строка, где названы хотя бы дата "
            "и сумма - например «Дата платежа» и «Сумма»."
        )
    return best_index


def _match_strength(header: str) -> int:
    """Насколько заголовок похож на известную колонку - теми же ступенями, что
    `resolve_columns`: 2 - точно или без оформления, 1 - с опечаткой, 0 - нет.

    Не `_header_score`: тот мягче (для поиска строки шапки это правильно), и
    склейка по нему выбирала «Сумма Дебет», которую привязка колонок потом не
    узнавала, - файл отказывался с «нет колонки суммы».
    """
    if not squash(header):
        return 0
    strength = 0
    for column in COLUMNS:
        if column.matches_exact(header) or column.matches_squashed(header):
            return 2
        if _loose_match(column, header):
            strength = 1
    return strength


def _merge_subheader(rows: Sequence[Sequence[Any]], index: int) -> tuple[list[str], int]:
    """Шапка в две строки: «Сумма» над «Дебет | Кредит», «Корреспондент» над
    «Наименование | БИН | Счёт».

    Так печатают выписки Halyk и ЦентрКредит: верхняя ячейка объединена над
    несколькими колонками, названия денег стоят строкой ниже. Одна верхняя
    строка не называет ни одной денежной колонки - и файл отказывался с «нет
    колонки суммы», хотя суммы в нём есть. Общее для всех входов -
    `intake.header.compose`; здесь - каталог выписок.
    Возвращает шапку и номер первой строки тела.
    """
    return intake_header.compose(rows, index, _match_strength, _header_score, swapped=False)


_header_text = intake_header.text
_looks_like_subheader = intake_header.looks_like_subheader
_is_numbering_row = intake_rows.is_numbering


_PERIOD_WORD = re.compile(r"^([a-zа-я]+)\.?\s*(\d{4}|\d{2})\s*(?:г\.?|года?|year)?$")
_PERIOD_MONTH_YEAR = re.compile(r"^(\d{1,2})[./\-](\d{4})$")
_PERIOD_YEAR_MONTH = re.compile(r"^(\d{4})[./\-](\d{1,2})$")
_PERIOD_YEAR = re.compile(r"^(\d{4})\s*(?:г\.?|года?|year)?$")
#: «3 квартал 2026», «III кв. 2026», «Q3 2026».
_PERIOD_QUARTER = re.compile(r"^(?:([1-4]|iv|iii|ii|i)\s*(?:-?й\s*)?(?:квартал|кв\.?)|q([1-4]))\s*(\d{4})\s*(?:г\.?|года?)?$")
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}


def parse_period(raw: Any, reading: DateReading) -> tuple[date, date, str] | None:
    """Месяц, квартал или год из колонки «за какой месяц»: начало, конец и «month»/«quarter»/«year».

    Только запись целиком: «ИЮНЬ 2026», «06.2026», «2026-06», «3 квартал 2026»,
    «2027 год», дата.
    «Старые (до Май/Июня 2026)» - не месяц, хотя в ней есть и «май», и «июня»,
    и год: подпись остаётся в строке источника, а месяца у операции нет.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)) and raw < 10000:
        # «6» - номер месяца без года, а не дата Excel из 1900 года.
        return None
    if isinstance(raw, (datetime, date, int, float)):
        day = parse_date(raw, reading)
        return _month_span(day.year, day.month) if day else None
    text = _SPACES.sub(" ", str(raw)).strip().lower().replace("ё", "е")
    if not text:
        return None

    def year_of(value: str) -> int:
        number = int(value)
        return number + (2000 if number < 70 else 1900) if number < 100 else number

    match = _PERIOD_WORD.match(text)
    if match and match.group(1)[:3] in _MONTH_NAMES:
        return _month_span(year_of(match.group(2)), _MONTH_NAMES[match.group(1)[:3]])
    match = _PERIOD_MONTH_YEAR.match(text)
    if match:
        return _month_span(int(match.group(2)), int(match.group(1)))
    match = _PERIOD_YEAR_MONTH.match(text)
    if match:
        return _month_span(int(match.group(1)), int(match.group(2)))
    match = _PERIOD_YEAR.match(text)
    if match:
        year = int(match.group(1))
        return date(year, 1, 1), date(year, 12, 31), "year"
    match = _PERIOD_QUARTER.match(text)
    if match:
        raw_quarter = match.group(1) or match.group(2)
        quarter = int(raw_quarter) if raw_quarter.isdigit() else _ROMAN[raw_quarter]
        year = int(match.group(3))
        last = quarter * 3
        return date(year, last - 2, 1), date(year, last, calendar.monthrange(year, last)[1]), "quarter"
    if _date_parts(text):
        day = parse_date(text, reading)
        return _month_span(day.year, day.month) if day else None
    return None


def _month_span(year: int, month: int) -> tuple[date, date, str] | None:
    if not 1 <= month <= 12 or not 1900 <= year <= 2200:
        return None
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1]), "month"


# ── Реквизиты выписки ────────────────────────────────────────────────────────
#
# Над таблицей банк печатает, чей это счёт, за какой период и с какими
# остатками; иногда остаток на конец - под таблицей. Для PDF это читал шаблон
# выписки, для таблиц не читал никто: Excel-выписка Kaspi Business заводилась
# без сверки с банком, а счёт приходилось выбирать в каждой строке.
#
# Реквизиты ищутся по подписям, как колонки - по заголовкам. Подпись и
# значение бывают в одной ячейке («Период: 01.09.2026 - 15.09.2026») или
# рядом (подпись, пустая объединённая ячейка, значение).


@dataclass
class StatementInfo:
    """Что банк напечатал вокруг таблицы операций."""

    account_number: str = ""
    currency: str = ""
    period_start: date | None = None
    period_end: date | None = None
    opening: Decimal | None = None
    closing: Decimal | None = None
    owner: str = ""
    owner_bin: str = ""
    bank: str = ""

    def found(self) -> bool:
        return bool(
            self.account_number
            or self.opening is not None
            or self.closing is not None
            or self.period_start
        )

    def fill(self, other: "StatementInfo") -> None:
        """Дописать пустые поля из другого источника (подвала выписки)."""
        for name in self.__dataclass_fields__:
            if getattr(self, name) in (None, "") and getattr(other, name) not in (None, ""):
                setattr(self, name, getattr(other, name))

    def to_bank(self, account: str | None) -> dict[str, Any]:
        """В том же виде, что `statements.read_statement` отдаёт для PDF: сверка
        с банком одна на все форматы."""
        return {
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "opening_balance": str(self.opening) if self.opening is not None else None,
            "closing_balance": str(self.closing) if self.closing is not None else None,
            "account_number": self.account_number,
            "card_number": "",
            "owner": self.owner,
            "bank_name": self.bank,
            "account": account,
        }


#: Подписи реквизитов: поле, написания, допустимо ли продолжение после подписи
#: («Входящий остаток на 01.09.2026»). Продолжение запрещено коротким и общим
#: словам: «Наименование банка» - не владелец счёта, «Счёт-фактура» - не счёт.
_REQUISITES: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    (
        "account_number",
        (
            "текущий счет", "номер счета", "счет", "лицевой счет", "расчетный счет", "иик", "iban",
            "account", "account number", "счет клиента", "банковский счет",
        ),
        False,
    ),
    ("currency", ("валюта счета", "валюта", "currency"), False),
    ("period", ("период", "за период", "период выписки", "выписка за период", "period"), True),
    (
        "opening",
        (
            "входящий остаток", "остаток на начало", "сальдо на начало", "начальный остаток",
            "входящее сальдо", "остаток входящий", "сальдо входящее", "баланс на начало",
            "opening balance",
        ),
        True,
    ),
    (
        "closing",
        (
            "исходящий остаток", "остаток на конец", "сальдо на конец", "конечный остаток",
            "исходящее сальдо", "остаток исходящий", "сальдо исходящее", "баланс на конец",
            "closing balance",
        ),
        True,
    ),
    (
        "owner",
        (
            "наименование", "клиент", "владелец счета", "владелец", "наименование клиента",
            "наименование организации", "организация", "фио", "account holder",
        ),
        False,
    ),
    ("owner_bin", ("иин/бин", "бин/иин", "бин", "иин", "инн", "бин клиента", "иин клиента"), False),
)

_DATE_IN_TEXT = re.compile(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2}")
_PERIOD_IN_TEXT = re.compile(
    r"\bс\s+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s+(?:г\.?\s*)?по\s+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
    re.IGNORECASE,
)
_DMY = DateReading("dmy", "реквизиты выписки")


def _requisite_label(text: str) -> str | None:
    label = norm(text).rstrip(":").strip()
    if not label:
        return None
    for key, names, open_end in _REQUISITES:
        for name in names:
            if label == name or (open_end and label.startswith(name + " ")):
                return key
    return None


def read_requisites(rows: Sequence[Sequence[Any]]) -> StatementInfo:
    """Реквизиты выписки из строк вокруг таблицы. Ничего не нашлось - пусто."""
    info = StatementInfo()
    texts: list[str] = []
    for cells in rows:
        for position, cell in enumerate(cells):
            if not _has_value(cell):
                continue
            if isinstance(cell, str):
                texts.append(cell)
            if not isinstance(cell, str):
                continue
            label_text, colon, inline = cell.partition(":")
            key = _requisite_label(label_text if colon else cell)
            if key is None:
                continue
            values: list[Any] = [inline.strip()] if colon and inline.strip() else []
            values += [value for value in cells[position + 1 :] if _has_value(value)]
            if values:
                _set_requisite(info, key, values)

    joined = "\n".join(texts)
    if not info.account_number:
        found = banks.find_accounts(joined)
        if len(found) == 1:
            info.account_number = found[0]
    if not info.period_start:
        match = _PERIOD_IN_TEXT.search(joined)
        if match:
            info.period_start = parse_date(match.group(1), _DMY)
            info.period_end = parse_date(match.group(2), _DMY)
    info.bank = banks.bank_name(number=info.account_number, text=joined)
    return info


def _set_requisite(info: StatementInfo, key: str, values: list[Any]) -> None:
    first = values[0]
    if key == "account_number" and not info.account_number:
        found = banks.find_accounts(first) or [banks.account_key(first)]
        info.account_number = found[0]
    elif key == "currency" and not info.currency:
        text = str(first).upper()
        match = re.search(r"\b([A-Z]{3})\b", text)
        if match:
            info.currency = match.group(1)
        elif "ТЕНГЕ" in text or "₸" in text:
            info.currency = "KZT"
    elif key == "period" and not info.period_start:
        days: list[date] = []
        for value in values:
            if isinstance(value, (datetime, date)):
                days.append(parse_date(value, _DMY))
                continue
            for piece in _DATE_IN_TEXT.findall(str(value)):
                parsed = parse_date(piece, _DMY)
                if parsed:
                    days.append(parsed)
        if days:
            info.period_start, info.period_end = days[0], days[-1]
    elif key in ("opening", "closing") and getattr(info, key) is None:
        money = parse_money(first)
        if money is not None:
            setattr(info, key, -money.value if money.negative else money.value)
    elif key == "owner" and not info.owner:
        name, party = banks.split_party(first)
        info.owner = name
        if party and not info.owner_bin:
            info.owner_bin = party
    elif key == "owner_bin" and not info.owner_bin:
        info.owner_bin = banks.party_id(first)


# ── Разбор строк ─────────────────────────────────────────────────────────────


@dataclass
class ParsedRow:
    """Строка файла, разобранная до значений операции."""

    line: int
    raw: dict[str, Any]
    state: str  # imported | skipped | failed (пока без записи - намерение)
    problems: list[dict[str, str]] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)

    def problem(self, field_key: str, text: str) -> None:
        self.problems.append({"field": field_key, "text": text})


@dataclass
class Preview:
    """Что мы поняли про файл целиком.

    `question` - единственное место, где импорт останавливается и спрашивает.
    Сюда попадает только то, чего нельзя решить по данным: порядок частей даты,
    когда все даты подходят под оба чтения. Всё остальное решается и
    показывается как решение.
    """

    file_name: str
    header_line: int
    mapping: dict[str, Any]
    unused_columns: list[str]
    rows: list[ParsedRow]
    date_reading: DateReading
    question: dict[str, Any] | None = None
    accounts_missing: list[str] = field(default_factory=list)
    #: Реквизиты и остатки, напечатанные банком в выписке, - PDF или таблицей.
    bank: dict[str, Any] | None = None
    #: Свои счета, которых нет в справочнике: на них уходили переводы между
    #: своими счетами. Имя - подсказка, номер - из выписки.
    accounts_suggested: list[dict[str, Any]] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        """Сколько строк в каком состоянии. Сумма частей равна `total`.

        `duplicate` появился не для симметрии. Пока его не было, предпросмотр
        повторной загрузки того же файла показывал «готово 2050», а заводилось
        ноль: повторы отсеивались уже после нажатия. Экран, на котором
        принимают решение, обязан показывать то, что случится, - иначе он
        просит согласия на цифру, которой не будет.
        """
        counter = Counter(row.state for row in self.rows)
        return {
            "total": len(self.rows),
            "ready": counter.get("imported", 0),
            "failed": counter.get("failed", 0),
            "skipped": counter.get("skipped", 0),
            "duplicate": counter.get("duplicate", 0),
        }


def _kind_from_word(text: str) -> str | None:
    lowered = norm(text)
    if not lowered:
        return None
    if lowered.rstrip(".") in KIND_CODES:
        return KIND_CODES[lowered.rstrip(".")]
    for kind, words in KIND_WORDS.items():
        if any(word in lowered for word in words):
            return kind
    return None


_is_total_row = intake_rows.is_total


def analyze(
    data: bytes,
    file_name: str,
    known_accounts: Sequence[str],
    *,
    date_order: str | None = None,
    default_account: str | None = None,
    account_numbers: dict[str, str] | None = None,
) -> Preview:
    """Разобрать файл и объяснить, что получилось, ничего не записывая.

    `known_accounts` - названия счетов компании. Счёт не создаётся импортом
    никогда: счёт - это место, где лежат деньги, и «создался сам из опечатки в
    выписке» для него недопустимо. Категории, контрагенты, проекты и теги,
    наоборот, создаются: их появление - нормальная работа, а не риск.

    `account_numbers` - номера счетов компании (IBAN → название счёта). По ним
    выписка сама находит свой счёт, а перевод на свой депозит отличается от
    расхода.
    """
    kind = formats.sniff(data, file_name)
    if kind == "pdf":
        return _analyze_statement(
            data,
            file_name,
            known_accounts,
            default_account=default_account,
            account_numbers=account_numbers,
        )

    rows = read_rows(data, file_name)
    try:
        return analyze_rows(
            rows,
            file_name,
            known_accounts,
            date_order=date_order,
            default_account=default_account,
            account_numbers=account_numbers,
        )
    except HeaderNotFound as missing:
        # Шапки таблицы нет. Это может быть выписка особого вида, которую
        # знает только её шаблон (Kaspi Gold в Excel), - пробуем шаблоны. Не
        # узнали и они - человек получает объяснение про шапку, а не про
        # шаблоны, о которых он не просил.
        if kind != "xlsx":
            raise
        try:
            return _analyze_statement(
                data,
                file_name,
                known_accounts,
                default_account=default_account,
                account_numbers=account_numbers,
            )
        except ImportError_:
            raise missing from None


@dataclass
class _RowContext:
    """Решения, принятые один раз на файл, - их видит разбор каждой строки."""

    reading: DateReading
    accounts_by_name: dict[str, str]
    #: Номер счёта → название счёта компании.
    numbers: dict[str, str]
    #: Счёт, на который ложатся строки без своего счёта.
    default_account: str | None
    missing_accounts: set[str]
    #: Выписка одного счёта: колонок счёта в файле нет, счёт - сам файл.
    single: bool = False
    #: Знак суммы несёт направление: в колонке «Сумма» есть и плюсы, и минусы.
    signed: bool = False
    #: БИН владельца счёта из реквизитов выписки.
    owner_bin: str = ""
    #: Свои счета, которых нет в справочнике: номер → сколько строк и чем похож.
    suggested: dict[str, dict[str, Any]] = field(default_factory=dict)


def analyze_rows(
    rows: list[list[Any]],
    file_name: str,
    known_accounts: Sequence[str],
    *,
    date_order: str | None = None,
    default_account: str | None = None,
    account_numbers: dict[str, str] | None = None,
) -> Preview:
    """Разобрать уже прочитанные строки.

    Отделено от `analyze` ради одного: строки приходят не только из файла. Лист
    Google Sheets читается своим клиентом и даёт ровно такие же строки - и он
    обязан пройти тот же разбор, те же замечания и ту же частичную заводку.
    Отдельный разбор «для Google» разъехался бы с этим на первой же правке.
    """
    header_index = _guess_header_index(rows)
    header, body_start = _merge_subheader(rows, header_index)
    layout = resolve_columns(header)
    if not layout.has("paid_at"):
        raise ImportError_(
            "В шапке нет колонки даты платежа. Нужны хотя бы две колонки: "
            "когда и сколько."
        )
    # Сумма приходит либо одной колонкой, либо парой «Приход»/«Расход» - так
    # устроена почти любая банковская выписка, и требовать одну «Сумму» значит
    # не читать выписки вообще.
    if not any(layout.has(key) for key in ("amount", "amount_income", "amount_expense")):
        raise ImportError_(
            "В шапке нет колонки суммы. Подойдёт «Сумма» или пара "
            "«Приход» / «Расход»."
        )

    body = rows[body_start:]

    # Порядок частей даты решается по всему файлу сразу - см. decide_date_order.
    date_cells: list[Any] = []
    for key in ("paid_at", "accrued_at"):
        index = layout.at(key)
        if index is None:
            continue
        date_cells.extend(row[index] for row in body if index < len(row))
    reading = decide_date_order(date_cells)
    if date_order in ("dmy", "mdy"):
        reading = DateReading(date_order, "порядок указан человеком", ambiguous=False)

    accounts_by_name = {norm(name): name for name in known_accounts}
    numbers = _numbers_map(account_numbers)

    # Реквизиты - над шапкой и под последней строкой с датой: остаток на конец
    # часть банков печатает под таблицей.
    info = read_requisites(rows[:header_index])
    info.fill(read_requisites(_tail(body, layout, reading)))

    # Выписка одного счёта: в файле нет ни одной колонки счёта. Тогда счёт -
    # сам файл, и он выбирается один раз: по номеру из реквизитов или вопросом.
    single = not any(layout.has(key) for key in ("account", "account_from", "account_to"))
    chosen = None
    chosen_by = ""
    if default_account:
        chosen = accounts_by_name.get(norm(default_account))
        if chosen is None:
            raise ImportError_(
                f"Счёта «{default_account}» нет в справочнике. "
                "Заведите его в «Справочниках» - импорт счета не создаёт."
            )
        chosen_by = "human"
    elif single and info.account_number and numbers.get(info.account_number):
        chosen = numbers[info.account_number]
        chosen_by = "number"

    ctx = _RowContext(
        reading=reading,
        accounts_by_name=accounts_by_name,
        numbers=numbers,
        default_account=chosen,
        missing_accounts=set(),
        single=single,
        signed=single and _both_signs(body, layout),
        owner_bin=info.owner_bin,
    )

    parsed: list[ParsedRow] = []
    last_operation: ParsedRow | None = None
    for offset, cells in enumerate(body):
        line = body_start + offset + 1  # человеку видна нумерация Excel
        raw = {
            header[i] if i < len(header) and header[i] else f"колонка {i + 1}": _jsonable(cells[i])
            for i in range(len(cells))
            if _has_value(cells[i])
        }
        row = ParsedRow(line=line, raw=raw, state="imported")
        parsed.append(row)

        if not any(_has_value(cell) for cell in cells):
            row.state = "skipped"
            row.problem("", "пустая строка")
            last_operation = None
            continue
        dated = parse_date(_cell_at(cells, layout, "paid_at"), reading) is not None
        if _is_total_row(cells, dated=dated):
            # Итоговая строка - не ошибка файла. Она нужна человеку в книге.
            row.state = "skipped"
            row.problem("", "строка итога - не операция")
            last_operation = None
            continue
        if offset < 2 and _is_numbering_row(cells):
            row.state = "skipped"
            row.problem("", "нумерация колонок под шапкой - не операция")
            continue
        if not dated and not _has_money(cells, layout):
            # Ни даты, ни суммы - денег в строке нет, отложенной ей быть не за
            # что. Раньше подпись банка под таблицей («Отчёт сформирован
            # пользователем…») ложилась тремя замечаниями и числилась среди
            # отложенных операций, которых не было.
            if last_operation is not None and _is_continuation(cells, layout):
                _continue_text(last_operation, cells, layout)
                row.state = "skipped"
                row.problem("", f"продолжение назначения платежа из строки {last_operation.line}")
                continue
            row.state = "skipped"
            row.problem("", "не операция: ни даты, ни суммы - подпись или реквизиты")
            last_operation = None
            continue

        _parse_row(row, cells, layout, ctx)
        last_operation = row

    _number_duplicates(parsed)

    question = None
    if reading.ambiguous and any(row.values.get("paid_at") for row in parsed):
        question = {
            "kind": "date_order",
            "title": "Как записаны даты в этом файле?",
            "text": (
                "Порядок частей даты по файлу определить нельзя: "
                + reading.evidence
                + ". Выберите один раз - он применится ко всем строкам."
            ),
            "samples": list(reading.samples),
            "options": [
                {"value": "dmy", "label": "День · месяц · год", "example": _sample_as(reading.samples, "dmy")},
                {"value": "mdy", "label": "Месяц · день · год", "example": _sample_as(reading.samples, "mdy")},
            ],
        }
    elif single and chosen is None and any(row.values.get("amount") for row in parsed):
        operations = sum(1 for row in parsed if row.values.get("amount"))
        question = _account_question(
            known_accounts,
            lead=f"Прочитали {operations} {_plural(operations, 'операцию', 'операции', 'операций')}.",
            number=info.account_number,
            owner=info.owner,
            bank=info.bank,
            currency=info.currency,
        )

    return Preview(
        file_name=file_name,
        header_line=header_index + 1,
        mapping=layout.to_dict(),
        unused_columns=[
            header[i] for i in range(len(header)) if header[i] and i not in layout.used
        ],
        rows=parsed,
        date_reading=reading,
        question=question,
        accounts_missing=sorted(ctx.missing_accounts),
        bank={**info.to_bank(chosen), "account_by": chosen_by} if single and info.found() else None,
        accounts_suggested=_suggestions(ctx, info),
    )


def _numbers_map(account_numbers: dict[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for number, name in (account_numbers or {}).items():
        key = banks.account_key(number)
        if key and name:
            out[key] = name
    return out


def _cell_at(cells: Sequence[Any], layout: Mapping, key: str) -> Any:
    index = layout.at(key)
    if index is None or index >= len(cells):
        return None
    return cells[index]


_MONEY_COLUMNS = ("amount", "amount_income", "amount_expense")


def _has_money(cells: Sequence[Any], layout: Mapping) -> bool:
    """Есть ли в денежных колонках хоть что-то - даже нечитаемое.

    Нечитаемое тоже считается: «пятьсот» в колонке суммы - это деньги, которые
    не разобрались, и такая строка обязана лечь отложенной с замечанием, а не
    пропасть как подпись.
    """
    return any(_has_value(_cell_at(cells, layout, key)) for key in _MONEY_COLUMNS)


def _is_continuation(cells: Sequence[Any], layout: Mapping) -> bool:
    """Строка - хвост назначения платежа, перенесённый банком на новую строку."""
    text_columns = {layout.at(key) for key in ("comment", "counterparty")} - {None}
    filled = {index for index, cell in enumerate(cells) if _has_value(cell)}
    return bool(filled) and filled <= text_columns


def _continue_text(row: ParsedRow, cells: Sequence[Any], layout: Mapping) -> None:
    for key in ("comment", "counterparty"):
        addition = _text(_cell_at(cells, layout, key))
        if not addition:
            continue
        before = row.values.get(key) or ""
        row.values[key] = f"{before} {addition}".strip()
    row.values["external_key"] = _row_key(row.values)


def _tail(body: Sequence[Sequence[Any]], layout: Mapping, reading: DateReading) -> list[Sequence[Any]]:
    """Строки под последней строкой с датой - подвал выписки."""
    last = -1
    for position, cells in enumerate(body):
        if parse_date(_cell_at(cells, layout, "paid_at"), reading) is not None:
            last = position
    return list(body[last + 1 :])


def _both_signs(body: Sequence[Sequence[Any]], layout: Mapping) -> bool:
    """Есть ли в колонке «Сумма» и приходы, и расходы, записанные знаком.

    Решается один раз на файл, как порядок частей даты. В выписке одного счёта
    с плюсами и минусами знак - это направление, и «+240 000» - поступление.
    Если минусов нет вовсе, знак ничего не говорит: это может быть список одних
    расходов, и строка без знака откладывается с вопросом, а не угадывается.
    """
    if not layout.has("amount") or layout.has("amount_income") or layout.has("amount_expense"):
        return False
    negative = positive = False
    for cells in body:
        money = parse_money(_cell_at(cells, layout, "amount"))
        if money is None or not money.value:
            continue
        if money.negative:
            negative = True
        else:
            positive = True
        if negative and positive:
            return True
    return False


def _account_question(
    known_accounts: Sequence[str],
    *,
    lead: str,
    number: str = "",
    owner: str = "",
    bank: str = "",
    currency: str = "",
) -> dict[str, Any]:
    """Вопрос «на какой счёт» - один для PDF и для таблиц.

    Если в выписке напечатан номер счёта, вопрос называет его и предлагает
    завести счёт с этим номером: у новой компании счетов в справочнике нет, и
    отправлять человека в «Справочники» посреди загрузки - лишний круг.
    """
    if number:
        whose = f" ({owner})" if owner else ""
        text = (
            f"{lead} Это выписка по счёту {number}{whose}, а счёта с таким номером в "
            "справочнике нет. Выберите его или заведите новый - номер запишем счёту, "
            "и следующая выписка ляжет на него сама."
        )
    else:
        text = f"{lead} В файле не сказано, какой это счёт, - выберите его один раз на всю загрузку."
    return {
        "kind": "account",
        "title": "На какой счёт лягут эти операции?",
        "text": text,
        "samples": [],
        "options": [{"value": name, "label": name, "example": ""} for name in known_accounts],
        "create": {
            "name": banks.suggest_account_name(number=number, bank=bank) if number else "",
            "number": number,
            "currency": currency,
        },
    }


def _suggestions(ctx: _RowContext, info: StatementInfo) -> list[dict[str, Any]]:
    """Свои счета из переводов, которых нет в справочнике, - с готовым именем."""
    out: list[dict[str, Any]] = []
    for number, seen in ctx.suggested.items():
        kind = "Депозит" if seen.get("deposit") else ""
        out.append(
            {
                "name": banks.suggest_account_name(number=number, bank=banks.bank_name(number=number), kind=kind),
                "number": number,
                "currency": info.currency,
                "rows": seen.get("rows", 0),
            }
        )
    return out


def _plural(count: int, one: str, few: str, many: str) -> str:
    if 11 <= count % 100 <= 14:
        return many
    if count % 10 == 1:
        return one
    if 2 <= count % 10 <= 4:
        return few
    return many


def _analyze_statement(
    data: bytes,
    file_name: str,
    known_accounts: Sequence[str],
    *,
    default_account: str | None,
    account_numbers: dict[str, str] | None = None,
) -> Preview:
    """Банковская выписка (PDF) → те же строки предпросмотра, что у таблицы.

    Отличие от таблицы одно: счёт в выписке не написан - файл сам и есть счёт.
    Поэтому, пока счёт не выбран, мы не заводим строки «как-нибудь», а
    спрашиваем, на какой счёт их положить. Это тот же механизм `question`, что
    и у порядка дат: спрашиваем один раз на файл и только то, чего в данных
    действительно нет. Номер счёта, напечатанный в выписке и записанный у
    счёта в справочнике, отвечает на вопрос сам.
    """
    accounts_by_name = {norm(name): name for name in known_accounts}
    chosen = None
    chosen_by = ""
    if default_account:
        chosen = accounts_by_name.get(norm(default_account))
        if chosen is None:
            raise ImportError_(
                f"Счёта «{default_account}» нет в справочнике. "
                "Заведите его в «Справочниках» - импорт счета не создаёт."
            )
        chosen_by = "human"

    try:
        parsed = read_statement(data, file_name, account=chosen)
    except StatementError as exc:
        raise ImportError_(str(exc)) from exc

    bank = dict(parsed.get("bank") or {})
    number = banks.account_key(bank.get("account_number"))
    if chosen is None and number:
        chosen = _numbers_map(account_numbers).get(number)
        chosen_by = "number" if chosen else ""

    rows: list[ParsedRow] = []
    for item in parsed["rows"]:
        values = dict(item["values"])
        if chosen and chosen_by == "number":
            # Разбор шёл без счёта - счёт узнан по номеру уже после.
            side = "account_to" if values.get("kind") == "income" else "account_from"
            values[side] = chosen
        row = ParsedRow(line=int(item["line"]), raw=dict(item["raw"]), state="imported")
        if not values.get("paid_at"):
            row.state = "failed"
            row.problem("paid_at", "в строке выписки не разобралась дата")
        if not values.get("amount"):
            row.state = "failed"
            row.problem("amount", "в строке выписки не разобралась сумма")
        if chosen is None:
            row.state = "failed"
            row.problem("account_to", "не выбран счёт, на который лечь операциям")
        values["external_key"] = fingerprint(values)
        row.values = values
        rows.append(row)

    _number_duplicates(rows)

    question = None
    if chosen is None:
        question = _account_question(
            known_accounts,
            lead=f"Прочитали {parsed['count']} операций шаблоном «{parsed['parser_key']}».",
            number=number,
            bank=banks.bank_name(number=number),
        )

    return Preview(
        file_name=file_name,
        header_line=0,
        mapping={
            "columns": {
                "paid_at": {"index": 0, "header": "Дата", "how": "statement"},
                "amount": {"index": 1, "header": "Сумма", "how": "statement"},
                "kind": {"index": 2, "header": "Операция", "how": "statement"},
                "comment": {"index": 3, "header": "Детали", "how": "statement"},
            },
            "width": 4,
            "parser": parsed["parser_key"],
        },
        unused_columns=[],
        rows=rows,
        date_reading=DateReading("dmy", f"выписка прочитана шаблоном «{parsed['parser_key']}»"),
        question=question,
        # Раньше здесь, пока счёт не выбран, стоял список ВСЕХ счетов компании:
        # экран показывал «Счетов нет в справочнике: Банковский счёт, Касса» и
        # кнопку их завести, которая падала на «счёт уже есть». Отсутствующих
        # счетов у выписки нет - есть невыбранный, и о нём спрашивает вопрос.
        accounts_missing=[],
        bank={**bank, "account": chosen, "account_by": chosen_by},
    )


def _number_duplicates(rows: list[ParsedRow]) -> None:
    """Различить одинаковые строки ВНУТРИ одного файла.

    Отпечаток считается по смыслу строки (дата, сумма, счета, комментарий), и
    это правильно: в перезакачанной выписке та же операция получает тот же
    отпечаток, и повторная загрузка не заводит вторую копию.

    Но две одинаковые операции в один день - обычное дело: два раза по 77 ₸ в
    Magnum. В выписке Kaspi Gold за год таких пар оказалось 219 из 2050, и все
    они молча не завелись как «уже есть». Деньги пропали из учёта, а сообщение
    выглядело буднично: «повторов 219».

    Поэтому к отпечатку добавляется номер вхождения в этом файле. Повторная
    загрузка того же файла даёт ту же последовательность, значит дедупликация
    между файлами продолжает работать; а два одинаковых платежа внутри файла
    получают разные отпечатки и заводятся оба.
    """
    seen: Counter[str] = Counter()
    for row in rows:
        key = row.values.get("external_key")
        if not key:
            continue
        seen[key] += 1
        if seen[key] > 1:
            row.values["external_key"] = f"{key}#{seen[key]}"

def _sample_as(samples: Sequence[str], order: str) -> str:
    """Как будет прочитан первый пример при выбранном порядке."""
    if not samples:
        return ""
    parsed = parse_date(samples[0], DateReading(order))
    return f"{samples[0]} → {parsed.isoformat()}" if parsed else samples[0]


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _parse_row(
    row: ParsedRow,
    cells: Sequence[Any],
    layout: Mapping,
    ctx: _RowContext,
) -> None:
    """Разобрать одну строку. Замечания складываются, строка не бросает."""

    def cell(key: str) -> Any:
        return _cell_at(cells, layout, key)

    reading = ctx.reading
    paid_at = parse_date(cell("paid_at"), reading)
    if paid_at is None:
        raw_date = cell("paid_at")
        row.state = "failed"
        if _has_value(raw_date):
            row.problem("paid_at", f"не понял дату «{str(raw_date).strip()}»")
        else:
            row.problem("paid_at", "нет даты платежа")
    row.values["paid_at"] = paid_at.isoformat() if paid_at else None

    accrued = parse_date(cell("accrued_at"), reading)
    row.values["accrued_at"] = accrued.isoformat() if accrued else None
    for key in ("period_start", "period_end"):
        value = parse_date(cell(key), reading)
        row.values[key] = value.isoformat() if value else None
    # «За какой месяц» - период начисления, а у месяца ещё и дата начисления:
    # по ней отчёт о прибылях кладёт платёж в свой месяц. Колонки периода и
    # даты начисления, если они есть в файле, точнее - их не перебиваем.
    span = parse_period(cell("period_month"), reading)
    row.values["period_label"] = ""
    if span is not None:
        start, end, unit = span
        if not row.values["period_start"] and not row.values["period_end"]:
            row.values["period_start"], row.values["period_end"] = start.isoformat(), end.isoformat()
        if unit == "month" and not row.values["accrued_at"]:
            row.values["accrued_at"] = start.isoformat()
    elif _has_value(cell("period_month")):
        # Период назван, но не месяцем: «Старые (до Май/Июня 2026)». Месяца у
        # операции нет, а подпись остаётся - оплата не за месяцы учёта.
        row.values["period_label"] = _text(cell("period_month")) or ""
    row.values["contract_ref"] = _text(cell("contract_ref")) or ""

    # Сумма и вид операции разбираются вместе: в двухколоночной выписке вид
    # задаёт как раз то, в какой из колонок стоит число.
    income = parse_money(cell("amount_income"))
    expense = parse_money(cell("amount_expense"))
    plain = parse_money(cell("amount"))
    kind = _kind_from_word(str(cell("kind") or ""))
    if ctx.single and kind == "transfer":
        # «Перевод» в выписке одного счёта - перевод человеку, а не между
        # своими счетами: направление у него задаёт колонка или знак.
        # Переводы между своими счетами узнаются по реквизитам, ниже.
        kind = None

    amount: Decimal | None = None
    if income and income.value:
        amount, kind = income.value, kind or "income"
    if expense and expense.value:
        if amount is not None:
            # Обе колонки заполнены - это не операция, а свод. Угадывать нельзя.
            row.state = "failed"
            row.problem("amount", "заполнены обе колонки - и «Приход», и «Расход»")
        else:
            amount, kind = expense.value, kind or "expense"
    if amount is None and plain is not None:
        amount = plain.value
        if plain.negative:
            # Минус - это расход. Finmap здесь теряет знак и записывает доход.
            kind = "expense"
        elif kind is None and ctx.signed:
            # В этом файле знак несёт направление (см. `_both_signs`).
            kind = "income"

    if amount is None:
        raw_amount = cell("amount") or cell("amount_income") or cell("amount_expense")
        row.state = "failed"
        if _has_value(raw_amount):
            row.problem("amount", f"не понял сумму «{str(raw_amount).strip()}»")
        else:
            row.problem("amount", "нет суммы")
    row.values["amount"] = str(amount) if amount is not None else None

    # Счета. Название приводится к счёту компании; неизвестное имя - замечание
    # со списком похожих, а не молчаливая подстановка. Номер счёта вместо
    # названия тоже узнаётся - если он записан у счёта в справочнике.
    def account(key: str) -> str | None:
        raw = cell(key)
        if not _has_value(raw):
            return None
        name = str(raw).strip()
        found = ctx.accounts_by_name.get(norm(name)) or ctx.numbers.get(banks.account_key(name))
        if found:
            return found
        close = [real for key_, real in ctx.accounts_by_name.items() if key_.startswith(norm(name)[:4])]
        # Строка без даты и без суммы - не операция, а повтор шапки внутри
        # данных («Дата | ТОО | Сумма» во второй строке выгрузки Kaspi Pay): её
        # «ТОО» в колонке счёта просило завести счёт «ТОО». Замечание у строки
        # остаётся, в список «завести счета» она не идёт.
        if paid_at is not None or amount is not None:
            ctx.missing_accounts.add(name)
        row.state = "failed"
        hint = f"; похожие есть: {', '.join(sorted(close)[:3])}" if close else ""
        row.problem(key, f"счёт «{name}» не найден{hint}")
        return None

    account_from = account("account_from")
    account_to = account("account_to")
    single = account("account")

    if account_from and account_to:
        kind = "transfer"
    elif kind is None:
        # Ни типа, ни двух счетов: одна колонка счёта и сумма без знака.
        kind = "income" if account_to else ("expense" if account_from else None)

    if kind is None:
        row.state = "failed"
        row.problem(
            "kind",
            "не понял, доход это или расход: нет ни колонки типа, ни знака суммы, "
            "ни разделения счетов",
        )

    if single and not account_from and not account_to:
        if kind == "expense":
            account_from = single
        else:
            account_to = single
    elif kind == "expense" and account_to and not account_from:
        # Знак суммы сказал «деньги ушли», а счёт в файле стоит в колонке
        # «На счёт»: в выписке с одной колонкой счёта так и бывает. Счёт
        # переносится в «со счёта» - иначе строка легла бы с замечанием
        # «не указано, с какого счёта ушли деньги», хотя счёт указан.
        account_from, account_to = account_to, None
    elif kind == "income" and account_from and not account_to:
        account_to, account_from = account_from, None

    # Контрагент и его реквизиты. БИН из ячейки «ТОО "Альфа"\nИИН/БИН …»
    # отделяется от имени, иначе один контрагент жил бы в справочнике под
    # столькими именами, сколькими способами банк его напечатал.
    party = _text(cell("counterparty"))
    party_bin = banks.party_id(cell("counterparty_bin"))
    if party:
        name, found_bin = banks.split_party(party)
        if found_bin:
            party, party_bin = (name or None), (party_bin or found_bin)
    other_number = banks.account_key(cell("counterparty_account"))

    # Перевод между своими счетами. В выписке одного счёта он выглядит
    # расходом или доходом, и без реквизитов так и ложился: «на Депозит
    # 1 200 000» становился расходом, отчёт о прибыли врал на миллион.
    outgoing: bool | None = None
    own_missing = False
    if ctx.single and kind in ("income", "expense"):
        other = ctx.numbers.get(other_number) if other_number else None
        own_bin = bool(ctx.owner_bin and party_bin and party_bin == ctx.owner_bin)
        if other and other != ctx.default_account:
            outgoing = kind == "expense"
            kind = "transfer"
            if outgoing:
                account_to = other
            else:
                account_from = other
        elif own_bin and not other:
            outgoing = kind == "expense"
            kind = "transfer"
            own_missing = True
            where = f" {other_number}" if other_number else ""
            row.state = "failed"
            row.problem(
                "account_to" if outgoing else "account_from",
                f"перевод между своими счетами: счёта{where} нет в справочнике - "
                "заведите его, и строка ляжет переводом",
            )
            if other_number:
                seen = ctx.suggested.setdefault(other_number, {"rows": 0, "deposit": False})
                seen["rows"] += 1
                seen["deposit"] = seen["deposit"] or "депозит" in norm(_text(cell("comment")) or "")

    if ctx.default_account:
        if kind == "income" and not account_to:
            account_to = ctx.default_account
        elif kind == "expense" and not account_from:
            account_from = ctx.default_account
        elif kind == "transfer":
            if outgoing is True and not account_from:
                account_from = ctx.default_account
            elif outgoing is False and not account_to:
                account_to = ctx.default_account
            elif outgoing is None and not account_to:
                account_to = ctx.default_account

    if kind == "income" and not account_to:
        row.state = "failed"
        row.problem("account_to", "не указано, на какой счёт пришли деньги")
    if kind == "expense" and not account_from:
        row.state = "failed"
        row.problem("account_from", "не указано, с какого счёта ушли деньги")
    if kind == "transfer" and not (account_from and account_to) and not own_missing:
        row.state = "failed"
        row.problem(
            "account_from" if not account_from else "account_to",
            "для перевода нужны оба счёта - и откуда, и куда",
        )

    row.values["kind"] = kind
    row.values["account_from"] = account_from
    row.values["account_to"] = account_to

    row.values["currency"] = (str(cell("currency")).strip().upper() or None) if _has_value(cell("currency")) else None
    row.values["category"] = _text(cell("category"))
    row.values["subcategory"] = _text(cell("subcategory"))
    # У перевода между своими счетами контрагента нет: это та же компания.
    row.values["counterparty"] = None if outgoing is not None else party
    row.values["project"] = _text(cell("project"))
    row.values["subproject"] = _text(cell("subproject"))
    row.values["comment"] = _text(cell("comment")) or ""
    tags = _text(cell("tags"))
    row.values["tags"] = [part.strip() for part in re.split(r"[,;]", tags) if part.strip()] if tags else []
    if party_bin and outgoing is None:
        row.values["counterparty_bin"] = party_bin
    if other_number:
        row.values["counterparty_account"] = other_number
    if outgoing is not None:
        # Направление - с точки зрения счёта выписки. Нужно сверке с банком,
        # пока счёт выписки ещё не выбран и по счетам его не понять.
        row.values["own_transfer"] = "out" if outgoing else "in"

    row.values["external_key"] = _row_key(row.values)


def _row_key(values: dict[str, Any]) -> str:
    """Отпечаток строки; у перевода между своими счетами - свой.

    Такой перевод виден в двух выписках: как списание в выписке счёта и как
    поступление в выписке депозита. Контрагент и назначение там могут быть
    напечатаны по-разному, а операция одна. Поэтому его отпечаток - только
    дата, сумма и оба счёта: вторая выписка находит перевод уже заведённым.
    """
    if values.get("own_transfer"):
        parts = [
            "own-transfer",
            str(values.get("paid_at") or ""),
            str(values.get("amount") or ""),
            str(values.get("account_from") or ""),
            str(values.get("account_to") or ""),
        ]
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return fingerprint(values)


def _text(value: Any) -> str | None:
    if not _has_value(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return _SPACES.sub(" ", str(value)).strip() or None


def fingerprint(values: dict[str, Any]) -> str:
    """Отпечаток операции - по смыслу строки, а не по её номеру.

    Номер строки не годится: в выписку дописывают операции сверху, и тогда та
    же самая операция получит другой номер и заведётся второй раз. Поэтому в
    отпечаток идут дата, сумма, счета, контрагент и комментарий.
    """
    parts = [
        str(values.get("paid_at") or ""),
        str(values.get("amount") or ""),
        str(values.get("kind") or ""),
        str(values.get("account_from") or ""),
        str(values.get("account_to") or ""),
        str(values.get("counterparty") or ""),
        (values.get("comment") or "")[:120],
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


__all__ = [
    "COLUMNS",
    "Mapping",
    "resolve_columns",
    "DateReading",
    "ImportError_",
    "Money",
    "ParsedRow",
    "Preview",
    "analyze",
    "decide_date_order",
    "fingerprint",
    "parse_date",
    "parse_money",
    "parse_period",
    "read_rows",
]
