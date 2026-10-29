"""Жертва взаимоблокировки повторяется (`app/finance/db.retrying`).

Postgres откатывает одну из двух транзакций, взявших замки в разном порядке
(счётчик компании и проведение журнала). Откаченная целиком транзакция
безопасна для повтора - вторая попытка идёт после коммита победителя. Любая
другая ошибка базы повтором не прикрывается.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from app.finance import db


class _Orig(Exception):
    def __init__(self, sqlstate: str):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _error(sqlstate: str, kind: type = OperationalError) -> Exception:
    return kind("UPDATE finance.counters …", {}, _Orig(sqlstate))


def test_vzaimoblokirovka_povtoryaetsya():
    calls = []

    def run():
        calls.append(1)
        if len(calls) == 1:
            raise _error("40P01")
        return "записано"

    assert db.retrying(run) == "записано" and len(calls) == 2


def test_drugaya_oshibka_ne_povtoryaetsya():
    calls = []

    def run():
        calls.append(1)
        raise _error("23505", IntegrityError)

    with pytest.raises(IntegrityError):
        db.retrying(run)
    assert len(calls) == 1


def test_popytki_konchayutsya():
    calls = []

    def run():
        calls.append(1)
        raise _error("40001")

    with pytest.raises(OperationalError):
        db.retrying(run)
    assert len(calls) == db.ATTEMPTS
