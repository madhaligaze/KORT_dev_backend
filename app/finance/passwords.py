"""Стойкость пароля при его задании.

Правило простое и объяснимое человеку, а не «одна заглавная, одна цифра, один
знак»: длина от восьми и не из числа тех, что подбирают первыми. Сложные
требования к составу люди обходят предсказуемо (`Password1!`), а длинную фразу
запрещают. Поэтому здесь - только длина и отсев заведомо угадываемых.

**Проверяется только при задании нового пароля** (регистрация, приглашение,
смена, задание по номеру). Уже сохранённые пароли не трогаются: сломать вход
действующим учёткам ради политики нельзя. Слабый, но уже заданный пароль
прикрыт иначе - подтверждением устройства при входе (`auth.py`), потому что
знание пароля перестаёт быть достаточным с незнакомого устройства.
"""
from __future__ import annotations

import re

MIN_LENGTH = 8

#: Заведомо угадываемые пароли и основы: списки утечек начинаются с них. Не
#: весь словарь (его носить незачем) - только то, что пробуют в первые секунды.
#: Сверяется в нижнем регистре и после снятия повторов вида «123123».
_COMMON = frozenset(
    {
        "password", "passw0rd", "qwerty", "qwertyuiop", "qwerty123", "qazwsx",
        "asdfgh", "asdfghjkl", "zxcvbn", "zxcvbnm", "1q2w3e4r", "1q2w3e4r5t",
        "qweasdzxc", "admin", "administrator", "welcome", "letmein", "iloveyou",
        "monkey", "dragon", "sunshine", "princess", "football", "baseball",
        "superman", "batman", "master", "login", "abc123", "abcd1234",
        "password1", "password123", "changeme", "default", "secret", "test1234",
        "123456", "1234567", "12345678", "123456789", "1234567890", "12345",
        "000000", "0000000", "00000000", "111111", "1111111", "11111111",
        "qwerty12345", "passwordpassword",
        # Частое по-русски латиницей и кириллицей.
        "parol", "parol123", "пароль", "пароль123", "йцукен", "йцукенг",
        "123йцукен", "qwertyйцукен",
    }
)

_DIGITS = re.compile(r"^\d+$")
_KEYBOARD_RUNS = ("1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm", "йцукенгшщзхъ", "фывапролджэ", "ячсмитьбю")


def _collapse_repeat(value: str) -> str:
    """«123123123» → «123», «abab» → «ab»: повтор основы - такой же простой."""
    n = len(value)
    for size in range(1, n // 2 + 1):
        if n % size == 0 and value[:size] * (n // size) == value:
            return value[:size]
    return value


def _is_run(value: str) -> bool:
    """Подряд по клавиатуре или по алфавиту: «abcdefgh», «87654321», «qwerty»."""
    low = value.lower()
    for run in _KEYBOARD_RUNS:
        if low in run or low in run[::-1]:
            return True
    if len(set(low)) == 1:
        return True
    forward = all(ord(low[i + 1]) - ord(low[i]) == 1 for i in range(len(low) - 1))
    backward = all(ord(low[i]) - ord(low[i + 1]) == 1 for i in range(len(low) - 1))
    return forward or backward


def problem(password: str) -> str | None:
    """Чем пароль плох, текстом для человека. `None` - пароль годится.

    Длину проверяем по исходной строке, а «слабость» - по снятому повтору и в
    нижнем регистре: «Password123» и «password123» одинаково угадываемы.
    """
    value = password or ""
    if len(value) < MIN_LENGTH:
        return f"Пароль короче {MIN_LENGTH} символов"
    low = value.lower()
    base = _collapse_repeat(low)
    if low in _COMMON or base in _COMMON:
        return "Такой пароль подбирают первым - придумайте другой"
    if _DIGITS.match(value):
        return "Пароль из одних цифр легко подбирается - добавьте буквы"
    if _is_run(value):
        return "Пароль идёт подряд по клавиатуре - придумайте другой"
    return None


def is_weak(password: str) -> bool:
    """Слабый ли уже заданный пароль - для подтверждения устройства при входе.

    Здесь плейнтекст есть только в момент входа, и этого хватает: политика к
    хешу в базе неприменима, а знать, что пароль угадываем, нужно, чтобы
    решить, пускать ли с незнакомого устройства без подтверждения.
    """
    return problem(password or "") is not None


__all__ = ["MIN_LENGTH", "is_weak", "problem"]
