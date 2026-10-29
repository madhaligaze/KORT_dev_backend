"""Полигон целиком: все источники «Тумар» в одной компании, человек-робот отвечает по каталогу.

Каждый вход полигона до этого проверялся в своей компании: 1С в интернете -
`test_onec_start`, 1С файлами - `test_onec_files`, таблицы отделов -
`test_excel_any_form`. Живая компания приходит со всем сразу, и входы
встречаются: реестр ложится поверх договоров 1С, ОСВ второй базы - рядом с
сальдо первой, выписки - поверх документов 1С. Здесь - так.

Порядок - первый час новой компании со страницы этапа (`STEPS`): 1С (база в
интернете, потом выгрузка файлами), Excel отделов (реестр, таблица
слушателей, таблица аренды), выписки банка, «Оплаты без договора». Робот
делает только то, что человек на экранах: подтверждает предложения KORT,
отвечает на вопросы ответом каталога (`cases.py`, по правде мира), один раз
размечает таблицу слушателей, вписывает сумму «1,5 млн» числом. Каждая
остановка KORT записывается с номером случая каталога: вопрос, предложение
или отказ. Шаги - методы `Robot`: стенд отдаёт любой из них человеку в
браузере и продолжает с того же места.

`uv run python -m polygon.whole` из `backend/tests` - полный прогон с печатью.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa

if __package__ in (None, "", "polygon"):
    # Запуск из `backend/tests`: пакет `app` лежит уровнем выше.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.finance import importing, secretbox
from app.finance import service as finance_service
from app.finance.contracts import importer
from app.finance.contracts import service as contracts_service
from app.finance.contracts.models import Contract, GroupEntity
from app.finance.contracts.payments import former_executors
from app.finance.core import excel, onec_start
from app.finance.core import participants as core_participants
from app.finance.core import payments as core_payments
from app.finance.core.debts import Debts
from app.finance.core.payments import Money
from app.finance.models import Counterparty, Operation
from app.finance.onec import files as onec_files
from app.finance.onec import sync as onec_sync
from app.finance.onec.models import OnecBase
from polygon import sources
from polygon.cases import ASK, BY_ID, OFFER, REFUSE
from polygon.world import TODAY, World

#: Шаги первого часа по порядку.
STEPS = ("onec_online", "onec_files", "registry", "school", "rent", "statements", "payments")
#: Разметка таблицы слушателей - ответ человека один раз на источник.
SCHOOL_LAYOUT = {
    "Слушатели": {
        "form": "contract",
        "columns": {
            "ФИО / Организация": "customer", "ИИН/БИН": "party_bin", "Курс": "subject", "Начало": "billing_from",
            "Стоимость": "amount", "Остаток": "remaining_snapshot",
            "Оплата 1": "pay_sum", "Оплата 2": "pay_sum", "Оплата 3": "pay_sum",
            "Дата 1": "pay_date", "Дата 2": "pay_date", "Дата 3": "pay_date",
        },
        "billing": "total", "department": "Учебный центр",
    },
    "Долги на 01.06": {"form": "balance", "columns": {"ФИО / Организация": "account", "Курс": "contract"}},
}
OFFICE = "1С Тумар Офис/"


@dataclass
class Stop:
    """Где KORT остановился: вопрос, предложение или отказ - и какой это случай каталога."""

    step: str
    kind: str
    case: str
    text: str


@dataclass
class Result:
    stops: list[Stop] = field(default_factory=list)
    #: Шаг → секунды сервера (без времени человека).
    seconds: dict[str, float] = field(default_factory=dict)
    #: Что человек сделал руками: ответов, подтверждений, разметок, файлов, полей.
    hands: dict[str, int] = field(default_factory=lambda: {"answers": 0, "confirms": 0, "layouts": 0, "files": 0, "fields": 0})
    owed: dict[str, Decimal] = field(default_factory=dict)
    truth: dict[str, Decimal] = field(default_factory=dict)
    #: Юрлицо → договор правды → (KORT, правда) - только где расходятся.
    by_contract: dict[str, dict[str, tuple[Decimal | None, Decimal | None]]] = field(default_factory=dict)
    contracts: int = 0
    entities: list[str] = field(default_factory=list)

    def of(self, kind: str) -> list[Stop]:
        return [stop for stop in self.stops if stop.kind == kind]

    def to_dict(self) -> dict[str, Any]:
        def text(value: Decimal | None) -> str | None:
            return str(value) if value is not None else None

        return {
            "stops": [stop.__dict__ for stop in self.stops], "seconds": self.seconds, "hands": self.hands,
            "owed": {key: str(value) for key, value in self.owed.items()}, "truth": {key: str(value) for key, value in self.truth.items()},
            "by_contract": {code: {key: [text(a), text(b)] for key, (a, b) in items.items()} for code, items in self.by_contract.items()},
            "contracts": self.contracts, "entities": self.entities,
        }


def run(session: Any, workspace: Any, world: World, files: dict[str, Any], steps: tuple[str, ...] = STEPS) -> Result:
    """Все источники полигона - в одну компанию; что KORT спросил, предложил и отказал - с номерами каталога."""
    robot = Robot(session, workspace, world, files)
    for name in steps:
        getattr(robot, name)()
    robot.owed()
    return robot.result


class Robot:
    """Человек-робот на экранах KORT: те же вызовы, что делают экраны, ответы - по каталогу."""

    def __init__(self, session: Any, workspace: Any, world: World, files: dict[str, Any]):
        self.session, self.workspace, self.world, self.files = session, workspace, world, files
        self.access = contracts_service.Access(view=True, edit=True, setup=True)
        self.actor = contracts_service.Actor(None, "robot@polygon")
        self.result = Result()
        self._clock = time.perf_counter()

    def lap(self, name: str) -> None:
        now = time.perf_counter()
        self.result.seconds[name] = round(self.result.seconds.get(name, 0) + now - self._clock, 2)
        self._clock = now

    def stop(self, step: str, kind: str, case: str, text: str) -> None:
        if any(item.step == step and item.text == text for item in self.result.stops):
            return
        self.result.stops.append(Stop(step, kind, case, text))
        if kind == ASK:
            self.result.hands["answers"] += 1
        elif kind == OFFER:
            self.result.hands["confirms"] += 1

    @property
    def by_code(self) -> dict[str, uuid.UUID | None]:
        """Наше юрлицо полигона → участник KORT: по полному или короткому имени."""
        names = {
            name: party_id
            for party_id, name in self.session.execute(
                sa.select(Counterparty.id, Counterparty.name)
                .join(GroupEntity, GroupEntity.counterparty_id == Counterparty.id)
                .where(GroupEntity.workspace_id == self.workspace.id)
            )
        }
        return {key: names.get(entity.name) or names.get(entity.short) for key, entity in self.world.entities.items()}

    def _bases(self) -> dict[str, OnecBase]:
        return {base.organization_name: base for base in self.session.scalars(sa.select(OnecBase).where(OnecBase.workspace_id == self.workspace.id))}

    def _ended(self, step: str, plan: dict[str, Any], entities: tuple[str, ...]) -> dict[str, Any]:
        """Шаг «Договоры» «Из 1С»: «закончился?» - ответ по правде; переводы на другое ТОО - предложение."""
        answers = {}
        for question in plan["contracts"]["questions"]:
            self.stop(step, ASK, _case_of(self.world, question["number"], entities, ASK), f"договор {question['number']} закончился?")
            truth = next(c for c in self.world.contracts.values() if c.number == question["number"] and c.entity in entities)
            answers[question["key"]] = {"end": truth.end.isoformat()} if truth.end else {"active": True}
        for move in plan["contracts"]["moves"]:
            self.stop(step, OFFER, _case_of(self.world, move["number"], entities, OFFER), f"{move['number']}: исполнитель - другое наше ТОО с даты")
        return answers

    def onec_online(self) -> None:
        """1С «Тумар Сервис» в интернете: адрес и пользователь базы - руками, дальше шаги «Из 1С»."""
        session, workspace, access, actor = self.session, self.workspace, self.access, self.actor
        odata = self.files["1С Тумар Сервис/odata.json"]
        org = odata["Catalog_Организации"][0]
        base = OnecBase(
            workspace_id=workspace.id, title="1С Тумар Сервис", url="https://1cfresh.kz/a/ea201/1", login="odata",
            secret=secretbox.seal("x"), organization_ref=org["Ref_Key"], organization_name=org["Description"],
            organization_bin=org["ИдентификационныйНомер"], sync_from=date(2026, 1, 1),
        )
        session.add(base)
        session.flush()
        self.result.hands["fields"] += 3
        snapshot = onec_sync.read(sources.OdataFake(odata), organization_ref=org["Ref_Key"], since=date(2026, 1, 1))
        onec_sync.apply(session, base, snapshot, since=base.sync_from)
        onec_start.add_entity(session, workspace, access, actor, base.id)
        onec_start.add_participants(session, workspace, access, actor, base.id, today=TODAY)
        onec_start.take_balances(session, workspace, access, actor, base.id, date(2026, 6, 1), client=sources.OdataFake(odata))
        osv = onec_start.stored_osv(session, workspace, base.id)
        plan = onec_start.plan(session, workspace, access, base.id, today=TODAY, osv=osv)
        answers = self._ended("1С Тумар Сервис", plan, ("ТС",))
        onec_start.add_contracts(session, workspace, access, actor, base.id, today=TODAY, answers=answers)
        onec_start.start_since(session, workspace, access, actor, base.id, osv)
        self.result.hands["confirms"] += 4  # юрлицо, участники, договоры, сальдо - шаги «Из 1С»
        self.lap("1С Тумар Сервис")

    def onec_files(self) -> None:
        """1С «Тумар Офис» и «Тумар Плюс» - выгрузка файлами: одна 1С на два ТОО."""
        session, workspace, access, actor = self.session, self.workspace, self.access, self.actor
        upload = onec_files.read([(name[len(OFFICE):], data) for name, data in self.files.items() if name.startswith(OFFICE)])
        onec_start.from_files(session, workspace, access, actor, upload)
        self.result.hands["files"] += sum(1 for name in self.files if name.startswith(OFFICE))
        bases = self._bases()
        for name in ('ТОО "Тумар Офис"', 'ТОО "Тумар Плюс"'):
            onec_start.add_entity(session, workspace, access, actor, bases[name].id)
        for name in ('ТОО "Тумар Офис"', 'ТОО "Тумар Плюс"'):
            base = bases[name]
            onec_start.add_participants(session, workspace, access, actor, base.id, today=TODAY)
            plan = onec_start.plan(session, workspace, access, base.id, today=TODAY, osv=onec_start.stored_osv(session, workspace, base.id))
            answers = self._ended(f"1С {name}", plan, ("ТО", "ТП"))
            onec_start.add_contracts(session, workspace, access, actor, base.id, today=TODAY, answers=answers)
            self.result.hands["confirms"] += 3
        office = bases['ТОО "Тумар Офис"']
        onec_start.start_since(session, workspace, access, actor, office.id, onec_start.stored_osv(session, workspace, office.id))
        self.result.hands["confirms"] += 1
        self.lap("1С Тумар Офис и Тумар Плюс")

    def registry(self) -> None:
        """Реестр офис-менеджера поверх договоров 1С: скрытая строка (Ф9), сумма с даты (Д5), сверка."""
        session, workspace, world = self.session, self.workspace, self.world
        data, name = self.files["Excel/Реестр договоров 2026.xlsx"], "Реестр договоров 2026.xlsx"
        self.result.hands["files"] += 1
        batch = importer.start(session, workspace, self.actor, data, name)
        sections = {section["key"]: section for section in batch.report["sections"]}
        decisions: dict[str, Any] = {
            "entities": {"confirmed": True, "own": [item["key"] for item in sections["entities"]["items"] if item["own"]]},
            "rules": {item["block"]: {"action": "accept"} for item in sections["rules"]["items"]},
            "known": {},
        }
        self.result.hands["confirms"] += 2
        for item in sections["hidden"]["items"]:
            self.stop("реестр", ASK, next(ph["case"] for ph in world.phantoms if ph["number"] == item["number"]),
                      f"скрытая строка {item['number']} - завести или пропустить?")
            decisions["hidden"] = "skip"
        everyone = tuple(world.entities)
        for item in sections["known"]["items"]:
            if item["question"]:
                self.stop("реестр", ASK, _case_of(world, item["number"], everyone, ASK), item["question"]["text"])
                decisions["known"][item["ref"]] = "kort"  # каталог Д5: сумма с даты по актам 1С, до этого прежняя
            for line in item["compare"]:
                self.stop("реестр", OFFER, _case_of(world, item["number"], everyone, OFFER),
                          f"{item['name']} {item['number']}: {line['text']} (файл {line['file']}, KORT {line['kort']})")
        for entry in sections["bins"]["items"]:
            for refused in entry["refused"]:
                party = next((p for p in world.parties.values() if p.registry_number == refused["raw"]), None)
                self.stop("реестр", REFUSE, next((c for c in (party.cases if party else ()) if BY_ID[c].expect == REFUSE), ""),
                          f"БИН «{refused['raw']}» у {entry['name']}: {refused['error']}")
        for entry in sections["amounts"]["items"]:
            self.stop("реестр", REFUSE, "Ф2", f"сумма «{entry['text']}» - не число")
        batch = importer.decide(session, workspace, batch.id, decisions)
        assert not batch.report["blocking"], batch.report["blocking"]
        importer.apply(session, workspace, self.access, self.actor, batch.id)
        self.lap("реестр")

    def school(self) -> None:
        """Сумма курса числом (Ф2) и таблица слушателей учебного центра - разметка один раз."""
        session, workspace, access, actor = self.session, self.workspace, self.access, self.actor
        ip = self.by_code["ИП"]
        course = session.scalar(sa.select(Contract).where(
            Contract.workspace_id == workspace.id, Contract.executor_id == ip, Contract.amount.is_(None), Contract.amount_terms != "",
        ))
        if course is not None:
            contracts_service.patch(session, workspace, access, actor, course.id, {"amount": "1500000"}, known_seq=None,
                                    mode=contracts_service.Mode(kind="fix"))
            self.result.hands["fields"] += 1
        data, name = self.files["Excel/Учебный центр 2026.xlsx"], "Учебный центр 2026.xlsx"
        self.result.hands["files"] += 1
        self.result.hands["layouts"] += 1
        layout = json.loads(json.dumps(SCHOOL_LAYOUT))
        for sheet in layout.values():
            sheet["executor"] = str(ip)
        answers = {"sheets": layout, "questions": {}}
        for sheet in excel.plan(session, workspace, data, name, answers, today=TODAY)["sheets"]:
            for question in sheet["questions"]:
                if question["kind"] == "date_order":
                    self.stop("слушатели", ASK, "Ф3", question["text"])
                    answers["sheets"][sheet["sheet"]]["date_order"] = "dmy"
                else:
                    self.stop("слушатели", ASK, "", question.get("text", question["kind"]))
        excel.apply(session, workspace, access, actor, data, name, answers, today=TODAY)
        self.result.hands["confirms"] += 1
        self.lap("таблица слушателей")

    def rent(self) -> None:
        """Таблица аренды поверх 1С: сверка, а не второй раз; спорный долг на 01.06 - вопрос (С1)."""
        data, name = self.files["Excel/Аренда - начисления 2026.xlsx"], "Аренда - начисления 2026.xlsx"
        self.result.hands["files"] += 1
        answers: dict[str, Any] = {"questions": {}}
        for sheet in excel.plan(self.session, self.workspace, data, name, {}, today=TODAY)["sheets"]:
            for question in sheet["questions"]:
                self.stop("аренда", ASK, "С1" if question["kind"] == "opening" else "", question.get("text", question["kind"]))
                answers["questions"][question["key"]] = "keep"  # каталог С1: по ОСВ 1С
            for refusal in sheet["refusals"]:
                self.stop("аренда", REFUSE, "Ф5" if refusal["kind"] == "duplicate" else "", refusal.get("text", refusal["kind"]))
        excel.apply(self.session, self.workspace, self.access, self.actor, data, name, answers, today=TODAY)
        self.result.hands["confirms"] += 1
        self.lap("таблица аренды")

    def statements(self) -> None:
        """Выписки Kaspi: счета юрлиц заводит человек, файлы - все разом; БИН плательщиков - предложением."""
        session, workspace = self.session, self.workspace
        for key, entity_id in self.by_code.items():
            entity = self.world.entities[key]
            finance_service.create_account(session, workspace, name=f"Kaspi {entity.short}", number=entity.iban, group_entity_id=entity_id)
            self.result.hands["fields"] += 2
        names = [account.name for account in finance_service.list_accounts(session, workspace.id)]
        numbers = finance_service.account_numbers(session, workspace.id)
        for name, data in sorted(self.files.items()):
            if not name.startswith("Банк/"):
                continue
            self.result.hands["files"] += 1
            preview = importing.analyze(data, Path(name).name, names, account_numbers=numbers)
            if preview.question is not None:
                self.stop("выписки", ASK, "", f"{name}: {preview.question}")
            batch = finance_service.save_preview(session, workspace, preview)
            finance_service.apply_batch(session, workspace, batch.id, actor="robot")
            self.result.hands["confirms"] += 1
        session.flush()
        for item in [item for item in core_participants.bin_proposals(session, workspace, self.access) if item["clear"]]:
            number = item["numbers"][0]["bin"]
            party = next((p for p in self.world.parties.values() if p.number == number), None)
            self.stop("участники", OFFER, next((c for c in (party.cases if party else ()) if BY_ID[c].expect == OFFER), ""),
                      f"{item['name']}: БИН {number}")
            core_participants.set_bin(session, workspace, self.access, self.actor, uuid.UUID(item["party_id"]), number)
        session.flush()
        self.lap("выписки")

    def payments(self) -> None:
        """«Оплаты без договора»: после каждого решения список заново, как на экране."""
        session, workspace, access, actor = self.session, self.workspace, self.access, self.actor
        handled: set[str] = set()
        for _guard in range(60):
            items = [item for item in Money(session, workspace, today=TODAY).without_contract() if item["operation_id"] not in handled]
            if not items:
                break
            item = items[0]
            handled.add(item["operation_id"])
            operation = session.get(Operation, uuid.UUID(item["operation_id"]))
            text = f"{item['paid_at']} {item['amount']} «{item['comment']}» от {item['payer'] or 'не указан'}"
            if item.get("split"):
                self.stop("оплаты", OFFER, _payment_case(self.world, operation, (OFFER,)), f"разделить, как в 1С: {text}")
                core_payments.split(session, workspace, access, actor, operation.id,
                                    [(uuid.UUID(part["id"]), part["amount"]) for part in item["split"]])
            elif item["direction"] == "out":
                self.stop("оплаты", OFFER, _payment_case(self.world, operation, (OFFER,)), f"возврат по договору: {text}")
                core_payments.decide(session, workspace, access, actor, operation.id, action="link",
                                     contract_id=uuid.UUID(item["candidates"][0]["id"]))
            else:
                self.stop("оплаты", ASK, _payment_case(self.world, operation, (ASK,)), text)
                chosen = _truth_contract(session, workspace, self.world, operation, self.by_code)
                if chosen is not None:
                    core_payments.decide(session, workspace, access, actor, operation.id, action="link", contract_id=chosen)
            session.flush()
        self.lap("оплаты без договора")

    def owed(self) -> None:
        """«Нам должны» на 25.10 против правды - по договорам."""
        session, workspace, world = self.session, self.workspace, self.world
        by_code = self.by_code
        debts = Debts(session, workspace, self.access, day=TODAY)
        truth_of = _truth_map(session, workspace, world, by_code)
        owed_by: dict[str, dict[str, Decimal]] = {}
        for item in debts.items.values():
            for code, entity_id in by_code.items():
                owed = item.owed_to(entity_id)
                if owed:
                    key = truth_of.get(item.contract.id) or f"? {item.contract.number or str(item.contract.id)[:8]}"
                    owed_by.setdefault(code, {})[key] = owed_by.get(code, {}).get(key, Decimal(0)) + owed
        self.lap("Нам должны")
        result = self.result
        result.owed = {code: sum(items.values(), Decimal(0)) for code, items in owed_by.items()}
        result.truth = dict(world.owed())
        for code in by_code:
            truth_items = {
                key: world.debt(key) for key, contract in world.contracts.items()
                if contract.side == "sale" and contract.executor_on(TODAY) == code and world.debt(key) > 0
            }
            mine = owed_by.get(code, {})
            result.by_contract[code] = {
                key: (mine.get(key), truth_items.get(key)) for key in set(mine) | set(truth_items)
                if (mine.get(key) or Decimal(0)) != (truth_items.get(key) or Decimal(0))
            }
        result.contracts = session.scalar(
            sa.select(sa.func.count()).select_from(Contract).where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None))
        )
        result.entities = sorted(
            session.scalars(
                sa.select(Counterparty.name).join(GroupEntity, GroupEntity.counterparty_id == Counterparty.id)
                .where(GroupEntity.workspace_id == workspace.id)
            )
        )


def _case_of(world: World, contract_number: str, entities: tuple[str, ...], wanted: str) -> str:
    """Случай каталога, который держит договор правды с этим номером, - того вида, что ждёт остановка."""
    for contract in world.contracts.values():
        if contract.number == contract_number and contract.entity in entities:
            for case in contract.cases:
                if BY_ID[case].expect == wanted:
                    return case
    return ""


def _payment_case(world: World, operation: Any, wanted: tuple[str, ...]) -> str:
    """Случай каталога у оплаты правды: свой у оплаты, иначе у её договора («О…»)."""
    for payment in world.payments:
        if payment.day == operation.paid_at and payment.amount == operation.amount_base and payment.purpose == operation.comment:
            for case in payment.cases:
                if BY_ID[case].expect in wanted:
                    return case
            for key, _month, _amount in payment.alloc:
                for case in world.contracts[key].cases:
                    if case.startswith("О") and BY_ID[case].expect in wanted:
                        return case
    return ""


def _truth_map(session: Any, workspace: Any, world: World, by_code: dict[str, Any]) -> dict[uuid.UUID, str]:
    """Договор KORT → ключ договора правды: наше юрлицо (с прежними), сторона по БИН или имени, номер."""
    code_of = {entity_id: code for code, entity_id in by_code.items() if entity_id}
    former = former_executors(session, workspace.id)
    parties = {party.id: party for party in session.scalars(sa.select(Counterparty).where(Counterparty.workspace_id == workspace.id))}
    out: dict[uuid.UUID, str] = {}
    for contract in session.scalars(sa.select(Contract).where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None))):
        codes = {code_of[item] for item in {contract.executor_id, contract.customer_id, *former.get(contract.id, ())} if item in code_of}
        party = parties.get(contract.customer_id if contract.executor_id in code_of else contract.executor_id)
        if party is None:
            continue
        keys = [
            key for key, truth in world.contracts.items()
            if truth.entity in codes and (
                (truth.party in world.parties and (world.parties[truth.party].number == party.bin
                                                    or world.parties[truth.party].name in party.name))
                or (truth.party in world.entities and world.entities[truth.party].name == party.name)
            )
        ]
        if len(keys) > 1:
            keys = [key for key in keys if world.contracts[key].number == contract.number] or keys
        if len(keys) == 1:
            out[contract.id] = keys[0]
    return out


def _truth_contract(session: Any, workspace: Any, world: World, operation: Any, by_code: dict[str, Any]) -> uuid.UUID | None:
    """Ответ человека на вопрос об оплате: договор, за который она по правде полигона."""
    truth = next(
        (p for p in world.payments if p.day == operation.paid_at and p.amount == operation.amount_base
         and p.purpose == operation.comment and p.alloc),
        None,
    )
    if truth is None:
        return None
    wanted = truth.alloc[0][0]
    return next((contract_id for contract_id, key in _truth_map(session, workspace, world, by_code).items() if key == wanted), None)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    from app.finance import db as finance_db_module
    from app.finance.config import finance_settings
    from app.finance.db import finance_session
    from polygon.world import build

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        engine = sa.create_engine(f"sqlite:///{Path(folder) / 'whole.db'}", future=True)
        finance_db_module.get_engine = lambda: engine  # type: ignore[assignment]
        finance_db_module._session_factory = None
        finance_db_module._initialized = False
        finance_settings.secrets_key = finance_settings.secrets_key or "polygon-whole"
        world = build()
        with finance_session() as session:
            workspace = finance_service.ensure_workspace(session)
            result = run(session, workspace, world, sources.render(world))
            session.rollback()
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
