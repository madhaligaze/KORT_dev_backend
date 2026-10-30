"""Пакет страны: Республика Казахстан.

Номер участника - БИН и ИИН
───────────────────────────
Двенадцать цифр. Контрольная - последняя: сумма первых одиннадцати с весами
1…11 по модулю 11; если вышло 10 - второй проход с весами 3…11, 1, 2; снова
10 - номер недействителен (такие не выдают).

Вид лица - по пятой цифре. У ИИН это первая цифра дня рождения (0-3), у БИН -
тип юрлица (4 - резидент, 5 - нерезидент, 6 - совместное ИП). Поэтому
«4, 5, 6» на пятом месте - организация, остальное - человек; ИП работает по
ИИН своего владельца, и отличить его от физлица можно только по имени.

Номер не угадывается: двенадцать цифр с неверной контрольной - отказ, а не
«наверное, опечатка». Из свободного текста (строка банка «… БИН 260540028486»,
комментарий журнала) берутся только номера с подписью БИН/ИИН рядом и верной
контрольной цифрой: двенадцать цифр подряд бывают и чем-то другим.

НДС, МРП, ЭСФ
─────────────
Числа года: МРП - законы о республиканском бюджете; порог постановки на учёт
по НДС в МРП и ставка - НК РК (до 2026 года - 20 000 МРП и 12%, с 01.01.2026 -
10 000 МРП и 16%). Компания поправляет год у себя (`core/tax.set_year`): закон
о бюджете выходит раньше обновления KORT.

Режимы: общеустановленный, упрощённая декларация, другой специальный (патент,
розничный налог и т.п.). На специальном режиме плательщиком НДС не бывают -
так с 2026 года по НК РК.

ЭСФ выписывается в течение 15 календарных дней после даты оборота (НК РК).

С 1 марта 2024 года вся страна живёт в одном поясе, UTC+5.

Праздники
─────────
Закон «О праздниках в РК»: национальные и государственные праздники -
нерабочие дни (1-2 января, 8 марта, 21-23 марта, 1, 7 и 9 мая, 6 июля, 30
августа, 25 октября, 16 декабря); совпал с выходным - выходной переносится на
следующий рабочий день (ТК РК). Религиозные праздники (7 января, первый день
Курбан айта) - нерабочие без переноса; дату Курбан айта ежегодно объявляет
правительство, здесь - известные годы. Переносы постановлением правительства
(«мост» между праздником и выходным) пакет не знает: их компания вписывает
себе сама (`core/cash_plans.set_calendar`).
"""
from __future__ import annotations

import re
from datetime import date, timedelta, timezone
from decimal import Decimal
from functools import lru_cache

from app.finance import packs

_WEIGHTS_1 = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
_WEIGHTS_2 = (3, 4, 5, 6, 7, 8, 9, 10, 11, 1, 2)

#: «БИН 260540028486», «ИИН/БИН: 0905 4000 2228», «BIN 090540002228».
_LABELED = re.compile(r"(?:БИН|ИИН|BIN|IIN)[\s:/№#.-]*(?:ИИН|БИН|BIN|IIN)?[\s:№#.-]*((?:\d[\s ]?){12})", re.IGNORECASE)
_IP_PREFIX = re.compile(r"^\s*(?:ип|индивидуальный\s+предприниматель)\b", re.IGNORECASE)


class BinIin(packs.Ids):
    title = "БИН/ИИН"
    length = 12

    def checksum_ok(self, number: str) -> bool:
        if len(number) != 12 or not number.isdigit():
            return False
        values = [int(ch) for ch in number]
        control = sum(v * w for v, w in zip(values, _WEIGHTS_1)) % 11
        if control == 10:
            control = sum(v * w for v, w in zip(values, _WEIGHTS_2)) % 11
            if control == 10:
                return False
        return control == values[11]

    def kind_of(self, number: str, name: str = "") -> str:
        """Вид лица по номеру; у ИИН - ИП, если так начинается имя."""
        if len(number) != 12:
            return ""
        if number[4] in "456":
            return packs.ORG
        return packs.IP if _IP_PREFIX.match(name or "") else packs.PERSON

    def check(self, raw: object, name: str = "") -> packs.Checked:
        """Проверить БИН/ИИН, написанный человеком: пробелы и дефисы не мешают."""
        number = packs.digits(raw)
        if not number:
            return packs.Checked("", "", "БИН или ИИН не указан")
        if len(number) != 12:
            return packs.Checked(number, "", f"В БИН и ИИН 12 цифр, а здесь {len(number)}")
        if not self.checksum_ok(number):
            return packs.Checked(number, "", "Контрольная цифра не сходится - проверьте номер")
        return packs.Checked(number, self.kind_of(number, name))

    def found_in(self, text: object) -> list[str]:
        out: list[str] = []
        for match in _LABELED.finditer(str(text or "")):
            number = packs.digits(match.group(1))
            if self.checksum_ok(number) and number not in out:
                out.append(number)
        return out


IDS = BinIin()

YEARS = {
    2023: packs.TaxNumbers(Decimal("3450"), 20000, Decimal("12")),
    2024: packs.TaxNumbers(Decimal("3692"), 20000, Decimal("12")),
    2025: packs.TaxNumbers(Decimal("3932"), 20000, Decimal("12")),
    2026: packs.TaxNumbers(Decimal("4325"), 10000, Decimal("16")),
}

#: Национальные и государственные праздники (месяц, день) - переносятся с выходного.
_STATE_HOLIDAYS = ((1, 1), (1, 2), (3, 8), (3, 21), (3, 22), (3, 23), (5, 1), (5, 7), (5, 9), (7, 6), (8, 30), (10, 25), (12, 16))
#: Религиозные: Рождество (7 января) и первый день Курбан айта - без переноса.
_KURBAN = {2024: date(2024, 6, 16), 2025: date(2025, 6, 6), 2026: date(2026, 5, 27), 2027: date(2027, 5, 16)}


@lru_cache(maxsize=16)
def holidays(year: int) -> frozenset[date]:
    """Нерабочие праздничные дни года с переносом выходных."""
    days = {date(year, month, day) for month, day in _STATE_HOLIDAYS}
    off = set(days) | {date(year, 1, 7)} | ({_KURBAN[year]} if year in _KURBAN else set())
    for day in sorted(days):
        if day.weekday() >= 5:
            moved = day + timedelta(days=1)
            while moved.weekday() >= 5 or moved in off:
                moved += timedelta(days=1)
            off.add(moved)
    return frozenset(off)


COUNTRY = packs.Country(
    code="KZ",
    title="Казахстан",
    ids=IDS,
    years=YEARS,
    regimes={"general": "общеустановленный", "simplified": "упрощённая декларация", "other": "другой специальный"},
    vat_regimes=frozenset({"general"}),
    vat_regime_refusal="На специальном режиме плательщиком НДС не бывают - выберите общеустановленный",
    esf_days=15,
    tz=timezone(timedelta(hours=5)),
    currency="KZT",
    currency_sign="₸",
    holidays=holidays,
)
