"""БИН и ИИН Казахстана: проверка контрольной цифры и вид лица.

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
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_WEIGHTS_1 = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
_WEIGHTS_2 = (3, 4, 5, 6, 7, 8, 9, 10, 11, 1, 2)

#: Вид лица: организация (БИН) или человек (ИИН: физлицо или ИП).
ORG = "org"
PERSON = "person"
IP = "ip"
KINDS = (ORG, PERSON, IP)

_DIGITS = re.compile(r"\D+")
#: «БИН 260540028486», «ИИН/БИН: 0905 4000 2228», «BIN 090540002228».
_LABELED = re.compile(r"(?:БИН|ИИН|BIN|IIN)[\s:/№#.-]*(?:ИИН|БИН|BIN|IIN)?[\s:№#.-]*((?:\d[\s ]?){12})", re.IGNORECASE)
_IP_PREFIX = re.compile(r"^\s*(?:ип|индивидуальный\s+предприниматель)\b", re.IGNORECASE)


def digits(text: object) -> str:
    """Только цифры: «0905 4000 2228» → «090540002228»."""
    return _DIGITS.sub("", str(text or ""))


def checksum_ok(number: str) -> bool:
    if len(number) != 12 or not number.isdigit():
        return False
    values = [int(ch) for ch in number]
    control = sum(v * w for v, w in zip(values, _WEIGHTS_1)) % 11
    if control == 10:
        control = sum(v * w for v, w in zip(values, _WEIGHTS_2)) % 11
        if control == 10:
            return False
    return control == values[11]


@dataclass(frozen=True)
class Checked:
    """Итог проверки: `number` - очищенный номер, `error` - пусто, если номер годен."""

    number: str
    kind: str
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def kind_of(number: str, name: str = "") -> str:
    """Вид лица по номеру; у ИИН - ИП, если так начинается имя."""
    if len(number) != 12:
        return ""
    if number[4] in "456":
        return ORG
    return IP if _IP_PREFIX.match(name or "") else PERSON


def check(raw: object, name: str = "") -> Checked:
    """Проверить БИН/ИИН, написанный человеком: пробелы и дефисы не мешают."""
    number = digits(raw)
    if not number:
        return Checked("", "", "БИН или ИИН не указан")
    if len(number) != 12:
        return Checked(number, "", f"В БИН и ИИН 12 цифр, а здесь {len(number)}")
    if not checksum_ok(number):
        return Checked(number, "", "Контрольная цифра не сходится - проверьте номер")
    return Checked(number, kind_of(number, name))


def found_in(text: object) -> list[str]:
    """БИН/ИИН, подписанные в тексте и с верной контрольной цифрой, по порядку."""
    out: list[str] = []
    for match in _LABELED.finditer(str(text or "")):
        number = digits(match.group(1))
        if checksum_ok(number) and number not in out:
            out.append(number)
    return out


__all__ = ["Checked", "IP", "KINDS", "ORG", "PERSON", "check", "checksum_ok", "digits", "found_in", "kind_of"]
