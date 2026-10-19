"""Реестр договоров: одна запись на все листы, сторона и сумма не меняются молча.

Набор проверяет то, что в реестре BBC ломалось руками: копии листов
разъезжались с «Сводной», перевод клиента на другое ТОО стирал историю, одна
и та же компания жила тремя написаниями, «20% по разовым» могло стать числом.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.finance import service as finance_service
from app.finance.contracts import service, setup
from app.finance.contracts.models import Contract, ContractAmendment
from app.finance.db import finance_session
from app.finance.service import FinanceError

FULL = service.Access(view=True, edit=True, setup=True)
OWNER = service.Actor(uuid.uuid4(), "owner@test")
OTHER = service.Actor(uuid.uuid4(), "lawyer@test")


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
    """Компания с двумя нашими ТОО - BBC и BBCA."""
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        setup.add_entity(session, workspace, name="BBC", code="BBC", full_name='ТОО "Big Business Consulting"')
        setup.add_entity(session, workspace, name="BBCA", code="BBCA")
        return workspace.id


def _ws(session, space_id):
    return finance_service.get_workspace(session, space_id)


def _make(session, space_id, actor=OWNER, **values):
    workspace = _ws(session, space_id)
    return service.create(session, workspace, FULL, actor, values)


def _age(session, contract_id, days=3):
    """Договор заведён не сегодня - правка стороны и суммы спросит режим."""
    contract = session.get(Contract, contract_id)
    contract.created_at = datetime.now(timezone.utc) - timedelta(days=days)
    session.flush()


def test_zasev_obshiy_i_povtornyy(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        first = setup.schema(session, workspace, FULL)
        second = setup.schema(session, workspace, FULL)
    keys = [item["key"] for item in first["fields"]]
    assert {"executor", "customer", "number", "amount", "folder_url", "economic_role"} <= set(keys)
    assert [item["value"] for item in first["lists"]["status"]][:2] == ["Действующий", "На исполнении"]
    assert first["views"][0]["key"] == "main"
    assert first["schema_rev"] == second["schema_rev"]
    assert {entity["code"] for entity in first["own_entities"]} == {"BBC", "BBCA"}


def test_storony_po_kavychkam_i_registru_odin_kontragent(space):
    with finance_session() as session:
        a = _make(session, space, executor="BBC", customer="ТОО «Атриум плюс»", number="№60-BBC-CONSULT")
        b = _make(session, space, executor="bbc", customer='ТОО "АТРИУМ ПЛЮС"', number="№61")
        assert a.customer_id == b.customer_id
        assert a.executor_id == b.executor_id
        # Без организационной формы - другое лицо: сводит только человек.
        c = _make(session, space, executor="BBC", customer="Атриум плюс")
        assert c.customer_id != a.customer_id


def test_napravlenie_i_smysl_po_storonam(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        sale = _make(session, space, executor="BBC", customer="ТОО Альфа", type="Абонентское обслуживание")
        purchase = _make(session, space, executor="ТОО Халык Актив", customer="BBCA", type="Купля-продажа")
        inside = _make(session, space, executor="BBC", customer="BBCA", subject="Агентский")
        loan = _make(session, space, executor="BBC", customer="ТОО Бета", subject="Финансовая помощь")
        registry = service.Registry(session, workspace)
        system = lambda c: registry.meaning(c.economic_role_id).get("system")  # noqa: E731
        assert (system(sale), sale.billing) == ("revenue", "month")
        assert system(purchase) == "expense"
        assert system(inside) == "intra_group"
        assert inside.billing == "terms"
        assert system(loan) == "financing"
        assert loan.provenance["economic_role"] == "subject"
        assert registry.roles_of(loan) == {"executor": "Займодавец", "customer": "Займополучатель"}


def test_protsent_tekstom_ne_stanovitsya_chislom(space):
    with finance_session() as session:
        agent = _make(session, space, executor="BBC", customer="ТОО Гамма", subject="Агентский",
                      amount="20% - по разовым,\n40% - по абон.")
        plain = _make(session, space, executor="BBC", customer="ТОО Гамма", amount="1\xa0200\xa0000")
        odd = _make(session, space, executor="BBC", customer="ТОО Гамма",
                    type="Абонентское обслуживание", amount="Урегулированы п.4 Приложение №1")
        workspace = _ws(session, space)
        registry = service.Registry(session, workspace)
        issues = service.issues_of(odd, registry, service.NumberIndex(), {})
        assert agent.amount is None and agent.amount_terms.startswith("20%")
        assert agent.billing == "terms"
        assert plain.amount == Decimal("1200000")
        assert "amount_unclear" in {issue["code"] for issue in issues}


def test_storona_i_summa_ne_menyayutsya_molcha(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        contract = _make(session, space, executor="BBCA", customer="ИП Сексенбаев", amount="200000")
        _age(session, contract.id)
        with pytest.raises(service.ModeRequired) as caught:
            service.patch(session, workspace, FULL, OTHER, contract.id, {"executor": "BBC"}, known_seq=None)
        assert caught.value.fields == ["executor"]
        # Тот же исполнитель другим написанием - не изменение, вопроса нет.
        service.patch(session, workspace, FULL, OTHER, contract.id, {"executor": "bbca"}, known_seq=None)
        # Правка примечания режима не требует.
        service.patch(session, workspace, FULL, OTHER, contract.id, {"note": "перекидка"}, known_seq=None)
        assert contract.note == "перекидка"


def test_zamena_lits_s_daty_hranit_istoriyu(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        contract = _make(session, space, executor="BBCA", customer="ИП Сексенбаев", amount="200000")
        _age(session, contract.id)
        old_executor = contract.executor_id
        mode = service.Mode.parse({"kind": "from_date", "effective_from": "2026-06-12", "number": "б/н"})
        service.patch(session, workspace, FULL, OTHER, contract.id, {"executor": "BBC"}, known_seq=None, mode=mode)
        amendment = session.query(ContractAmendment).filter_by(contract_id=contract.id).one()
        assert contract.executor_id != old_executor
        assert amendment.before == {"executor": str(old_executor)}
        assert amendment.applied_at is not None


def test_izmenenie_summy_v_budushchem_vperedi(space, monkeypatch):
    with finance_session() as session:
        workspace = _ws(session, space)
        contract = _make(session, space, executor="BBC", customer="ТОО Альфа", amount="500000")
        _age(session, contract.id)
        ahead = (service.today() + timedelta(days=10)).isoformat()
        mode = service.Mode.parse({"kind": "from_date", "effective_from": ahead})
        service.patch(session, workspace, FULL, OTHER, contract.id, {"amount": "750 000"}, known_seq=None, mode=mode)
        assert contract.amount == Decimal("500000")
        seq_before = contract.seq
        contract_id = contract.id

    monkeypatch.setattr(service, "today", lambda: date.today() + timedelta(days=11))
    with finance_session() as session:
        assert service.apply_due(session) == 1
    with finance_session() as session:
        contract = session.get(Contract, contract_id)
        assert contract.amount == Decimal("750000")
        assert contract.seq > seq_before
        assert service.apply_due(session) == 0


def test_opechatka_menyaet_srazu(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        contract = _make(session, space, executor="BBC", customer="ТОО Альфа", amount="500000")
        _age(session, contract.id)
        mode = service.Mode.parse({"kind": "fix"})
        service.patch(session, workspace, FULL, OTHER, contract.id, {"amount": "50000"}, known_seq=None, mode=mode)
        assert contract.amount == Decimal("50000")
        assert session.query(ContractAmendment).count() == 0


def test_konflikt_po_polyu_a_ne_po_zapisi(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        contract = _make(session, space, executor="BBC", customer="ТОО Альфа", note="a")
        seen = contract.seq
        service.patch(session, workspace, FULL, OTHER, contract.id, {"note": "b"}, known_seq=seen)
        # Другое поле с тем же прочитанным номером - не конфликт.
        service.patch(session, workspace, FULL, OWNER, contract.id, {"folder_url": "https://x"}, known_seq=seen)
        with pytest.raises(service.FieldConflict) as caught:
            service.patch(session, workspace, FULL, OWNER, contract.id, {"note": "c"}, known_seq=seen)
        assert caught.value.conflicts == ["note"]


def test_nomer_u_drugogo_kontragenta_i_tak_i_dolzhno_byt(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        first = _make(session, space, executor="BBC", customer="ТОО Nekatrade", number="№ЮО-59")
        second = _make(session, space, executor="BBC", customer="ТОО Номад и Ко", number="№ ЮО-59")
        listing = service.list_all(session, workspace, FULL)
        by_id = {item["id"]: item for item in listing["contracts"]}
        codes = {issue["code"] for issue in by_id[str(second.id)]["issues"]}
        assert "number_taken" in codes
        service.acknowledge(session, workspace, FULL, OWNER, second.id, "number_taken", on=True)
        listing = service.list_all(session, workspace, FULL)
        issue = next(i for i in {x["id"]: x for x in listing["contracts"]}[str(second.id)]["issues"]
                     if i["code"] == "number_taken")
        assert issue["acknowledged"] is True
        # Сменили номер на другой занятый - отметка к нему не относится.
        _make(session, space, executor="BBC", customer="ТОО Третий", number="№ЮО-60")
        service.patch(session, workspace, FULL, OWNER, second.id, {"number": "ЮО-60"}, known_seq=None)
        listing = service.list_all(session, workspace, FULL)
        issue = next(i for i in {x["id"]: x for x in listing["contracts"]}[str(second.id)]["issues"]
                     if i["code"] == "number_taken")
        assert issue["acknowledged"] is False
        assert first.number == "№ЮО-59"


def test_list_po_predmetu_a_ne_po_vidu(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        agent = _make(session, space, executor="BBC Marketing", customer="ТОО Альфа", type="Иное", subject="Агентский")
        rent = _make(session, space, executor="BBCA", customer="ТОО Бета", type="Аренда")
        service_contract = _make(session, space, executor="BBC", customer="ТОО Гамма", type="Абонентское обслуживание")
        registry = service.Registry(session, workspace)
        agent_subject = registry.resolve_value("subject", "Агентский", create=False)
        rent_type = registry.resolve_value("type", "Аренда", create=False)
        subscription = registry.resolve_value("type", "Абонентское обслуживание", create=False)
        setup.upsert_view(session, workspace, {
            "title": "Прочие договоры",
            "blocks": [
                {"title": "АГЕНТСКИЙ ДОГОВОР",
                 "filter": {"any": [{"all": [{"field": "subject", "op": "in", "value": [str(agent_subject.id)]}]}]}},
                {"title": "АРЕНДА",
                 "filter": {"any": [{"all": [{"field": "type", "op": "in", "value": [str(rent_type.id)]}]}]},
                 "defaults": {"type": str(rent_type.id)}},
            ],
        })
        setup.upsert_view(session, workspace, {
            "title": "Исполнитель ГК",
            "blocks": [{"title": "", "filter": {"any": [{"all": [
                {"field": "type", "op": "in", "value": [str(subscription.id)]},
                {"field": "executor_is_own", "op": "is", "value": True},
            ]}]}}],
        })
        listing = service.list_all(session, workspace, FULL)
        views = {item["id"]: {(v["view"], v["block"]) for v in item["views"]} for item in listing["contracts"]}
        prochie = next(v for v in service.Registry(session, workspace).views if v.title == "Прочие договоры").key
        executor_gk = next(v for v in service.Registry(session, workspace).views if v.title == "Исполнитель ГК").key
        assert (prochie, 0) in views[str(agent.id)]
        assert (prochie, 1) in views[str(rent.id)]
        assert (executor_gk, 0) in views[str(service_contract.id)]
        # Агентский без нашего исполнителя «Исполнитель ГК» не ловит - вид «Иное».
        assert all(view != executor_gk for view, _ in views[str(agent.id)])
        assert all(("main", 0) in item for item in views.values())


def test_karman_bloka_stavit_podstanovki(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        registry = service.Registry(session, workspace)
        rent_type = registry.resolve_value("type", "Аренда", create=False)
        view = setup.upsert_view(session, workspace, {
            "title": "Прочие",
            "blocks": [{"title": "АРЕНДА", "defaults": {"type": str(rent_type.id)},
                        "filter": {"any": [{"all": [{"field": "type", "op": "in", "value": [str(rent_type.id)]}]}]}}],
        })
        contract = service.create(session, workspace, FULL, OWNER,
                                  {"executor": "BBCA", "customer": "ТОО Эмбебап"}, view_key=view.key, block=0)
        registry = service.Registry(session, workspace)
        assert contract.type_id == rent_type.id
        assert contract.billing == "month"
        assert registry.meaning(contract.economic_role_id)["system"] == "revenue"
        assert registry.roles_of(contract)["executor"] == "Арендодатель"


def test_smysl_daty_okonchaniya_ne_skhlopyvaetsya(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        terminated = _make(session, space, executor="BBC", customer="ТОО А", status="недействующий",
                           type="Абонентское обслуживание", end_date="12.05.2025")
        fulfilled = _make(session, space, executor="BBC", customer="ТОО Б", status="исполнен",
                          type="Разовая услуга", end_date="01.04.2025")
        unclear = _make(session, space, executor="BBC", customer="ТОО В", status="действующий",
                        type="Абонентское обслуживание", end_date="01.04.2025")
        # Статус - закрытый список: своё значение сначала добавляют в настройке,
        # и без смысла оно горит замечанием.
        setup.add_value(session, workspace, "status", "нужно закрыть по бух")
        odd = _make(session, space, executor="BBC", customer="ТОО Г", status="нужно закрыть по бух")
        registry = service.Registry(session, workspace)
        codes = {issue["code"] for issue in service.issues_of(odd, registry, service.NumberIndex(), {})}
        assert (terminated.end_kind, fulfilled.end_kind, unclear.end_kind) == ("terminated", "fulfilled", "unknown")
        assert "status_unknown" in codes


def test_izmeneniya_posle_nomera(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        first = _make(session, space, executor="BBC", customer="ТОО А")
        mark = service.list_all(session, workspace, FULL)["seq"]
        second = _make(session, space, executor="BBC", customer="ТОО Новый клиент")
        batch = service.changes(session, workspace, FULL, mark)
        assert [item["id"] for item in batch["contracts"]] == [str(second.id)]
        assert batch["parties"][str(second.customer_id)]["name"] == "ТОО Новый клиент"
        service.remove(session, workspace, FULL, OWNER, first.id)
        batch = service.changes(session, workspace, FULL, batch["seq"])
        assert batch["removed"] == [str(first.id)]


def test_ne_otkryty_polya_ne_otdayutsya_i_ne_pravyatsya(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        contract = _make(session, space, executor="BBC", customer="ТОО А", amount="100000")
        lawyer = service.Access(view=True, edit=True, hidden=frozenset({"paid_snapshot", "amount"}))
        listing = service.list_all(session, workspace, lawyer)
        assert "amount" not in listing["contracts"][0]["values"]
        with pytest.raises(PermissionError):
            service.patch(session, workspace, lawyer, OWNER, contract.id, {"amount": "1"}, known_seq=None)
        with pytest.raises(FinanceError):
            service.patch(session, workspace, FULL, OWNER, contract.id, {"paid_snapshot": "1"}, known_seq=None)


def test_udalenie_ne_uvodit_ispolzuemoe_molcha(space):
    """Удаление (в корзину) не отнимает смысл у договоров молча.

    Юрлицо с договорами удаляется (27.09.2026: ошибочное «ИП WE make» с одним
    договором не удалялось никак): договор остаётся, юрлицо перестаёт быть
    «нашим», и это видно замечанием, а из корзины оно возвращается. Статус,
    стоящий в договорах, не удаляется - подпись стала бы идентификатором.
    «Выручка» - системный смысл: без неё продажи выпали бы из порога НДС.
    """
    from app.finance import trash

    with finance_session() as session:
        workspace = _ws(session, space)
        contract = _make(session, space, executor="BBC", customer="ТОО «Бета»", status="Действующий")
        schema = setup.schema(session, workspace, FULL)
        bbc = next(item for item in schema["own_entities"] if item["code"] == "BBC")
        bbca = next(item for item in schema["own_entities"] if item["code"] == "BBCA")

        setup.update_entity(session, workspace, uuid.UUID(bbc["id"]), {"archived": True})
        registry = service.Registry(session, workspace)
        assert not registry.is_own(uuid.UUID(bbc["id"]))
        codes = {issue["code"] for issue in service.one(session, workspace, FULL, contract)["contract"]["issues"]}
        assert "no_own_party" in codes
        trash.restore(session, workspace, "entity", uuid.UUID(bbc["id"]))
        assert service.Registry(session, workspace).is_own(uuid.UUID(bbc["id"]))

        setup.update_entity(session, workspace, uuid.UUID(bbca["id"]), {"archived": True})
        after = setup.schema(session, workspace, FULL)
        assert {item["code"] for item in after["own_entities"]} == {"BBC"}
        assert not service.Registry(session, workspace).is_own(uuid.UUID(bbca["id"]))

        status_id = uuid.UUID(str(contract.status_id))
        with pytest.raises(FinanceError, match="сведите"):
            setup.update_value(session, workspace, status_id, {"archived": True})

        revenue = next(v for v in schema["lists"]["economic_role"] if v["meaning"].get("system") == "revenue")
        with pytest.raises(FinanceError, match="системный смысл"):
            setup.update_value(session, workspace, uuid.UUID(revenue["id"]), {"archived": True})
        with pytest.raises(FinanceError, match="системный смысл"):
            setup.update_value(
                session, workspace, uuid.UUID(revenue["id"]), {"meaning": {"system": "expense"}}
            )

        unused = setup.add_value(session, workspace, "status", "Временный")
        setup.update_value(session, workspace, unused.id, {"archived": True})
        final = setup.schema(session, workspace, FULL)
        assert "Временный" not in [v["value"] for v in final["lists"]["status"]]
        assert "Временный" in [v["value"] for v in final["archived_values"]["status"]]
