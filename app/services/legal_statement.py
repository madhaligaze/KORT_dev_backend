"""Чья выписка — юрлица или физлица, и как читать выписку юрлица.

Вид таблицы выбирается по владельцу счёта, а не по банку. Раньше вид для
юрлиц («Юр счёт»: дата, приход, расход, контрагент, комментарий) был только у
шаблона Kaspi Business, и выписка ТОО из Halyk уходила в Excel в виде для
физлица: контрагент и назначение платежа в одной ячейке, а в итогах нули в
«Пополнениях» при миллионе прихода.

Признаки владельца, от сильного к слабому:

* БИН владельца. У БИН пятая цифра — вид организации (4, 5, 6), у ИИН на её
  месте первая цифра дня рождения (0–3), так что БИН от ИИН отличается по
  самому номеру. ИИН юрлица не отменяет: у ИП номер — ИИН.
* Организационная форма в имени владельца: ТОО, АО, ИП, LLP…
* Шапка таблицы юрлица: контрагент, назначение платежа, дебет и кредит.
* Против: «ФИО» и номер карты в реквизитах — так печатают выписку физлица.

Одной шапки таблицы мало: такие же колонки бывают у текущего счёта физлица.
Нужен владелец — номер или имя.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

from app.schemas.statement import StatementTotals, StatementTransaction

LEGAL = "legal"
PERSONAL = "personal"

_TAX_ID = re.compile(r"(?<!\d)(\d{12})(?!\d)")

_SHORT_FORMS = (
    "ТОО", "АО", "АҚ", "ЖШС", "ИП", "ЖК", "ЧК", "ОО", "ОФ", "ОЮЛ", "КХ", "ФХ",
    "ПК", "ГУ", "КГУ", "РГП", "РГКП", "ГКП", "КГП", "ЧУ", "ООО", "ОАО", "ЗАО", "ПАО",
    "LLP", "LLC", "JSC",
)
_LEGAL_FORM = re.compile(
    r"^\s*[«\"'“]?(?:" + "|".join(_SHORT_FORMS) + r")(?![\w-])"
    r"|товарищество\s+с\s+ограниченной"
    r"|акционерное\s+общество"
    r"|индивидуальный\s+предприниматель"
    r"|крестьянское\s+(?:\(фермерское\)\s+)?хозяйство"
    r"|жауапкершілігі\s+шектеулі"
    r"|акционерлік\s+қоғам"
    r"|жеке\s+кәсіпкер"
    r"|(?<![\w-])(?:LLP|LLC|Ltd|JSC|Inc|GmbH)\.?\s*$",
    re.IGNORECASE,
)
_PERSONAL_MARKERS = re.compile(
    r"(?<!\w)фио(?!\w)|номер\s+карт|держатель\s+карт|kaspi\s+gold",
    re.IGNORECASE,
)

# Полное название формы → как её пишут в учёте. Банк печатает и так и так:
# у Halyk в одной выписке «Товарищество с ограниченной ответственностью
# "Алатау Пауэр"» и «ТОО "К.Эл.М"».
_LONG_FORMS = (
    (re.compile(r"товарищество\s+с\s+ограниченной\s+ответственностью", re.IGNORECASE), "ТОО"),
    (re.compile(r"акционерное\s+общество", re.IGNORECASE), "АО"),
    (re.compile(r"индивидуальный\s+предприниматель", re.IGNORECASE), "ИП"),
)
_PARTY_TAX_ID = re.compile(
    r"[,;|(]?\s*(?:ИИН|БИН|ИНН|IIN|BIN)(?:\s*/\s*(?:ИИН|БИН|IIN|BIN))?\s*[:№]?\s*(\d{10,12})\)?",
    re.IGNORECASE,
)
# Служебные номера банка в назначении платежа: номер проводки в начале и
# «Внешний референс» в конце. Назначение платежа они не меняют, а читать
# комментарий мешают. Номер документа у строки остаётся отдельно.
_BANK_REFERENCE = re.compile(r"^\s*референс\s+\d+\s*", re.IGNORECASE)
_EXTERNAL_REFERENCE = re.compile(r"\s*внешний\s+референс\s*:?\s*[\w/-]*\s*", re.IGNORECASE)

_PARTY_HEADERS = ("контрагент", "бенефициар", "корреспондент", "получател", "отправител")
_PURPOSE_HEADERS = ("назначение платежа", "детали платежа", "кнп", "основание платежа")


def tax_id_type(value: object) -> str | None:
    """«bin», «iin» или None — по пятой цифре двенадцатизначного номера."""
    match = _TAX_ID.search(re.sub(r"[\s\xa0]", "", str(value or "")))
    if not match:
        return None
    digits = match.group(1)
    fifth = int(digits[4])
    if fifth in (4, 5, 6):
        return "bin"
    if fifth <= 3:
        return "iin"
    return None


def has_legal_form(name: object) -> bool:
    return bool(_LEGAL_FORM.search(str(name or "")))


def holder_kind(
    *,
    holder: str | None,
    tax_id: str | None,
    header_cells: Iterable[object] = (),
    requisites: str = "",
) -> str | None:
    """LEGAL, PERSONAL или None, если признаков не хватило.

    `requisites` — только реквизиты над таблицей: в самой таблице БИН и формы
    собственности есть у контрагентов, и владельца они не описывают.
    """
    score = 0
    if tax_id_type(tax_id) == "bin":
        score += 3
    if has_legal_form(holder):
        score += 3
    if _corporate_header(header_cells):
        score += 1
    if _PERSONAL_MARKERS.search(requisites or ""):
        score -= 3
    if score >= 2:
        return LEGAL
    if score < 0:
        return PERSONAL
    return None


def _corporate_header(cells: Iterable[object]) -> bool:
    texts = [str(cell or "").lower().replace("\n", " ") for cell in cells]
    texts = [text for text in texts if text.strip()]
    has_party = any(any(marker in text for marker in _PARTY_HEADERS) for text in texts)
    has_purpose = any(any(marker in text for marker in _PURPOSE_HEADERS) for text in texts)
    has_sides = any(text.strip().startswith("дебет") for text in texts) and any(
        text.strip().startswith("кредит") for text in texts
    )
    return has_party and has_purpose and has_sides


def counterparty_name(raw: str | None) -> str:
    """«Товарищество с ограниченной ответственностью "Алатау Пауэр" БИН …» → «ТОО "Алатау Пауэр"»."""
    text = str(raw or "")
    text = _PARTY_TAX_ID.sub(" ", text)
    for pattern, short in _LONG_FORMS:
        text = pattern.sub(short, text)
    text = re.sub(r"[\s\xa0]+", " ", text.replace("|", " "))
    return text.strip(" ,;-")


def counterparty_tax_id(raw: str | None) -> str:
    match = _PARTY_TAX_ID.search(str(raw or ""))
    return match.group(1) if match else ""


def payment_purpose(raw: str | None) -> str:
    text = re.sub(r"[\s\xa0]+", " ", str(raw or "")).strip()
    text = _BANK_REFERENCE.sub("", text)
    text = _EXTERNAL_REFERENCE.sub(" ", text)
    for pattern, short in _LONG_FORMS:
        text = pattern.sub(short, text)
    return re.sub(r"\s+", " ", text).strip(" ,;")


def legal_operation(
    transaction: StatementTransaction,
    *,
    owner_tax_id: str | None = None,
) -> str:
    """Вид операции для учёта юрлица — по назначению платежа и контрагенту.

    Названия совпадают с теми, что даёт шаблон Kaspi Business, чтобы итоги
    считались одинаково («перевод» и «комиссия» ищутся в названии).
    """
    purpose = (transaction.comment or transaction.detail or "").lower().replace("ё", "е")
    income = transaction.income is not None
    if "депозит" in purpose or "вклад" in purpose:
        if "вознагражден" in purpose:
            return "Вознаграждение по депозиту"
        return "Возврат с депозита" if income else "Перевод на депозит"
    if "комисси" in purpose:
        return "Комиссия банка"
    party_id = counterparty_tax_id(transaction.raw_counterparty)
    owner = re.sub(r"\D", "", owner_tax_id or "")
    if owner and party_id and party_id == owner:
        return "Перевод между своими счетами"
    if "перевод" in purpose:
        return "Перевод"
    return "Поступление" if income else "Списание"


def legal_totals(transactions: Iterable[StatementTransaction]) -> StatementTotals:
    """Итоги так же, как у Kaspi Business.

    `topup_total` — весь приход, `purchase_total` — комиссии банка,
    `transfer_total` — переводы в обе стороны. Подписи в Excel и в боте для
    юрлица поэтому другие: см. `totals_labels`.
    """
    totals: dict[str, float] = defaultdict(float)
    for item in transactions:
        operation = (item.operation or "").lower()
        if item.income is not None:
            totals["income_total"] += item.income
            totals["topup_total"] += item.income
        if item.expense is not None:
            totals["expense_total"] += item.expense
        if "комиссия" in operation:
            totals["purchase_total"] += item.expense or 0.0
        if "перевод" in operation:
            totals["transfer_total"] += abs(item.amount)
    return StatementTotals(**{key: round(value, 2) for key, value in totals.items()})


def totals_labels(kind: str | None) -> dict[str, str]:
    """Подписи итогов. У юрлица в `purchase_total` комиссии, а не покупки."""
    if kind == LEGAL:
        return {
            "topup_total": "Поступления",
            "transfer_total": "Переводы",
            "purchase_total": "Комиссии",
            "cash_withdrawal_total": "Снятия",
        }
    return {
        "topup_total": "Пополнения",
        "transfer_total": "Переводы",
        "purchase_total": "Покупки",
        "cash_withdrawal_total": "Снятия",
    }
