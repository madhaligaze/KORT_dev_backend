"""Доли сотрудников и отделов в договоре.

На экране и в выгрузке с 30.09.2026 - «Сотрудники» и «Доли сотрудников»
(было «Исполнители»); в коде люди договора по-прежнему `people`.

Откуда
──────
29.09.2026 финансисты BBC: над одним договором работают несколько юристов, и
доли у них разные - договор на 700 000, у одного 500 000, у другого 200 000.
Проверка по книгам: в реестре ЮО 6 договоров из 421 на двоих-троих
(«Елжас, Рысбек · 700 000», «Тимур, Салтанат, Алтынай · 348 000»), колонок
долей нет ни в одной книге, а «Сводка по сотрудникам» считает `SUMIFS` по
точному имени - договор на двоих в неё не попадает вовсе. Людей в договоре
сколько угодно: доля - у каждого своя строка, а не «первый и второй».

Суммой или процентом
────────────────────
Доля хранится так, как её ввели (`share_amount` или `share_percent`), а
второе число выводится из суммы договора. Процент следует за суммой: договор
подорожал соглашением - доли в процентах пересчитались сами. Сумма остаётся
суммой: у договора, увеличенного после распределения, появится
нераспределённый остаток, и это видно, а не спрятано пересчётом. Внутри
договора - одна единица на весь блок: половина долей в тенге, половина в
процентах складывалась бы только при известной сумме.

Кто видит
─────────
* Владелец и администратор - всё.
* Начальник отдела (право «Сотрудники и права» своего отдела) - доли всех
  людей в договорах своего отдела: отдел договора, отдел-соисполнитель или
  отдел кого-то из ответственных. Правит их он же.
* Остальные - только свою долю, если стоят ответственными. Чужих сумм в
  ответе нет вовсе, а не спрятано в интерфейсе: иначе их было бы видно в
  инструментах браузера.
* Доли отделов видят администратор, владелец и начальник отдела - в
  договорах, где стоит его отдел (с полным правом на договоры - во всех),
  пока администратор не снял ему «видит доли отделов» в «Правах отдела»
  (30.09.2026: по умолчанию видит). Правят - администратор и начальник, у
  которого в договорах «включено всё» (правит, все договоры, без ограничения
  юрлиц).
* Исключение - доля отдела, которым отобрана книга: в «Разовых ЮО» сумма
  договора - доля ЮО, и её видят все, кому открыт договор (ниже, «Доля
  отдела книги»).

Поэтому доли не входят в ответ реестра (он общий на одинаковые права,
`SharedBuild`), а приходят своим запросом. И в «Историю» договора события
долей попадают только тем, кому доли открыты (`hidden_history`).

Отделы долей - это поле «Отдел»
───────────────────────────────
С 30.09.2026 отделы договора - один список (`Contract.department_rows`), как
люди в «Ответственном лице»: разнесли доли между HR и ЮО - поле стало «HR,
ЮО»; вписали в поле «ОБО, НО, ЮО, HR» - у договора четыре отдела, доли пока не
указаны. Главного отдела нет. Убрать отдел из списка может только
администратор или владелец - и здесь, и в самом поле.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history
from app.finance.contracts.models import Contract, ContractDepartment, ContractPerson, Department
from app.finance.contracts.service import (
    Access,
    Actor,
    NotFound,
    Registry,
    _check_departments_kept,
    _check_write,
    _finish,
    _history_title,
    get_contract,
    people_of,
    read_money,
    visible_to,
)
from app.finance.models import Workspace
from app.finance.service import FinanceError

UNITS = ("amount", "percent")
PEOPLE_KIND = "contract.shares.people"
DEPARTMENTS_KIND = "contract.shares.departments"
_CENT = Decimal("0.01")
_PERCENT_STEP = Decimal("0.0001")
_HUNDRED = Decimal(100)
#: Допуск суммы долей: копейки округления процента не повод отказать.
_AMOUNT_SLACK = Decimal("1")
_PERCENT_SLACK = Decimal("0.01")


class SharesError(FinanceError):
    """Доли нельзя принять - с текстом для человека."""


# ── Кто что видит ────────────────────────────────────────────────────────────


def _departments_of_people(registry: Registry, people: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    return {
        employee.department_id
        for employee in registry.employees_for(people).values()
        if employee.department_id is not None
    }


def co_departments(session: Session, contract_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    if not contract_ids:
        return {}
    rows = session.execute(
        sa.select(ContractDepartment.contract_id, ContractDepartment.department_id)
        .where(ContractDepartment.contract_id.in_(list(contract_ids)))
        .order_by(ContractDepartment.position)
    )
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for contract_id, department_id in rows:
        out.setdefault(contract_id, []).append(department_id)
    return out


def heads_contract(
    access: Access,
    contract: Contract,
    registry: Registry,
    people: Sequence[uuid.UUID],
    co: Sequence[uuid.UUID] = (),
) -> bool:
    """Договор отдела, которым человек руководит: его отдел в списке отделов
    договора или в отделе кого-то из ответственных."""
    head = access.head_department
    if head is None:
        return False
    if head in contract.department_ids or head in co:
        return True
    return head in _departments_of_people(registry, people)


def people_scope(
    access: Access,
    contract: Contract,
    registry: Registry,
    people: Sequence[uuid.UUID],
    co: Sequence[uuid.UUID] = (),
) -> str:
    """`all` - доли всех людей; `own` - только своя; `none` - ни одной."""
    if access.admin or heads_contract(access, contract, registry, people, co):
        return "all"
    if access.employee_id is not None and access.employee_id in people:
        return "own"
    return "none"


def departments_open(access: Access, contract: Contract | None = None) -> bool:
    """Видны ли доли отделов договора.

    Администратору и владельцу - всегда. Начальнику - пока администратор не
    снял «видит доли отделов» (по умолчанию видит, решение владельца
    30.09.2026): с полным правом на договоры - во всех, иначе - в договорах,
    где стоит его отдел, в том числе совместных.
    """
    if access.admin:
        return True
    if access.head_department is None or not access.head_shares:
        return False
    if access.full_contracts:
        return True
    return contract is not None and access.head_department in contract.department_ids


def departments_editable(access: Access) -> bool:
    """Править доли отделов - администратор и начальник с полным правом на договоры."""
    return access.admin or (access.head_department is not None and access.head_shares and access.full_contracts)


def hidden_history(
    session: Session, registry: Registry, access: Access, contract: Contract
) -> frozenset[str]:
    """Какие события долей не показывать в «Истории» договора этому человеку."""
    people = people_of(session, [contract.id]).get(contract.id, [])
    co = co_departments(session, [contract.id]).get(contract.id, [])
    hidden: set[str] = set()
    if people_scope(access, contract, registry, people, co) != "all":
        hidden.add(PEOPLE_KIND)
    if not departments_open(access, contract):
        hidden.add(DEPARTMENTS_KIND)
    return frozenset(hidden)


# ── Числа ────────────────────────────────────────────────────────────────────


def _money_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(_CENT, rounding=ROUND_HALF_UP), "f")


def _percent_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(_PERCENT_STEP, rounding=ROUND_HALF_UP).normalize(), "f")


def _pair(total: Decimal | None, amount: Decimal | None, percent: Decimal | None) -> tuple[Decimal | None, Decimal | None, str | None]:
    """(сумма, процент, как введено) - второе число выводится из суммы договора."""
    if amount is not None:
        derived = (amount * _HUNDRED / total) if total else None
        return amount, derived, "amount"
    if percent is not None:
        derived = (total * percent / _HUNDRED).quantize(_CENT, rounding=ROUND_HALF_UP) if total is not None else None
        return derived, percent, "percent"
    return None, None, None


_PERCENT_JUNK = re.compile(r"[\s  %]+")


def _read_percent(raw: Any, title: str) -> Decimal | None:
    if raw is None or raw == "":
        return None
    text = _PERCENT_JUNK.sub("", str(raw)).replace(",", ".")
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise SharesError(f"{title}: «{raw}» - не процент") from exc
    if value < 0:
        raise SharesError(f"{title}: доля не бывает отрицательной")
    if value > _HUNDRED:
        raise SharesError(f"{title}: больше 100%")
    return value.quantize(_PERCENT_STEP, rounding=ROUND_HALF_UP)


def _read_amount(raw: Any, title: str) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        amount, terms = read_money(raw, field=title)
    except FinanceError as exc:
        raise SharesError(str(exc).replace("сумма договора", "доля")) from exc
    if terms:
        raise SharesError(f"{title}: «{terms}» - не сумма")
    return amount


def _read(raw: Any, unit: str, title: str) -> tuple[Decimal | None, Decimal | None]:
    if unit == "percent":
        return None, _read_percent(raw, title)
    return _read_amount(raw, title), None


def _group(total: Decimal | None, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Итог блока: распределено и остаток - суммой и процентом, где их можно посчитать."""
    amounts = [row["_amount"] for row in rows if row["_amount"] is not None]
    percents = [row["_percent"] for row in rows if row["_percent"] is not None]
    given = [row for row in rows if row["entered"] is not None]
    allocated_amount = sum(amounts, Decimal(0)) if amounts and len(amounts) == len(given) else None
    allocated_percent = sum(percents, Decimal(0)) if percents and len(percents) == len(given) else None
    out: dict[str, Any] = {
        "given": len(given),
        "allocated_amount": _money_text(allocated_amount),
        "allocated_percent": _percent_text(allocated_percent),
        "rest_amount": None,
        "rest_percent": None,
        "over": False,
    }
    if allocated_percent is not None:
        out["rest_percent"] = _percent_text(_HUNDRED - allocated_percent)
        out["over"] = allocated_percent > _HUNDRED + _PERCENT_SLACK
    if allocated_amount is not None and total is not None:
        out["rest_amount"] = _money_text(total - allocated_amount)
        out["over"] = out["over"] or allocated_amount > total + _AMOUNT_SLACK
    return out


def _unit_of(rows: Iterable[dict[str, Any]]) -> str | None:
    for row in rows:
        if row["entered"]:
            return row["entered"]
    return None


def _clean(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in row.items() if not key.startswith("_")} for row in rows]


# ── Доля отдела книги ────────────────────────────────────────────────────────
#
# 30.09.2026, юристы и финансисты BBC: в «Разовых ЮО» (карточки, таблица,
# выгрузка) сумма договора - доля ЮО. №ОКР/59 на 348 000 разделён HR 70% /
# ЮО 30%, а в списке стояло 348 000; нужно 104 400. В карточке договора сумма
# остаётся целой. Книга «про отдел», если все её листы отобраны одним отделом
# (`views.book_department`).
#
# Долю этого отдела ответ реестра несёт всем, кому открыт договор, - и юристу
# без права на доли отделов: об этом и просили («видеть долю Юридического
# отдела»). Долей других отделов в ответе нет.
#
# «Оплачено» и «Остаток» из сводки - та же часть договора: клиент платит за
# договор целиком, и отделу приходится его доля платежа. Так же «По
# сотрудникам» считает остаток у человека с долей (`staff.ts`).


def department_share(contract: Contract, department_id: uuid.UUID) -> tuple[Decimal | None, Decimal | None] | None:
    """Доля отдела в договоре - (сумма, процент), если она задана; иначе `None`."""
    for row in contract.department_rows:
        if row.department_id == department_id and (row.share_amount is not None or row.share_percent is not None):
            amount, percent, _entered = _pair(contract.amount, row.share_amount, row.share_percent)
            return amount, percent
    return None


def book_shares(contract: Contract, departments: Iterable[uuid.UUID]) -> dict[str, dict[str, str | None]]:
    """Доли отделов книг в договоре - для ответа реестра: отдел → сумма и процент."""
    out: dict[str, dict[str, str | None]] = {}
    for department_id in departments:
        share = department_share(contract, department_id)
        if share is not None:
            out[str(department_id)] = {"amount": _money_text(share[0]), "percent": _percent_text(share[1])}
    return out


@dataclass(frozen=True)
class BookShare:
    """Сколько договора у отдела книги.

    `amount` - что стоит в «Сумме»; `fraction` - часть договора для «Оплачено»
    и «Остатка» (`None` - не посчитать: доля суммой у договора без суммы);
    `kind`: `share` - доля задана, `whole` - отдел в договоре один, `unset` -
    отделов несколько, а доли этого нет. Тогда, как у «По сотрудникам»,
    договор у отдела целиком, а лист и выгрузка говорят, что доля не задана.
    """

    amount: Decimal | None
    fraction: Decimal | None
    kind: str


def book_share(contract: Contract, department_id: uuid.UUID) -> BookShare:
    total = contract.amount
    share = department_share(contract, department_id)
    if share is not None:
        amount, percent = share
        if percent is not None:
            fraction: Decimal | None = percent / _HUNDRED
        elif amount is not None and total:
            fraction = amount / total
        else:
            fraction = None
        return BookShare(amount, fraction, "share")
    return BookShare(total, Decimal(1), "whole" if len(contract.department_ids) <= 1 else "unset")


# ── Чтение ───────────────────────────────────────────────────────────────────


def _people_rows(session: Session, contract_id: uuid.UUID) -> list[ContractPerson]:
    return list(
        session.scalars(
            sa.select(ContractPerson)
            .where(ContractPerson.contract_id == contract_id)
            .order_by(ContractPerson.position)
        )
    )


def _load(
    session: Session, workspace: Workspace, access: Access, contract_id: uuid.UUID, *, for_update: bool = False
) -> tuple[Registry, Contract, list[uuid.UUID], list[uuid.UUID]]:
    registry = Registry(session, workspace)
    contract = get_contract(session, workspace, contract_id, for_update=for_update)
    people = people_of(session, [contract.id]).get(contract.id, [])
    if not visible_to(contract, registry, access, people):
        raise NotFound("Договор не найден")
    co = co_departments(session, [contract.id]).get(contract.id, [])
    return registry, contract, people, co


def of_contract(session: Session, workspace: Workspace, access: Access, contract_id: uuid.UUID) -> dict[str, Any]:
    registry, contract, people, co = _load(session, workspace, access, contract_id)
    return _out(session, registry, access, contract, people, co)


def _writable(contract: Contract, registry: Registry, access: Access, people: Sequence[uuid.UUID]) -> bool:
    return visible_to(contract, registry, access, people, write=True)


def _out(
    session: Session,
    registry: Registry,
    access: Access,
    contract: Contract,
    people: Sequence[uuid.UUID],
    co: Sequence[uuid.UUID],
) -> dict[str, Any]:
    total = contract.amount
    scope = people_scope(access, contract, registry, people, co)
    employees = registry.employees_for(people)
    person_rows: list[dict[str, Any]] = []
    for row in _people_rows(session, contract.id):
        mine = access.employee_id is not None and row.employee_id == access.employee_id
        if scope == "none" or (scope == "own" and not mine):
            continue
        employee = employees.get(row.employee_id)
        department = registry.departments.get(employee.department_id) if employee and employee.department_id else None
        amount, percent, entered = _pair(total, row.share_amount, row.share_percent)
        person_rows.append(
            {
                "employee_id": str(row.employee_id),
                "name": employee.full_name if employee else "",
                "department": department.code if department else "",
                "amount": _money_text(amount),
                "percent": _percent_text(percent),
                "entered": entered,
                "mine": mine,
                "_amount": amount,
                "_percent": percent,
            }
        )
    writable = _writable(contract, registry, access, people)
    people_out: dict[str, Any] = {
        "scope": scope,
        "can_edit": scope == "all" and writable,
        "count": len(people),
        "unit": _unit_of(person_rows),
        "rows": _clean(person_rows),
    }
    if scope == "all":
        people_out["summary"] = _group(total, person_rows)
    departments_out: dict[str, Any] | None = None
    if departments_open(access, contract):
        rows: list[dict[str, Any]] = []
        stored = contract.department_rows
        taken = {row.department_id for row in stored}
        for row in stored:
            department = registry.departments.get(row.department_id)
            amount, percent, entered = _pair(total, row.share_amount, row.share_percent)
            rows.append(
                {
                    "department_id": str(row.department_id),
                    "code": department.code if department else "",
                    "title": (department.title or department.code) if department else "",
                    "amount": _money_text(amount),
                    "percent": _percent_text(percent),
                    "entered": entered,
                    "_amount": amount,
                    "_percent": percent,
                }
            )
        departments_out = {
            "can_edit": writable and departments_editable(access),
            # Убрать любой отдел - администратор или владелец; сотрудник - только
            # вписанный им самим и без доли (это фронт знает по `departments_by`).
            "can_remove": writable and access.admin,
            "unit": _unit_of(rows),
            "rows": _clean(rows),
            "summary": _group(total, rows),
            "choices": [
                {"id": str(item.id), "code": item.code, "title": item.title or item.code}
                for item in sorted(registry.departments.values(), key=lambda item: item.position)
                if item.archived_at is None or item.id in taken
            ],
        }
    return {
        "contract_id": str(contract.id),
        "amount": _money_text(total),
        "billing": contract.billing or "",
        "seq": contract.seq,
        "people": people_out,
        "departments": departments_out,
    }


def all_visible(session: Session, workspace: Workspace, access: Access) -> dict[str, Any]:
    """Доли всех видимых договоров - для «По сотрудникам», листа и отбора «С долями».

    Договоры, где у кого-то задана доля, и совместные - двое исполнителей и
    больше, даже пока доли не разнесены (`people` тогда пустой). До 30.09.2026
    совместные без сумм сюда не попадали, и «С долями» у владельца был пуст:
    на проде долей не ввели ещё ни в одном договоре, а совместных - шесть.

    У каждого - доли, открытые этому человеку: сотруднику - своя и только в
    его договорах, начальнику - договоров его отдела, администратору - все.
    """
    registry = Registry(session, workspace)
    rows = session.execute(
        sa.select(ContractPerson.contract_id, ContractPerson.employee_id, ContractPerson.share_amount, ContractPerson.share_percent)
        .join(Contract, Contract.id == ContractPerson.contract_id)
        .where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None))
    ).all()
    everyone: dict[uuid.UUID, int] = {}
    by_contract: dict[uuid.UUID, list[tuple[uuid.UUID, Decimal | None, Decimal | None]]] = {}
    for contract_id, employee_id, amount, percent in rows:
        everyone[contract_id] = everyone.get(contract_id, 0) + 1
        if amount is not None or percent is not None:
            by_contract.setdefault(contract_id, []).append((employee_id, amount, percent))
    together = {contract_id for contract_id, count in everyone.items() if count > 1}
    ids = list(set(by_contract) | together)
    if not ids:
        return {"contracts": {}}
    contracts = {
        item.id: item for item in session.scalars(sa.select(Contract).where(Contract.id.in_(ids)))
    }
    people = people_of(session, ids)
    co = co_departments(session, ids)
    out: dict[str, Any] = {}
    for contract_id in ids:
        contract = contracts.get(contract_id)
        if contract is None:
            continue
        mine = people.get(contract_id, [])
        if not visible_to(contract, registry, access, mine):
            continue
        scope = people_scope(access, contract, registry, mine, co.get(contract_id, []))
        if scope == "none":
            continue
        entry: dict[str, Any] = {}
        for employee_id, amount, percent in by_contract.get(contract_id, []):
            if scope == "own" and employee_id != access.employee_id:
                continue
            value, share, _entered = _pair(contract.amount, amount, percent)
            entry[str(employee_id)] = {"amount": _money_text(value), "percent": _percent_text(share)}
        # Совместный договор - в ответе и без сумм; одиночный - только с
        # заданной долей. Сотруднику со `own` сюда попадают лишь договоры,
        # где он сам исполнитель (`people_scope`).
        if entry or contract_id in together:
            out[str(contract_id)] = {"scope": scope, "people": entry}
    return {"contracts": out}


# ── Запись ───────────────────────────────────────────────────────────────────


def _check_sum(total: Decimal | None, unit: str, values: list[Decimal], label: str) -> None:
    if not values:
        return
    summed = sum(values, Decimal(0))
    if unit == "percent" and summed > _HUNDRED + _PERCENT_SLACK:
        raise SharesError(f"{label}: вместе {_percent_text(summed)}% - больше 100%")
    if unit == "amount" and total is not None and summed > total + _AMOUNT_SLACK:
        raise SharesError(
            f"{label}: вместе {_money_text(summed)} - больше суммы договора {_money_text(total)}"
        )


def _unit(raw: Any) -> str:
    unit = str(raw or "amount")
    if unit not in UNITS:
        raise SharesError("Доля - суммой или процентом")
    return unit


def _snapshot(total: Decimal | None, amount: Decimal | None, percent: Decimal | None) -> dict[str, str | None]:
    return {"amount": _money_text(amount), "percent": _percent_text(percent)}


def set_people(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    contract_id: uuid.UUID,
    unit_raw: Any,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Доли ответственных - все разом: названные получают значение, не названные - пусто."""
    registry, contract, people, co = _load(session, workspace, access, contract_id, for_update=True)
    if people_scope(access, contract, registry, people, co) != "all":
        raise PermissionError("Доли сотрудников распределяет начальник отдела или администратор")
    _check_write(contract, registry, access, people)
    unit = _unit(unit_raw)
    employees = registry.employees_for(people)
    rows = {row.employee_id: row for row in _people_rows(session, contract.id)}
    wanted: dict[uuid.UUID, tuple[Decimal | None, Decimal | None]] = {}
    for item in items or []:
        try:
            employee_id = uuid.UUID(str((item or {}).get("employee_id")))
        except ValueError as exc:
            raise SharesError("Сотрудник указан неверно") from exc
        if employee_id not in rows:
            raise SharesError("Доля - только у ответственного этого договора: сначала впишите его в «Ответственное лицо»")
        if employee_id in wanted:
            raise SharesError("Один сотрудник назван дважды")
        name = employees[employee_id].full_name if employee_id in employees else "Доля"
        wanted[employee_id] = _read(item.get("value"), unit, name)
    values = [amount if unit == "amount" else percent for amount, percent in wanted.values()]
    _check_sum(contract.amount, unit, [value for value in values if value is not None], "Доли сотрудников")
    before = {
        str(employee_id): _snapshot(contract.amount, row.share_amount, row.share_percent)
        for employee_id, row in rows.items()
    }
    for employee_id, row in rows.items():
        amount, percent = wanted.get(employee_id, (None, None))
        row.share_amount = amount
        row.share_percent = percent
    after = {
        str(employee_id): _snapshot(contract.amount, row.share_amount, row.share_percent)
        for employee_id, row in rows.items()
    }
    if before != after:
        _finish(session, registry, contract, actor, [])
        given = sum(1 for value in after.values() if value["amount"] or value["percent"])
        history.write(
            session,
            workspace,
            kind=PEOPLE_KIND,
            entity="contract",
            entity_id=contract.id,
            # Без сумм в заголовке: заголовок читают там, где суммы не открыты.
            title=f"договор {contract.number or ''} · доли сотрудников: задано у {given} из {len(rows)}".replace("  ", " "),
            before={"people_shares": before},
            after={"people_shares": after},
            actor=actor.email,
        )
    session.flush()
    return _out(session, registry, access, contract, people, co)


def set_departments(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    contract_id: uuid.UUID,
    unit_raw: Any,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Отделы договора и их доли - список целиком: не названный отдел уходит из договора.

    Список отделов - это и поле «Отдел»: его перемена пишется в «Историю» как
    правка поля, видимая всем, кому открыт договор, а суммы - отдельным
    событием долей, только тем, кому доли открыты.
    """
    if not departments_editable(access):
        raise PermissionError("Доли отделов распределяет администратор или начальник отдела с полным правом на договоры")
    registry, contract, people, co = _load(session, workspace, access, contract_id, for_update=True)
    _check_write(contract, registry, access, people)
    unit = _unit(unit_raw)
    current = {row.department_id: row for row in contract.department_rows}
    wanted: list[tuple[Department, Decimal | None, Decimal | None]] = []
    seen: set[uuid.UUID] = set()
    for item in items or []:
        try:
            department_id = uuid.UUID(str((item or {}).get("department_id")))
        except ValueError as exc:
            raise SharesError("Отдел указан неверно") from exc
        department = registry.departments.get(department_id)
        if department is None or (department.archived_at is not None and department_id not in current):
            raise SharesError("Такого отдела нет")
        if department_id in seen:
            raise SharesError(f"Отдел {department.code} назван дважды")
        seen.add(department_id)
        amount, percent = _read(item.get("value"), unit, f"Доля отдела {department.code}")
        wanted.append((department, amount, percent))
    values = [amount if unit == "amount" else percent for _dep, amount, percent in wanted]
    _check_sum(contract.amount, unit, [value for value in values if value is not None], "Доли отделов")
    _check_departments_kept(access, registry, contract, [department for department, _a, _p in wanted], actor.user_id)
    list_before = [str(item) for item in contract.department_ids]
    before = {
        str(department_id): _snapshot(contract.amount, row.share_amount, row.share_percent)
        for department_id, row in current.items()
    }
    rows: list[ContractDepartment] = []
    for index, (department, amount, percent) in enumerate(wanted):
        row = current.get(department.id) or ContractDepartment(department_id=department.id, added_by=actor.user_id)
        row.share_amount = amount
        row.share_percent = percent
        row.position = index
        rows.append(row)
    contract.department_rows = rows
    session.flush()
    list_after = [str(item) for item in contract.department_ids]
    after = {
        str(department.id): _snapshot(contract.amount, amount, percent) for department, amount, percent in wanted
    }
    if before != after or list_before != list_after:
        _finish(session, registry, contract, actor, ["department"] if list_before != list_after else [])
        if list_before != list_after:
            # Поле «Отдел» поменялось - это видят все, кому открыт договор.
            history.write(
                session,
                workspace,
                kind="contract.update",
                entity="contract",
                entity_id=contract.id,
                title=_history_title(
                    registry,
                    {"department": list_before},
                    {"department": list_after},
                    f"договор {contract.number or ''}".strip(),
                ),
                before={"department": list_before},
                after={"department": list_after},
                actor=actor.email,
            )
        if before != after:
            codes = ", ".join(department.code for department, _a, _p in wanted) or "-"
            history.write(
                session,
                workspace,
                kind=DEPARTMENTS_KIND,
                entity="contract",
                entity_id=contract.id,
                title=f"договор {contract.number or ''} · отделы в договоре: {codes}".replace("  ", " "),
                before={"department_shares": before},
                after={"department_shares": after},
                actor=actor.email,
            )
    co = [department.id for department, _a, _p in wanted]
    return _out(session, registry, access, contract, people, co)


__all__ = [
    "BookShare",
    "DEPARTMENTS_KIND",
    "PEOPLE_KIND",
    "SharesError",
    "UNITS",
    "all_visible",
    "book_share",
    "book_shares",
    "co_departments",
    "department_share",
    "departments_editable",
    "departments_open",
    "heads_contract",
    "hidden_history",
    "of_contract",
    "people_scope",
    "set_departments",
    "set_people",
]
