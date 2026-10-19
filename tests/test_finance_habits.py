"""Привычки учётки: вид раздела по умолчанию по тому, чем человек пользуется.

29.09.2026: «сотрудник часто открывает таблицу, а не карточки - при следующих
заходах программа сразу должна показать табличный вид».
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.finance import audit, auth, habits

BASE = "/api/v1/finance"
NOW = datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc)


def test_tablitsa_pobezhdaet_kogda_v_ney_rabotayut() -> None:
    data: dict = {}
    for _ in range(6):
        data = habits.record(data, "contracts", "contracts-sheet", minutes=4, now=NOW)
    data = habits.record(data, "contracts", "contracts", minutes=1, now=NOW)
    assert habits.prefer(data, now=NOW) == {"contracts": "contracts-sheet"}


def test_bez_yavnogo_perevesa_privychki_net() -> None:
    """Поровну - вида по умолчанию нет: открывается выбранный последним."""
    data = habits.record({}, "contracts", "contracts-sheet", minutes=10, now=NOW)
    data = habits.record(data, "contracts", "contracts", minutes=9, now=NOW)
    assert habits.prefer(data, now=NOW) == {}
    # Меньше пяти минут на всё - привычки ещё нет.
    assert habits.prefer(habits.record({}, "x", "y", minutes=3, now=NOW), now=NOW) == {}


def test_novaya_privychka_perevesivaet_staruyu() -> None:
    """Полгода карточек, потом три недели таблицы - таблица, а не карточки."""
    data = habits.record({}, "contracts", "contracts", minutes=30, now=NOW - timedelta(days=60))
    assert habits.prefer(data, now=NOW - timedelta(days=60)) == {"contracts": "contracts"}
    for day in range(21):
        data = habits.record(data, "contracts", "contracts-sheet", minutes=2, now=NOW - timedelta(days=21 - day))
    assert habits.prefer(data, now=NOW) == {"contracts": "contracts-sheet"}


def test_shchelchok_vesit_i_minuty_ogranicheny() -> None:
    data = habits.record({}, "g", "a", pick=True, now=NOW)
    assert data["groups"]["g"]["modes"]["a"] == habits.PICK_WEIGHT
    data = habits.record({}, "g", "a", minutes=10_000, now=NOW)
    assert data["groups"]["g"]["modes"]["a"] == habits.MAX_MINUTES
    with pytest.raises(habits.HabitError):
        habits.record({}, "../x", "a", now=NOW)


@pytest.fixture
def app(tmp_path, monkeypatch) -> FastAPI:
    from sqlalchemy import create_engine

    from app.core.config import settings
    from app.finance import db as finance_db_module
    from finance_routes import finance_app

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    monkeypatch.setattr(settings, "environment", "test")
    auth._ATTEMPTS.clear()
    audit._VIEWS.clear()
    return finance_app()


def test_privychka_za_uchetkoy_a_ne_za_brauzerom(app: FastAPI) -> None:
    owner = TestClient(app)
    owner.post(f"{BASE}/auth/register", json={"email": "owner@bbc.kz", "password": "pass-12345", "company": "BBC", "full_name": "Ермеков Нурболат"})
    assert owner.get(f"{BASE}/looks/habits/prefer").json() == {"prefer": {}}
    for _ in range(3):
        answer = owner.post(f"{BASE}/looks/habits/contracts", json={"mode": "contracts-sheet", "minutes": 3})
        assert answer.status_code == 200, answer.text
    assert answer.json()["prefer"] == {"contracts": "contracts-sheet"}
    # Другой браузер той же учётки - та же привычка.
    other = TestClient(app)
    other.post(f"{BASE}/auth/login", json={"email": "owner@bbc.kz", "password": "pass-12345"})
    assert other.get(f"{BASE}/looks/habits/prefer").json()["prefer"] == {"contracts": "contracts-sheet"}
    # И вместе с «кто я» - вход открывает вид по привычке с первого кадра.
    assert other.get(f"{BASE}/auth/me").json()["habits"] == {"contracts": "contracts-sheet"}
    # Как «вид листа» привычки не читаются и не затираются.
    assert owner.get(f"{BASE}/looks/habits").status_code == 404
    assert owner.put(f"{BASE}/looks/habits", json={"look": {}}).status_code == 404
    assert owner.post(f"{BASE}/looks/habits/contracts", json={"mode": "bad mode"}).status_code == 400
