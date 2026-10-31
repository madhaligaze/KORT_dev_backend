"""Полигон целиком (п.10 страницы этапа 2): все источники «Тумар» в одной компании.

Готово, когда: долги KORT на 25.10 равны правде до тенге - по каждому договору
каждого нашего юрлица; вопросов ровно столько, сколько в каталоге «спросит»,
и это те самые случаи (больше - KORT мучает человека, меньше - угадывает).
Предложения и отказы - тоже по каталогу: случай, у которого KORT остановился,
назван в каталоге этим видом. Робот и порядок - `polygon/whole.py`.
"""
from __future__ import annotations

from collections import Counter

import pytest
from polygon import sources, whole
from polygon.cases import ASK, CASES, OFFER, REFUSE
from polygon.world import build

from app.finance import service as finance_service
from app.finance.config import finance_settings
from app.finance.db import finance_session


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    monkeypatch.setattr(finance_settings, "secrets_key", finance_settings.secrets_key or "polygon-test")
    yield engine


@pytest.fixture(scope="module")
def polygon():
    world = build()
    return world, sources.render(world)


def test_poligon_celikom_dohodit_do_pravdy(polygon, finance_db) -> None:
    world, files = polygon
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        result = whole.run(session, workspace, world, files)

    # Входы не двоят друг друга: четыре наших юрлица, каждый договор правды - один раз.
    assert len(result.entities) == 4, result.entities
    assert result.by_contract == {code: {} for code in world.entities}, result.by_contract
    for code, truth in world.owed().items():
        assert result.owed.get(code, 0) == truth, code

    asked = [stop.case for stop in result.of(ASK)]
    assert "" not in asked, [stop.text for stop in result.of(ASK) if not stop.case]
    assert Counter(asked) == Counter(case.id for case in CASES if case.expect == ASK), asked
    assert len(asked) == 10
    # Предложения и отказы: каждый - случай каталога этого вида.
    for kind in (OFFER, REFUSE):
        for stop in result.of(kind):
            assert stop.case, stop
            assert next(case for case in CASES if case.id == stop.case).expect == kind, stop


def test_poligon_ochered_resheniy_po_katalogu(polygon, finance_db, monkeypatch) -> None:
    """Очередь «решить» (этап 3): вопросы оплат полигона - случаи каталога, по одному на случай.

    Оплаты холдинга за дочку (У7) - один вопрос на плательщика, а не по вопросу
    на каждую: человек отвечает один раз. Ответы - вариантами очереди (их
    команды и параметры, «другой договор» - с договором правды), и после них
    долги на 25.10 - правда до тенге, а очередь пуста.
    """
    import uuid

    from app.finance.contracts import service
    from app.finance.model import apply as apply_module
    from app.finance.model import questions
    from app.finance.model.context import system
    from app.finance.models import Operation

    world, files = polygon
    monkeypatch.setattr(questions, "today", lambda: whole.TODAY)
    monkeypatch.setattr(service, "today", lambda: whole.TODAY)
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        robot = whole.Robot(session, workspace, world, files)
        for name in whole.STEPS:
            if name != "payments":
                getattr(robot, name)()
        ctx = system(session, workspace)
        rows = questions.questions(ctx, {"params": {"kind": "payments"}})["rows"]

        cases: Counter[str] = Counter()
        for row in rows:
            operations = [session.get(Operation, uuid.UUID(item["id"])) for item in row["objects"] if item["type"] == "operation"]
            case = whole._payment_case(world, operations[0], (OFFER,) if row["suggested"] else (ASK,))
            cases[case] += 1
            if case == "У7":
                assert row["count"] > 1, "оплаты холдинга - одним вопросом"
                assert {whole._payment_case(world, item, (ASK,)) for item in operations} == {"У7"}
            if row["suggested"]:
                option = next(item for item in row["options"] if item["key"] == row["suggested"])
                extra = {}
            else:
                truth = whole._truth_contract(session, workspace, world, operations[0], robot.by_code)
                assert truth is not None, row["text"]
                option = next((item for item in row["options"] if item["key"] == f"link:{truth}"), None)
                extra = {}
                if option is None:
                    option = next(item for item in row["options"] if item["key"] == "pick")
                    extra = {"contract": str(truth)}
            assert option["open"]
            apply_module.apply(ctx, [{"command": item["command"], "params": {**item["params"], **extra}} for item in option["commands"]])
        assert cases == Counter({"У7": 1, "О4": 1, "О5": 1, "О11": 1, "О2": 1, "О6": 1}), cases
        assert questions.questions(ctx, {"params": {"kind": "payments"}})["rows"] == []

        robot.owed()
        for code, truth in world.owed().items():
            assert robot.result.owed.get(code, 0) == truth, code
