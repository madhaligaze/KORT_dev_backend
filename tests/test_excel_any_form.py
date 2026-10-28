"""Excel любой формы: таблицы отделов «Тумар» складываются в одну модель без кода под них.

Полигон «Тумар» (`tests/polygon`): у ИП Сейткали нет 1С - договоры только в
таблице слушателей учебного центра, оплаты - выписки Kaspi. Таблицу KORT сам
не понимает («не понимаю лист»), и человек размечает её один раз: строка -
договор, колонки, чьи договоры, как начисляются. Дальше KORT спрашивает
только по каталогу: порядок частей даты (Ф3), курс, оплаченный мамой (О11);
ИИН слушателя из выписки - предложение (У9). Долг ИП на 25.10 обязан совпасть
с правдой.

Таблица аренды «Тумар Офис» - второй источник после 1С: она сверяется, а не
заводит заново. Вопрос - только где она спорит с 1С: долг на 01.06 у
«Каспий Трейд» (С1). Октябрь с двумя «Сумма» не читается (Ф5), сентября нет
(Ф7), старое имя арендатора - написание участника (У6).
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from polygon import sources
from polygon.world import TODAY, build

from app.finance import importing
from app.finance import service as finance_service
from app.finance.config import finance_settings
from app.finance.contracts import importer, service
from app.finance.contracts import payments as contract_payments
from app.finance.contracts.models import Contract, CounterpartyName, GroupEntity
from app.finance.core import excel, participants
from app.finance.core import settings as core_settings
from app.finance.core.debts import Debts
from app.finance.core.payments import Money
from app.finance.db import finance_session
from app.finance.models import Counterparty, Operation

FULL = service.Access(view=True, edit=True, setup=True)
ACTOR = service.Actor(None, "robot@polygon")
SCHOOL = "Excel/Учебный центр 2026.xlsx"
RENT = "Excel/Аренда - начисления 2026.xlsx"


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


def _registry(session, workspace, files) -> dict[str, uuid.UUID]:
    """Реестр офис-менеджера - тем же разбором, что в «Первых шагах»; человек отвечает только на вопросы."""
    batch = importer.start(session, workspace, ACTOR, files["Excel/Реестр договоров 2026.xlsx"], "Реестр договоров 2026.xlsx")
    sections = {section["key"]: section for section in batch.report["sections"]}
    own = [item["key"] for item in sections["entities"]["items"] if item["own"]]
    rules = {item["block"]: {"action": "accept"} for item in sections["rules"]["items"]}
    batch = importer.decide(session, workspace, batch.id, {"hidden": "skip", "entities": {"confirmed": True, "own": own}, "rules": rules})
    importer.apply(session, workspace, FULL, ACTOR, batch.id)
    return {
        name: party_id
        for party_id, name in session.execute(
            sa.select(Counterparty.id, Counterparty.name).join(GroupEntity, GroupEntity.counterparty_id == Counterparty.id)
        )
    }


def _school_answers(ip: uuid.UUID) -> dict:
    """Разметка таблицы слушателей - ответ человека один раз на источник."""
    return {
        "sheets": {
            "Слушатели": {
                "form": "contract",
                "columns": {
                    "ФИО / Организация": "customer", "ИИН/БИН": "party_bin", "Курс": "subject", "Начало": "billing_from",
                    "Стоимость": "amount", "Остаток": "remaining_snapshot",
                    "Оплата 1": "pay_sum", "Оплата 2": "pay_sum", "Оплата 3": "pay_sum",
                    "Дата 1": "pay_date", "Дата 2": "pay_date", "Дата 3": "pay_date",
                },
                "executor": str(ip), "billing": "total", "department": "Учебный центр",
            },
            "Долги на 01.06": {"form": "balance", "columns": {"ФИО / Организация": "account", "Курс": "contract"}, "executor": str(ip)},
        },
        "questions": {},
    }


def _statements(session, workspace, world, files, key: str, entity_id: uuid.UUID) -> None:
    entity = world.entities[key]
    finance_service.create_account(session, workspace, name=f"Kaspi {entity.short}", number=entity.iban, group_entity_id=entity_id)
    names = [account.name for account in finance_service.list_accounts(session, workspace.id)]
    numbers = finance_service.account_numbers(session, workspace.id)
    for name, data in sorted(files.items()):
        if name.startswith("Банк/") and entity.short in name:
            preview = importing.analyze(data, Path(name).name, names, account_numbers=numbers)
            batch = finance_service.save_preview(session, workspace, preview)
            finance_service.apply_batch(session, workspace, batch.id, actor="robot")
    session.flush()


def test_tablica_slushateley_ne_ponyatna_bez_razmetki(polygon, finance_db) -> None:
    _world, files = polygon
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        described = excel.describe(session, workspace, files[SCHOOL], "Учебный центр 2026.xlsx")
        assert [sheet["form"] for sheet in described["sheets"]] == [None, None], "не угадывает"
        proposed = excel.plan(session, workspace, files[SCHOOL], "Учебный центр 2026.xlsx", {}, today=TODAY)
        assert [item["kind"] for sheet in proposed["sheets"] for item in sheet["setup"]] == ["form", "form"]
        # «Лист не нужен» - разметка без формы: лист больше не спрашивает, что значит строка.
        skipped = excel.plan(session, workspace, files[SCHOOL], "Учебный центр 2026.xlsx",
                             {"sheets": {"Долги на 01.06": {"skip": True}}}, today=TODAY)
        assert [sheet["sheet"] for sheet in skipped["sheets"]] == ["Слушатели"]


def test_ip_seytkali_iz_tablicy_i_vypisok_vidit_nam_dolzhny(polygon, finance_db) -> None:
    world, files = polygon
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        entities = _registry(session, workspace, files)
        ip = entities["ИП Сейткали"]
        # Ф2: «1,5 млн» в реестре - не число; человек называет сумму.
        course = session.scalar(sa.select(Contract).where(Contract.executor_id == ip))
        assert course.amount is None and course.amount_terms == "1,5 млн"
        service.patch(session, workspace, FULL, ACTOR, course.id, {"amount": "1500000"}, known_seq=None, mode=service.Mode(kind="fix"))

        data, name = files[SCHOOL], "Учебный центр 2026.xlsx"
        answers = _school_answers(ip)
        proposed = excel.plan(session, workspace, data, name, answers, today=TODAY)
        sheets = {sheet["sheet"]: sheet for sheet in proposed["sheets"]}
        listeners = sheets["Слушатели"]
        assert listeners["setup"] == [] and listeners["rows"] == 15
        # Ф3 - единственный вопрос: все дни не больше 12, порядок частей даты по данным не вывести.
        assert [question["kind"] for question in listeners["questions"]] == ["date_order"]
        # Курс «Самал Клиник» из реестра найден, а не заведён вторым; остальные 14 - новые.
        assert (listeners["contracts"]["new"], listeners["contracts"]["fill"]) == (14, 1)
        assert sheets["Долги на 01.06"]["openings"]["as_of"] == "2026-06-01"

        answers["sheets"]["Слушатели"]["date_order"] = "dmy"
        result = excel.apply(session, workspace, FULL, ACTOR, data, name, answers, today=TODAY)
        assert (result["created"], result["filled"], result["blocked"]) == (14, 1, [])
        assert core_settings.payments(session, workspace).since == date(2026, 6, 1), "долги на 01.06 - начало учёта"
        # Разметка запомнена: тот же файл ещё раз узнаётся без вопроса и не двоит договоры.
        again = excel.describe(session, workspace, data, name)
        assert [sheet["form"] for sheet in again["sheets"]] == ["contract", "balance"]
        repeat = excel.plan(session, workspace, data, name, {}, today=TODAY)
        assert {sheet["sheet"]: sheet["contracts"]["new"] for sheet in repeat["sheets"]} == {"Слушатели": 0, "Долги на 01.06": 0}

        _statements(session, workspace, world, files, "ИП", ip)
        # У9: «КАСЫМОВ Е.» с ИИН в выписке - «Касымов Ерлан» из таблицы, предложение (человек принимает).
        proposals = [item for item in participants.bin_proposals(session, workspace, FULL) if item["clear"]]
        assert [(item["name"], item["numbers"][0]["bin"]) for item in proposals] == [("Касымов Ерлан", world.parties["kasym"].number)]
        participants.set_bin(session, workspace, FULL, ACTOR, uuid.UUID(proposals[0]["party_id"]), proposals[0]["numbers"][0]["bin"])
        session.flush()
        # О11: мама платит за курс дочери - в «Оплатах без договора», человек относит к курсу.
        money = Money(session, workspace, today=TODAY)
        lonely = [item for item in money.without_contract() if item.get("direction", "in") == "in"]
        assert [item["comment"] for item in lonely] == ["Перевод с карты. За курс дочери"]
        daughter = session.scalar(
            sa.select(Contract).join(Counterparty, Counterparty.id == Contract.customer_id).where(Counterparty.name == "Иванова Мария")
        )
        operation = session.scalar(sa.select(Operation).where(Operation.comment == "Перевод с карты. За курс дочери"))
        contract_payments.decide(session, workspace, FULL, ACTOR, daughter.id, operation.id, "link")
        session.flush()

        debts = Debts(session, workspace, FULL, day=TODAY)
        got = sum((item.owed_to(ip) or Decimal(0) for item in debts.items.values()), Decimal(0))
        assert got == world.owed()["ИП"] == Decimal(900000)


def test_tablica_arendy_sveryaetsya_s_1s_i_sprashivaet_tolko_spornoe(polygon, finance_db) -> None:
    from test_onec_files import _answers, _upload

    from app.finance.core import onec_start
    from app.finance.onec import files as onec_files
    from app.finance.onec.models import OnecBase

    world, files = polygon
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        upload = _upload(files)
        onec_files.apply(session, workspace, upload, actor="robot")
        bases = {base.organization_name: base for base in session.scalars(sa.select(OnecBase))}
        for name in ('ТОО "Тумар Офис"', 'ТОО "Тумар Плюс"'):
            onec_start.add_entity(session, workspace, FULL, ACTOR, bases[name].id)
        for name in ('ТОО "Тумар Офис"', 'ТОО "Тумар Плюс"'):
            base = bases[name]
            onec_start.add_participants(session, workspace, FULL, ACTOR, base.id, today=TODAY)
            plan = onec_start.plan(session, workspace, FULL, base.id, today=TODAY, osv=upload.osv)
            onec_start.add_contracts(session, workspace, FULL, ACTOR, base.id, today=TODAY, answers=_answers(world, plan))
        onec_start.start_since(session, workspace, FULL, ACTOR, bases['ТОО "Тумар Офис"'].id, upload.osv)
        session.flush()
        before = {
            contract.id: contract.amount for contract in session.scalars(sa.select(Contract).where(Contract.deleted_at.is_(None)))
        }

        data, name = files[RENT], "Аренда - начисления 2026.xlsx"
        proposed = excel.plan(session, workspace, data, name, {}, today=TODAY)
        by_form = {sheet["sheet"]: sheet for sheet in proposed["sheets"]}
        # Ф5: в «Октябре» две колонки «Сумма» - лист не читается.
        assert [refusal["kind"] for refusal in by_form["Октябрь"]["refusals"]] == ["duplicate"]
        months = by_form["Июнь, Июль, Август"]
        assert months["form"] == "contract_month"
        # Ф7: сентября нет - строка «нет в файле», не вопрос.
        assert [item[:7] for item in months["missing"]] == ["09.2026"]
        # С1 - единственный вопрос: долг «Каспий Трейд» на 01.06 по ОСВ и по таблице разный.
        assert [question["kind"] for question in months["questions"]] == ["opening"], months["questions"]
        disputes = [item for item in months["openings"]["items"] if item["state"] != "same"]
        assert [(item["number"], item["state"]) for item in disputes] == [("А-01", "dispute")]
        kaspiy = disputes[0]
        assert Decimal(kaspiy["amount"]) == world.contracts["kaspiy"].amount_on(date(2026, 6, 1))
        assert Decimal(kaspiy["kort"]) == world.opening("kaspiy")
        # Ф8: «съехал 30.06» у А-03 - KORT уже знает конец из ответа в «Из 1С»; У6 - старое имя арендатора.
        assert [(item["number"], item["state"]) for item in months["ends"]] == [("А-03", "same")]
        assert [item["name"] for item in months["contracts"]["items"] if item["action"] == "alias"] == ['ТОО "Бетон Строй"']
        # Начисления листов совпадают с KORT: аренда по счетам и актам 1С.
        assert [item for item in months["compare"] if item["kind"] in ("accrual", "unknown")] == []

        answers = {"questions": {f"opening:{kaspiy['contract_id']}": "keep"}}
        result = excel.apply(session, workspace, FULL, ACTOR, data, name, answers, today=TODAY)
        assert (result["created"], result["openings"], result["aliases"], result["blocked"]) == (0, 0, 1, ["Октябрь"])
        after = {
            contract.id: contract.amount for contract in session.scalars(sa.select(Contract).where(Contract.deleted_at.is_(None)))
        }
        assert after == before, "сверка ничего не завела и не переписала"
        assert session.scalar(sa.select(sa.func.count()).select_from(CounterpartyName).where(CounterpartyName.name == 'ТОО "Бетон Строй"')) == 1


def _sheet(rows: list[list]) -> bytes:
    import io

    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "Клиенты"
    sheet.append(["Клиент", "БИН/ИИН", "Стоимость", "Начало"])
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_oshibka_bin_ne_derzhit_list_a_spornyy_uchastnik_po_otvetu(finance_db) -> None:
    from polygon.world import broken

    from app.finance.contracts import setup

    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        ours = setup.add_entity(session, workspace, name="Наше ТОО", code="НТ")
        # Два участника с одним ключом имени (кавычки разные) - написание «ТОО Бета» спорное.
        for name in ('ТОО "Бета"', "ТОО «Бета»"):
            session.add(Counterparty(workspace_id=workspace.id, role="client", name=name, normalized_name=name.lower()))
        session.flush()
        twins = list(session.scalars(sa.select(Counterparty).where(Counterparty.name.in_(('ТОО "Бета"', "ТОО «Бета»")))))
        data = _sheet([["ТОО Альфа", broken("080944130482"), 100000, "15.09.2026"], ["ТОО Бета", "", 200000, "16.09.2026"]])
        answers = {"sheets": {"Клиенты": {"form": "contract", "executor": str(ours.counterparty_id), "billing": "total"}}, "questions": {}}
        proposed = excel.plan(session, workspace, data, "клиенты.xlsx", answers, today=TODAY)["sheets"][0]
        # У4: БИН с ошибкой - замечание, а не отказ листу; спорный участник - вопрос.
        assert proposed["ready"] and [item["kind"] for item in proposed["refusals"]] == ["bin"]
        assert [question["kind"] for question in proposed["questions"]] == ["party"]
        answers["questions"][proposed["questions"][0]["key"]] = str(twins[1].id)
        result = excel.apply(session, workspace, FULL, ACTOR, data, "клиенты.xlsx", answers, today=TODAY)
        assert result["created"] == 2 and result["blocked"] == []
        beta = session.scalar(sa.select(Contract).where(Contract.amount == Decimal(200000)))
        assert beta.customer_id == twins[1].id, "договор - у участника, которого назвал человек"
        alfa = session.scalar(sa.select(Contract).where(Contract.amount == Decimal(100000)))
        assert session.get(Counterparty, alfa.customer_id).bin in ("", None), "БИН с ошибкой не записан"
