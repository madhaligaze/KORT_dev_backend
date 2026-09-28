"""Банковские реквизиты в строках выписки: номер счёта, БИН, банк.

Зачем они разбору, а не только глазам
─────────────────────────────────────
Выписка счёта ТОО в Excel (Kaspi Business, Halyk, ЦентрКредит — у всех
одинаково) называет контрагента одной ячейкой «ТОО "Альфа"\\nИИН/БИН
200940008138» и рядом даёт его счёт. По этим двум полям видно то, чего не видно
по сумме:

* **Перевод на свой депозит — не расход.** «Перевод со счёта Kaspi Pay на
  Депозит» на 1 200 000 с контрагентом — самой же компанией — без реквизитов
  ложился расходом, и отчёт о прибыли врал на миллион без единой ошибки.
  Совпал БИН контрагента с БИН владельца счёта — это движение между своими
  счетами, и это факт из данных, а не догадка.
* **Счёт выписки узнаётся по номеру.** Номер счёта напечатан в шапке выписки;
  если он записан у счёта в справочнике, спрашивать «на какой счёт» не нужно.
"""
from __future__ import annotations

import re
from typing import Any

#: Номер счёта: IBAN (казахстанский — 20 знаков) или иной номер от 8 знаков.
#: Банки печатают IBAN и слитно, и группами по четыре.
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b")

#: БИН или ИИН внутри ячейки контрагента: «ИИН/БИН 200940008138», «БИН: …».
_PARTY_ID = re.compile(
    r"[,;|(]?\s*(?:ИИН|БИН|ИНН|IIN|BIN)(?:\s*/\s*(?:ИИН|БИН|IIN|BIN))?\s*[:№]?\s*(\d{10,12})\)?",
    re.IGNORECASE,
)

#: Банк по БИК. Только те, в чьих кодах уверены: название нужно, чтобы
#: предложить имя нового счёта, и неверное имя хуже пустого.
_BANK_BY_BIC = {
    "CASPKZKA": "Kaspi",
    "HSBKKZKX": "Halyk",
    "KCJBKZKX": "ЦентрКредит",
    "IRTYKZKA": "Forte",
    "TSESKZKA": "Jusan",
    "EURIKZKA": "Евразийский",
    "KSNVKZKA": "Freedom",
}

#: Банк по коду в казахстанском IBAN (знаки 5–7). Коды сверены с БИК тех же
#: строк в настоящих выписках: 722 — CASPKZKA, 601 — HSBKKZKX, 856 — KCJBKZKX.
_BANK_BY_IBAN_CODE = {"722": "Kaspi", "601": "Halyk", "856": "ЦентрКредит"}

_BIC = re.compile(r"\b([A-Z]{4}KZ[A-Z0-9]{2})\b")


def account_key(value: Any) -> str:
    """Номер счёта без оформления: без пробелов, дефисов и регистра.

    Пустая строка — если это не номер: меньше восьми знаков или нет ни одной
    цифры. «Касса» номером счёта не станет, а «KZ87 722S 0000 2583 1219» и
    «kz87722s000025831219» — один и тот же счёт.
    """
    text = re.sub(r"[\s\-]", "", str(value or "")).upper()
    if len(text) < 8 or not any(char.isdigit() for char in text):
        return ""
    if not re.fullmatch(r"[A-Z0-9]+", text):
        return ""
    return text


def find_accounts(text: Any) -> list[str]:
    """Все номера счетов (IBAN) в тексте — в порядке появления, без повторов."""
    found: list[str] = []
    for match in _IBAN.finditer(str(text or "").upper()):
        key = account_key(match.group(0))
        if key and key not in found:
            found.append(key)
    return found


def party_id(value: Any) -> str:
    """БИН/ИИН из отдельной ячейки: 10–12 цифр, иначе пусто."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if 10 <= len(digits) <= 12 else ""


def split_party(text: Any) -> tuple[str, str]:
    """«ТОО "Альфа"\\nИИН/БИН 200940008138» → («ТОО "Альфа"», «200940008138»).

    Без разделения БИН становился частью имени, и один контрагент в справочнике
    жил столько раз, сколькими способами банк его напечатал.
    """
    raw = str(text or "")
    match = _PARTY_ID.search(raw)
    if not match:
        return _clean_name(raw), ""
    name = raw[: match.start()] + " " + raw[match.end() :]
    return _clean_name(name), match.group(1)


def _clean_name(text: str) -> str:
    name = re.sub(r"[\s\xa0]+", " ", text.replace("|", " ")).strip()
    return name.strip(" ,;-")


def bank_name(*, number: str = "", text: str = "") -> str:
    """Банк по БИК, напечатанному в тексте, или по коду в IBAN."""
    for bic in _BIC.findall(str(text or "").upper()):
        if bic in _BANK_BY_BIC:
            return _BANK_BY_BIC[bic]
    key = account_key(number)
    if key.startswith("KZ") and len(key) == 20:
        return _BANK_BY_IBAN_CODE.get(key[4:7], "")
    return ""


def suggest_account_name(*, number: str, bank: str = "", kind: str = "") -> str:
    """Имя для нового счёта: «Kaspi ·1219», «Депозит ·5920».

    Только подсказка в поле, которое человек может переписать: название счёта
    — его решение, а по нему потом читают все отчёты.
    """
    tail = account_key(number)[-4:]
    head = kind or bank or "Счёт"
    return f"{head} ·{tail}" if tail else head


__all__ = [
    "account_key",
    "bank_name",
    "find_accounts",
    "party_id",
    "split_party",
    "suggest_account_name",
]
