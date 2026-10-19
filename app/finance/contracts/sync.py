"""Сверка реестра с внешним источником - шлюз для людей и программ без кода KORT.

29.09.2026: коллега со своим Claude и доступом к книгам Google должен уметь
актуализировать реестр «как живой пользователь», не зная устройства
программы. Щёлкать лист Univer робот умеет плохо (холст, кириллица без
нажатия клавиш), а голый API требует знать, как сопоставить строку с
договором и в каком виде слать значения. Здесь всё это в одном месте.

Строка источника - номер, заказчик и значения по названиям колонок (как в
«Настроить реестр») или по ключам полей. Для каждой строки:

* **сопоставление без угадывания** - по номеру (без «№», пробелов и
  регистра), при нескольких договорах с таким номером - ещё и по заказчику.
  Не сошлось однозначно - строка не трогается, в отчёте «неоднозначно»;
* **поле за полем** - «совпадает», «заполнить» (в KORT пусто), «расходится»
  (в KORT другое - по умолчанию НЕ меняется: решает человек),
  «изменить» (только с `overwrite="all"`). Пустое в источнике не стирает
  ничего;
* **нет в KORT** - «завести» (если `create`).

По умолчанию - пробный прогон: отчёт, в базу ничего. С `apply` правки идут
тем же путём, что правка в карточке (`service.patch` / `service.create`):
закрытые списки, права отдела, история договора с «Вернуть». Строка, которую
сервер не принял, откатывается целиком и попадает в отчёт с причиной;
остальные строки это не останавливает.
"""
from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history
from app.finance.contracts import service
from app.finance.contracts.fields import (
    COMPUTED_FIELDS,
    DERIVED_FIELDS,
    LIVE_FIELDS,
    SNAPSHOT_FIELDS,
    SUMMARY_FIELDS,
    number_key,
    party_key,
)
from app.finance.contracts.models import Contract, ContractPerson, Employee
from app.finance.layout import norm
from app.finance.models import Counterparty, Workspace
from app.finance.service import FinanceError

MAX_ROWS = 2000
#: Поля, правка которых спрашивает «опечатка или с даты»: сверка - это
#: исправление по источнику, а не соглашение с даты.
_MODE_KEYS = {"executor", "customer", "amount"}
_READ_ONLY = set(SNAPSHOT_FIELDS) | set(LIVE_FIELDS) | set(SUMMARY_FIELDS) | set(DERIVED_FIELDS) | set(COMPUTED_FIELDS)
OVERWRITE = ("empty", "all")


class SyncError(FinanceError):
    pass


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == []


def _field_keys(registry: service.Registry) -> dict[str, str]:
    """Название колонки (любое из имён поля) или ключ → ключ поля."""
    out: dict[str, str] = {}
    for item in registry.fields:
        out[norm(item.key)] = item.key
        out[norm(item.title)] = item.key
        for name in item.names or []:
            out.setdefault(norm(name), item.key)
    return out


def _party_names(session: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    return dict(session.execute(sa.select(Counterparty.id, Counterparty.name).where(Counterparty.id.in_(ids))).all())


def _people_names(session: Session, contract_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    out: dict[uuid.UUID, list[str]] = {}
    if not contract_ids:
        return out
    rows = session.execute(
        sa.select(ContractPerson.contract_id, Employee.full_name)
        .join(Employee, Employee.id == ContractPerson.employee_id)
        .where(ContractPerson.contract_id.in_(contract_ids))
        .order_by(ContractPerson.position)
    )
    for contract_id, name in rows:
        out.setdefault(contract_id, []).append(name)
    return out


def _current(registry: service.Registry, contract: Contract, key: str, parties: dict, people: dict) -> Any:
    """Что стоит в договоре - в виде, сравнимом с текстом источника."""
    raw = service.value_of(contract, key)
    if key == "amount" and raw is None:
        # Сумма словами («20% по разовым») живёт в «Условии суммы».
        return contract.amount_terms or ""
    if key in ("executor", "customer"):
        return parties.get(getattr(contract, f"{key}_id"), "") if raw else ""
    if key in ("type", "subject", "status", "economic_role"):
        value = registry.values.get(getattr(contract, f"{key}_id")) if raw else None
        return value.value if value else ""
    if key == "department":
        return ", ".join(
            registry.departments[item].code for item in contract.department_ids if item in registry.departments
        )
    if key == "people":
        return ", ".join(people.get(contract.id, []))
    field = registry.field_by_key.get(key)
    if field is not None and not field.system and field.type in ("list", "multi_list"):
        ids = raw if isinstance(raw, list) else [raw] if raw else []
        names = [registry.values[uuid.UUID(str(item))].value for item in ids if uuid.UUID(str(item)) in registry.values]
        return ", ".join(names)
    return raw


def _codes(text: Any) -> set[str]:
    return {norm(part) for part in re.split(r"[,;\n/]+", str(text or "")) if part.strip()}


def _same(registry: service.Registry, key: str, current: Any, incoming: Any) -> bool:
    """Совпадают ли значения по смыслу: даты - датой, суммы - числом, текст - без регистра и лишних пробелов."""
    field = registry.field_by_key.get(key)
    kind = field.type if field is not None else "text"
    try:
        if kind == "date" or key in service.DATE_KEYS:
            left = date.fromisoformat(str(current)[:10]) if current else None
            right = service.read_date(incoming, field=key)
            return left == right
        if kind in ("money", "number") or key == "amount":
            right, terms = service.read_money(incoming, field=key)
            if right is None:
                return norm(str(current or "")) == norm(terms)
            left = Decimal(str(current)) if not _blank(current) else None
            return left == right
    except (FinanceError, ArithmeticError, ValueError):
        return False
    if key in ("executor", "customer"):
        return party_key(current) == party_key(incoming)
    if key == "number":
        return number_key(current) == number_key(incoming)
    if key == "department":
        # «ЮО, HR» и «HR, ЮО» - одни и те же отделы: список, а не текст.
        return _codes(current) == _codes(incoming)
    if isinstance(current, bool):
        return current == (str(incoming).strip().lower() in ("1", "true", "да", "yes", "истина"))
    return norm(str(current or "")) == norm(str(incoming or ""))


def run(
    session: Session,
    workspace: Workspace,
    access: service.Access,
    actor: service.Actor,
    rows: list[dict[str, Any]],
    *,
    source: str = "",
    apply: bool = False,
    overwrite: str = "empty",
    create: bool = True,
) -> dict[str, Any]:
    if overwrite not in OVERWRITE:
        raise SyncError("overwrite: «empty» - только пустое, «all» - и расходящееся")
    if not isinstance(rows, list) or not rows:
        raise SyncError("Строк нет")
    if len(rows) > MAX_ROWS:
        raise SyncError(f"За раз - не больше {MAX_ROWS} строк")
    if apply and not access.edit:
        raise PermissionError("Правка договоров вам не открыта")
    registry = service.Registry(session, workspace)
    keys = _field_keys(registry)
    everything = list(
        session.scalars(sa.select(Contract).where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None)))
    )
    # Только договоры, открытые этому человеку, - как в реестре. До 01.10.2026
    # сверка брала все договоры компании и отдавала в отчёте их значения, в
    # том числе скрытых от человека полей: пробный прогон с номером открывал
    # договор другого отдела сотруднику с правом правки своего.
    people_now = service.people_of(session, [item.id for item in everything])
    contracts = [
        item for item in everything if service.visible_to(item, registry, access, people_now.get(item.id, []))
    ]
    closed_numbers = {number_key(item.number) for item in everything} - {number_key(item.number) for item in contracts}
    by_number: dict[str, list[Contract]] = {}
    for contract in contracts:
        by_number.setdefault(number_key(contract.number), []).append(contract)
    parties = _party_names(session, {c.customer_id for c in contracts if c.customer_id} | {c.executor_id for c in contracts if c.executor_id})

    report: list[dict[str, Any]] = []
    totals = {"rows": len(rows), "matched": 0, "create": 0, "ambiguous": 0, "errors": 0,
              "same": 0, "fill": 0, "change": 0, "conflict": 0, "applied": 0, "closed": 0}
    plans: list[tuple[int, Contract | None, dict[str, Any]]] = []

    for index, row in enumerate(rows):
        row = row if isinstance(row, dict) else {}
        line: dict[str, Any] = {"row": index, "number": str(row.get("number") or ""), "customer": str(row.get("customer") or "")}
        report.append(line)
        if not number_key(row.get("number")):
            line.update(status="error", error="Нет номера договора")
            totals["errors"] += 1
            continue
        values: dict[str, Any] = {}
        unknown: list[str] = []
        for name, value in (row.get("values") or {}).items():
            key = keys.get(norm(str(name)))
            if key is None:
                unknown.append(str(name))
                continue
            if key in _READ_ONLY:
                unknown.append(f"{name} (считается само - только чтение)")
                continue
            if key in access.hidden:
                # Скрытое поле не читается и не пишется - и не называется
                # своим заголовком, как в реестре.
                unknown.append(f"{name} (поле вам не открыто)")
                continue
            values[key] = value
        if row.get("customer") and "customer" not in values:
            values["customer"] = row["customer"]
        if unknown:
            line["unknown"] = unknown
        found = by_number.get(number_key(row["number"]), [])
        if len(found) > 1 and row.get("customer"):
            found = [item for item in found if party_key(parties.get(item.customer_id, "")) == party_key(row["customer"])]
        if len(found) > 1:
            line.update(status="ambiguous", error="Номер у нескольких договоров - укажите заказчика точнее",
                        candidates=[str(item.id) for item in found])
            totals["ambiguous"] += 1
            continue
        if not found and number_key(row["number"]) in closed_numbers:
            # Договор с этим номером есть, но человеку не открыт: не заводить
            # второй такой же и не показывать чужой. Что номер занят, реестр и
            # так говорит замечанием «номер уже есть у … - договор вам не открыт».
            line.update(status="closed", error="Договор с этим номером вам не открыт - строка не тронута")
            totals["closed"] += 1
            continue
        if not found:
            if not create:
                line.update(status="missing")
                continue
            clean = {key: value for key, value in values.items() if not _blank(value)}
            clean["number"] = row["number"]
            line.update(status="create", fields=[{"key": k, "incoming": v, "action": "fill"} for k, v in clean.items()])
            totals["create"] += 1
            plans.append((index, None, clean))
            continue
        contract = found[0]
        people = _people_names(session, [contract.id])
        line.update(status="matched", contract_id=str(contract.id))
        totals["matched"] += 1
        fields: list[dict[str, Any]] = []
        planned: dict[str, Any] = {}
        for key, incoming in values.items():
            if key == "number":
                continue
            current = _current(registry, contract, key, parties, people)
            item = {"key": key, "title": registry.field_by_key[key].title if key in registry.field_by_key else key,
                    "current": current if not isinstance(current, (Decimal,)) else str(current), "incoming": incoming}
            if _blank(incoming):
                item["action"] = "same" if _blank(current) else "keep"
            elif _same(registry, key, current, incoming):
                item["action"] = "same"
            elif _blank(current):
                item["action"] = "fill"
                planned[key] = incoming
            elif overwrite == "all":
                item["action"] = "change"
                planned[key] = incoming
            else:
                item["action"] = "conflict"
            if item["action"] in totals:
                totals[item["action"]] += 1
            fields.append(item)
        line["fields"] = fields
        if planned:
            plans.append((index, contract, planned))

    if apply:
        for index, contract, values in plans:
            line = report[index]
            try:
                with session.begin_nested():
                    if contract is None:
                        made = service.create(session, workspace, access, actor, values, source="app")
                        line["contract_id"] = str(made.id)
                    else:
                        mode = service.Mode(kind="fix") if _MODE_KEYS & set(values) else None
                        service.patch(session, workspace, access, actor, contract.id, values, known_seq=None, mode=mode)
                line["applied"] = True
                totals["applied"] += 1
            except Exception as exc:  # noqa: BLE001 - строку, которую сервер не принял, описываем словами
                line["applied"] = False
                line["error"] = str(exc) or type(exc).__name__
                totals["errors"] += 1
        history.write(
            session, workspace, kind="contract.sync", entity="contracts",
            title=(
                f"сверка с источником{f' «{source}»' if source else ''}: строк {totals['rows']}, "
                f"применено {totals['applied']}, расходится {totals['conflict']}, ошибок {totals['errors']}"
            ),
            after={key: value for key, value in totals.items()},
        )
    return {"dry_run": not apply, "overwrite": overwrite, "summary": totals, "rows": report}


__all__ = ["MAX_ROWS", "SyncError", "run"]
