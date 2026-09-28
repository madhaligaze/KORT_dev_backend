"""Способ заполнения поля: закрытый список не заводит новое из опечатки.

На проде 26.09.2026 в статусах висело «им», в видах — «Взыскание», оба ни в
одном договоре: ячейка листа принимала любой текст и молча заводила из него
значение справочника. Лист показывал людей коротко
(«Наталья П.»), и та же ячейка, скопированная в соседнюю строку, заводила
нового сотрудника «Наталья П.». Набор держит поведение по «Настройкам
реестра» BBC: статус, ответственный, отдел и исполнитель — из списка; вид и
предмет — список или своё.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.finance.layout import norm
from app.finance import service as finance_service
from app.finance.contracts import service, setup
from app.finance.contracts.models import Contract, Employee, ListValue
from app.finance.db import finance_session

FULL = service.Access(view=True, edit=True, setup=True)
OWNER = service.Actor(uuid.uuid4(), "owner@test")


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    yield engine


@pytest.fixture
def space(finance_db):
    """BBC и Omar Development — наши; три сотрудника и отдел ЮО."""
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        setup.add_entity(session, workspace, name="BBC", code="BBC")
        setup.add_entity(session, workspace, name="Omar Development & Consulting")
        for index, name in enumerate(("Наталья Петровна", "Жанара", "Нурболат")):
            session.add(Employee(workspace_id=workspace.id, full_name=name, normalized_name=norm(name), position=index))
        gone = Employee(workspace_id=workspace.id, full_name="Ушедший Сотрудник", normalized_name=norm("Ушедший Сотрудник"))
        gone.archived_at = datetime.now(timezone.utc)
        session.add(gone)
        setup.upsert_department(session, workspace, {"code": "ЮО", "title": "Юр отдел"})
        session.flush()
        return workspace.id


def _ws(session, space_id):
    return finance_service.get_workspace(session, space_id)


def _make(session, space_id, **values):
    return service.create(session, _ws(session, space_id), FULL, OWNER, values)


def _patch(session, space_id, contract, **values):
    return service.patch(session, _ws(session, space_id), FULL, OWNER, contract.id, values, known_seq=None)


def _values(session, space_id, key):
    return {
        row.value
        for row in session.scalars(
            sa.select(ListValue).where(ListValue.workspace_id == space_id, ListValue.field_key == key)
        )
    }


def _people(session, contract):
    return [
        session.get(Employee, employee_id).full_name
        for employee_id in service.people_of(session, [contract.id]).get(contract.id, [])
    ]


def test_opechatka_v_statuse_ne_zavodit_novyy_status(space):
    with finance_session() as session:
        contract = _make(session, space, executor="BBC", customer="ТОО А", status="Действующий")
        before = _values(session, space, "status")
        with pytest.raises(service.NotInList, match="«им» нет в списке «Текущее состояние»"):
            _patch(session, space, contract, status="им")
        assert _values(session, space, "status") == before


def test_odnoznachnoe_nachalo_prinimaetsya_neskolko_kandidatov_net(space):
    with finance_session() as session:
        contract = _make(session, space, executor="BBC", customer="ТОО А")
        _patch(session, space, contract, status="дейст")
        registry = service.Registry(session, _ws(session, space))
        assert registry.values[contract.status_id].value == "Действующий"
        # «Н» — «На исполнении», «Недействующий», «Не состоялся»: наугад не выбираем.
        with pytest.raises(service.NotInList, match="подходит несколько"):
            _patch(session, space, contract, status="н")


def test_novaya_stroka_s_opechatkoy_zavoditsya_a_opechatka_vidna(space):
    with finance_session() as session:
        contract = _make(session, space, executor="BBC", customer="ТОО А", number="17-7", status="им")
        registry = service.Registry(session, _ws(session, space))
        issues = service.issues_of(contract, registry, service.NumberIndex(), {})
        assert contract.number == "17-7" and contract.status_id is None
        assert any(issue["code"] == "unread_status" and issue["text"] == "«им» нет в списке «Текущее состояние»" for issue in issues)
        assert "им" not in _values(session, space, "status")
        # Выбрали из списка — замечание уходит.
        _patch(session, space, contract, status="Действующий")
        issues = service.issues_of(contract, registry, service.NumberIndex(), {})
        assert not any(issue["code"] == "unread_status" for issue in issues)


def test_ispolnitel_tolko_nashe_yurlico_i_otkaz_do_voprosa_o_rezhime(space):
    with finance_session() as session:
        contract = _make(session, space, executor="BBC", customer="ТОО А")
        contract.created_at = datetime.now(timezone.utc) - timedelta(days=3)
        session.flush()
        # Договор не сегодняшний: смена исполнителя спросила бы «опечатка или с
        # даты». Не наше юрлицо — отказ сразу, без вопроса.
        with pytest.raises(service.NotInList, match="не наше юрлицо"):
            _patch(session, space, contract, executor="ТОО Левый")
        # Наше юрлицо по началу имени — дальше обычный вопрос о режиме.
        with pytest.raises(service.ModeRequired):
            _patch(session, space, contract, executor="Omar")
    with finance_session() as session:
        workspace = _ws(session, space)
        fresh = _make(session, space, executor="omar", customer="ТОО Б")
        registry = service.Registry(session, workspace)
        assert registry.parties[fresh.executor_id].name == "Omar Development & Consulting"


def test_pokupka_ispolnitel_chuzhoy_esli_zakazchik_nash(space):
    # «Заказчик ГК / КУПЛЯ-ПРОДАЖА» в файле BBC: BBCA покупает у «Халык Актив».
    # Наша сторона здесь — заказчик; исполнитель чужой законно.
    with finance_session() as session:
        workspace = _ws(session, space)
        purchase = _make(session, space, executor="Халык Актив", customer="BBC")
        registry = service.Registry(session, workspace)
        assert registry.parties[purchase.executor_id].name == "Халык Актив"
        assert not any(
            issue["code"] == "unread_executor"
            for issue in service.issues_of(purchase, registry, service.NumberIndex(), {})
        )
        _patch(session, space, purchase, executor="ТОО Другой поставщик")
        # Сменили заказчика на чужого вместе с исполнителем — нашей стороны нет.
        with pytest.raises(service.NotInList, match="не наше юрлицо"):
            _patch(session, space, purchase, executor="ТОО Третий", customer="ТОО Четвёртый")


def test_ispolnitel_s_opechatkoy_v_novoy_stroke(space):
    with finance_session() as session:
        contract = _make(session, space, executor="ТОО Левый", customer="ТОО А")
        registry = service.Registry(session, _ws(session, space))
        issues = service.issues_of(contract, registry, service.NumberIndex(), {})
        assert contract.executor_id is None
        assert any(issue["code"] == "unread_executor" and "не наше юрлицо" in issue["text"] for issue in issues)


def test_otvetstvennyy_po_korotkomu_imeni_bez_dvoynika(space):
    with finance_session() as session:
        contract = _make(session, space, executor="BBC", customer="ТОО А", people="Наталья П., Жанара")
        assert _people(session, contract) == ["Наталья Петровна", "Жанара"]
        count = session.scalar(sa.select(sa.func.count()).select_from(Employee).where(Employee.workspace_id == space))
        with pytest.raises(service.NotInList, match="нет среди сотрудников"):
            _patch(session, space, contract, people="Асхат")
        with pytest.raises(service.NotInList, match="в корзине"):
            _patch(session, space, contract, people="Ушедший Сотрудник")
        assert session.scalar(sa.select(sa.func.count()).select_from(Employee).where(Employee.workspace_id == space)) == count


def test_otvetstvennyy_v_otkrytom_spiske_zavoditsya_no_ne_dubliruetsya(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        setup.update_field(session, workspace, "people", {"fill": "hint"})
        contract = _make(session, space, executor="BBC", customer="ТОО А", people="Асхат, Наталья П.")
        assert _people(session, contract) == ["Асхат", "Наталья Петровна"]
        names = [row.full_name for row in session.scalars(sa.select(Employee).where(Employee.workspace_id == space))]
        assert names.count("Наталья Петровна") == 1 and "Наталья П." not in names


def test_otdel_iz_spiska(space):
    with finance_session() as session:
        contract = _make(session, space, executor="BBC", customer="ТОО А", department="юо")
        assert contract.department_id is not None
        with pytest.raises(service.NotInList, match="Отдела «ЮОО» нет в списке"):
            _patch(session, space, contract, department="ЮОО")


def test_vid_spisok_ili_svoe(space):
    with finance_session() as session:
        contract = _make(session, space, executor="BBC", customer="ТОО А", type="Консалтинг")
        assert "Консалтинг" in _values(session, space, "type")
        assert contract.type_id is not None


def test_zagruzka_fayla_ne_ogranichena(space):
    # У файла свой протокол: незнакомый статус — как написан, смысл ему
    # назначает человек. `_set_field` без `strict` — дорога загрузки.
    with finance_session() as session:
        workspace = _ws(session, space)
        registry = service.Registry(session, workspace)
        scratch = Contract(attrs={}, provenance={}, file_snapshot={})
        service._set_field(scratch, "status", "нужно закрыть по бух", registry, people_out={})
        assert "нужно закрыть по бух" in _values(session, space, "status")


def test_nastroyka_sposoba_i_skhema(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        fields = {item["key"]: item for item in setup.schema(session, workspace, FULL)["fields"]}
        assert (fields["status"]["fill"], fields["people"]["fill"], fields["executor"]["fill"]) == ("list", "list", "own")
        assert (fields["type"]["fill"], fields["subject"]["fill"], fields["customer"]["fill"]) == ("hint", "hint", "hint")
        assert fields["executor"]["fills"] == ["hint", "own"]
        assert "fill" not in fields["number"]
        with pytest.raises(finance_service.FinanceError, match="выбора из списка у этого типа нет"):
            setup.update_field(session, workspace, "number", {"fill": "list"})
        with pytest.raises(finance_service.FinanceError, match="Такого способа"):
            setup.update_field(session, workspace, "status", {"fill": "own"})
        setup.update_field(session, workspace, "status", {"fill": "hint"})
        contract = _make(session, space, executor="BBC", customer="ТОО А", status="на согласовании")
        assert contract.status_id is not None and "на согласовании" in _values(session, space, "status")


def test_obyazatelnoe_pole_daet_zamechanie(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        empty = _make(session, space, executor="BBC", customer="ТОО А")
        seq_before = empty.seq
        setup.update_field(session, workspace, "number", {"required": True})
        setup.update_field(session, workspace, "people", {"required": True})
        session.refresh(empty)
        assert empty.seq > seq_before  # договоры перечитаются опросом
        registry = service.Registry(session, workspace)
        codes = {issue["code"]: issue["text"] for issue in service.issues_of(empty, registry, service.NumberIndex(), {}, [])}
        assert codes["required_number"] == "Не заполнено: «№ Договора»"
        assert "required_people" in codes
        # Исполнитель пустым уже назван своим замечанием — второй раз не говорим.
        setup.update_field(session, workspace, "executor", {"required": True})
        bare = _make(session, space, customer="ТОО Б", number="1")
        codes = {issue["code"] for issue in service.issues_of(bare, registry, service.NumberIndex(), {}, [])}
        assert "no_executor" in codes and "required_executor" not in codes and "required_number" not in codes
