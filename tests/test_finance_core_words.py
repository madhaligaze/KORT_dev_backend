"""Слова компании в колонках таблицы (фаза 9, R12 и R13): «Счет» и «Вид Услуги».

Формула таблицы другой компании пишет состояние счёта своими словами, а не
словами BBC, - без словаря все её счета уходили в «Без срока», а бот не
знал, в какой пост их класть. Слову дают смысл, и список, число у пункта,
бот и сверка понимают его сразу, без нового чтения таблицы.
"""
from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from fastapi import FastAPI

from app.finance import bills_bot, history
from app.finance import service as finance_service
from app.finance.contracts import bills as bills_module
from app.finance.contracts import service
from app.finance.core import settings as core_settings
from app.finance.db import finance_session
from app.finance.models import ActionLog
from app.finance.service import FinanceError

FULL = service.Access(view=True, edit=True, setup=True, admin=True)
OWNER = service.Actor(uuid.uuid4(), "owner@test")
VALUES = [
    ["Наша Фирма", "Заказчик (Название Фирмы)", "№ Договора", "Вид Услуги", "Счет", "Период с (нач.)"],
    ["АЛЬФА", "ТОО Бета", "Д/1", "Ежемесячное ведение", "Задержка", "01.10.2026"],
    ["АЛЬФА", "ТОО Гамма", "Д/2", "Помещение", "Выписан", "01.10.2026"],
    ["АЛЬФА", "ТОО Дельта", "Д/3", "Абон.П.", "Просрочка более 5 дней", "01.10.2026"],
]


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    yield engine


def _board() -> bills_module.Board:
    return bills_module.build_board(VALUES, title="Сводка", worksheet="Сводка")


def test_slova_kompanii_menyayut_smysl_srazu():
    board = _board()
    by_number = {bill.number: bill for bill in [*board.bills, *board.done]}
    assert by_number["Д/1"].group == bills_module.UNDATED and by_number["Д/2"].group == bills_module.UNDATED
    assert by_number["Д/3"].group == bills_module.OVERDUE, "слова BBC KORT знает сам"
    assert [bills_bot.kind_of(by_number[number]) for number in ("Д/1", "Д/2", "Д/3")] == [
        bills_bot.UNKNOWN, bills_bot.UNKNOWN, bills_bot.ABON,
    ]
    words = {"bill": {"задержка": "overdue", "выписан": "issued"}, "kind": {"ежемесячное ведение": "abon", "помещение": "rent"}}
    worded = bills_module.worded(board, words)
    by_number = {bill.number: bill for bill in [*worded.bills, *worded.done]}
    assert by_number["Д/1"].group == bills_module.OVERDUE and by_number["Д/2"].group == bills_module.DONE
    assert by_number["Д/2"].key in worded.issued, "выставленное словом компании - в «Выставлены»"
    assert [bills_bot.kind_of(by_number[number]) for number in ("Д/1", "Д/2", "Д/3")] == [
        bills_bot.ABON, bills_bot.RENT, bills_bot.ABON,
    ]
    assert worded.counts()[bills_module.OVERDUE] == 2


def test_slovo_v_nastroykah_s_pravami_i_zhurnalom(finance_db):
    token = history.CONTEXT.set(history.AuditContext(user_id=OWNER.user_id))
    try:
        with finance_session() as session:
            workspace = finance_service.ensure_workspace(session)
            with pytest.raises(PermissionError):
                core_settings.set_table_word(
                    session, workspace, service.Access(view=True, edit=True), OWNER, part="bill", word="Задержка", meaning="overdue"
                )
            with pytest.raises(FinanceError):
                core_settings.set_table_word(session, workspace, FULL, OWNER, part="bill", word="Задержка", meaning="abon")
            words = core_settings.set_table_word(session, workspace, FULL, OWNER, part="bill", word=" Задержка ", meaning="overdue")
            assert words["bill"] == {"задержка": "overdue"} and words["kind"] == {}
            title = session.scalar(sa.select(ActionLog.title).where(ActionLog.kind == "core.settings"))
            assert title == "слово «Задержка» колонки «Счет» - просрочен"
            assert core_settings.set_table_word(session, workspace, FULL, OWNER, part="bill", word="задержка", meaning="")["bill"] == {}
    finally:
        history.CONTEXT.reset(token)


@pytest.fixture
def app(finance_db, monkeypatch) -> FastAPI:
    from app.core.config import settings
    from app.finance import audit as audit_module
    from app.finance import auth
    from finance_routes import finance_app

    monkeypatch.setattr(settings, "environment", "test")
    auth._ATTEMPTS.clear()
    audit_module._VIEWS.clear()
    bills_module._slots.clear()
    return finance_app()


def test_slova_tablicy_cherez_api(app: FastAPI, monkeypatch) -> None:
    from app.finance.contracts import summary as summary_module
    from test_finance_access import BASE, register

    monkeypatch.setattr(
        summary_module, "_read",
        lambda spreadsheet_id, tab, extra=None: summary_module.build_index(
            [["Заказчик (Название Фирмы)", "№ Договора", "Сумма Факт Поступ."], ["ТОО Бета", "Д/1", "0"]],
            title="Сводка", worksheet=tab, columns=summary_module.columns_for(extra),
        ),
    )
    monkeypatch.setattr(
        bills_module, "_read",
        lambda spreadsheet_id, tab, extra=None: bills_module.build_board(
            VALUES, title="Сводка", worksheet=tab, spreadsheet_id=spreadsheet_id, columns=bills_module.columns_for(extra)
        ),
    )
    owner = register(app)
    connected = owner.post(f"{BASE}/contracts/setup/summary", json={"link": "https://docs.google.com/spreadsheets/d/abc/edit", "worksheet": "Сводка"})
    assert connected.status_code == 200, connected.text
    seen = owner.get(f"{BASE}/core/table-words").json()
    bills_seen = {item["text"]: item for item in seen["seen"]["bill"]}
    assert bills_seen["Задержка"]["how"] == "unknown" and bills_seen["Просрочка более 5 дней"]["meaning"] == "overdue"
    assert {item["text"]: item["how"] for item in seen["seen"]["kind"]} == {
        "Ежемесячное ведение": "unknown", "Помещение": "unknown", "Абон.П.": "kort",
    }
    saved = owner.put(f"{BASE}/core/table-words", json={"part": "bill", "word": "Задержка", "meaning": "overdue"})
    assert saved.status_code == 200, saved.text
    assert {item["text"]: item for item in saved.json()["seen"]["bill"]}["Задержка"] == {
        "text": "Задержка", "rows": 1, "meaning": "overdue", "how": "company",
    }
    board = owner.get(f"{BASE}/bills").json()
    assert {item["number"]: item["group"] for item in board["items"]}["Д/1"] == "overdue", "список понимает слово сразу"
