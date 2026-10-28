"""Участники: поиск по любому написанию и БИН, досье, двойники, сведение, БИН из источников.

Почему так
──────────
В таблицах копии прода клиент - это строка «Заказчик», а в соседних колонках - как его
пишет банк и как 1С; на 19.10 у 96 клиентов было три и больше написаний, а в
152 строках колонку банка не заполнили, и оплаты не находили строку. В KORT
та же болезнь: 24.10 на проде 17 клиентов заведены дважды («ТОО Х» и «Х») и
ни одного БИН. Ядро лечит это одним правилом: **участник - это БИН, имя -
одно из написаний**.

* Поиск идёт по имени, полному наименованию, всем написаниям и началу БИН.
* Двойники предлагаются, а не сводятся сами: тот же БИН; то же имя без
  кавычек и регистра, где у одного формы («ТОО», «ИП») нет вовсе. «ТОО Альфа» и
  «ИП Альфа» - разные лица, их не предлагаем.
* Сводит человек (владелец или администратор). Сведение переносит всё, что
  ссылается на участника, и оставляет точку восстановления: «Восстановление» в
  кабинете возвращает как было (`undo_merge`).
* БИН из источников - только предложением: 1С (`party_bin` документа,
  сопоставленного с договором или однозначно с именем), строка банка в сводке
  («… БИН 260540028486»). Ставит человек; «не тот» запоминается, и номер больше
  не предлагается.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history
from app.finance.contracts.fields import bump, party_key
from app.finance.contracts.models import Contract, CounterpartyName, GroupEntity, ListValue
from app.finance.contracts.service import Access, Actor, Registry, people_of, visible_to
from app.finance import packs
from app.finance.core import book as book_module
from app.finance.core import tax
from app.finance.core.models import EntityTaxPeriod
from app.finance.layout import norm
from app.finance.models import Account, Counterparty, Invoice, Operation, Recurrence, Workspace
from app.finance.onec.models import OnecBase, OnecDocument
from app.finance.service import FinanceError

#: Источники написания - как у `counterparty_names.source`.
SOURCES = ("registry", "bank", "1c", "manual")
SOURCE_TITLES = {"registry": "реестр", "bank": "банк", "1c": "1С", "manual": "вручную"}

_FORM = re.compile(r"^\s*(тоо|ип|ао|оао|зао|ооо|чк|кх|пк|гу|ксп|филиал)\b\.?", re.IGNORECASE)
_LONG_FORMS = (
    (re.compile(r"товарищество\s+с\s+ограниченной\s+ответственностью", re.IGNORECASE), "тоо"),
    (re.compile(r"индивидуальный\s+предприниматель", re.IGNORECASE), "ип"),
    (re.compile(r"акционерное\s+общество", re.IGNORECASE), "ао"),
)
_QUOTES = re.compile(r"[«»\"'“”„]")


class ParticipantError(FinanceError):
    """Отказ с причиной для человека."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expand(name: str) -> str:
    text = _QUOTES.sub(" ", str(name or ""))
    for pattern, short in _LONG_FORMS:
        text = pattern.sub(short, text)
    return " ".join(text.split())


def form_of(name: str) -> str:
    """Организационная форма в начале имени: «тоо», «ип»…; нет - пусто."""
    found = _FORM.match(_expand(name))
    return found.group(1).lower() if found else ""


def bare_key(name: str) -> str:
    """Имя без формы, кавычек, регистра и знаков: «ТОО "Бергауф Астана"» → «бергауфастана»."""
    return party_key(_FORM.sub("", _expand(name)))


def _plural(count: int, one: str, few: str, many: str) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    return few if 2 <= tail <= 4 else many


def _contracts_word(count: int) -> str:
    return f"{count} {_plural(count, 'договор', 'договора', 'договоров')}"


def _require_edit(access: Access) -> None:
    if not access.edit:
        raise PermissionError("Правка реестра вам не открыта - только просмотр")


def _require_setup(access: Access, who: str = "Сводит участников") -> None:
    if not access.setup:
        raise PermissionError(f"{who} владелец или администратор")


def _party(session: Session, workspace: Workspace, party_id: uuid.UUID) -> Counterparty:
    party = session.get(Counterparty, party_id)
    if party is None or party.workspace_id != workspace.id:
        raise ParticipantError("Такого участника нет")
    return party


def _schema_changed(session: Session, workspace: Workspace) -> None:
    """Стороны договоров - часть схемы реестра: клиент перечитает стороны."""
    bump(session, workspace.id, "schema")


def _touch(session: Session, workspace: Workspace, contract_ids: Iterable[uuid.UUID]) -> None:
    """Договоры, у которых сменилась сторона, - с новым номером изменения: лист и карточки увидят."""
    ids = list(set(contract_ids))
    if not ids:
        return
    seq = bump(session, workspace.id, "contracts")
    session.execute(sa.update(Contract).where(Contract.id.in_(ids)).values(seq=seq))


# ── Поиск ────────────────────────────────────────────────────────────────────


def search(session: Session, workspace: Workspace, access: Access, q: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Участники по любому написанию, полному наименованию или началу БИН."""
    text = (q or "").strip()
    if len(text) < 2:
        return []
    digits = packs.digits(text)
    like = f"%{text}%"
    key = party_key(text)
    # Имя - по `normalized_name` (нижний регистр Python): ILIKE в SQLite не
    # опускает кириллицу, и «без догов» не находило «ТОО Без договоров».
    query = (
        sa.select(Counterparty)
        .where(
            Counterparty.workspace_id == workspace.id,
            Counterparty.archived_at.is_(None),
            sa.or_(
                Counterparty.normalized_name.like(f"%{norm(text)}%"),
                Counterparty.name.ilike(like),
                Counterparty.full_name.ilike(like),
                Counterparty.id.in_(
                    sa.select(CounterpartyName.counterparty_id).where(
                        CounterpartyName.workspace_id == workspace.id,
                        sa.or_(CounterpartyName.name.ilike(like), CounterpartyName.normalized.like(f"%{key}%") if key else sa.false()),
                    )
                ),
                Counterparty.bin.like(f"{digits}%") if len(digits) >= 4 else sa.false(),
            ),
        )
        .limit(200)
    )
    parties = list(session.scalars(query))
    if not parties:
        return []
    counts = _contract_counts(session, workspace, access, [party.id for party in parties])
    own = _own_entities(session, workspace)

    def rank(party: Counterparty) -> tuple[int, int, str]:
        if digits and party.bin == digits:
            first = 0
        elif party_key(party.name) == key:
            first = 1
        elif party.name.lower().startswith(text.lower()):
            first = 2
        else:
            first = 3
        return first, -counts.get(party.id, 0), party.name.lower()

    parties.sort(key=rank)
    regimes: dict[uuid.UUID, list[EntityTaxPeriod]] = {}
    return [_hit(session, party, own, counts, regimes) for party in parties[:limit]]


def _own_entities(session: Session, workspace: Workspace) -> dict[uuid.UUID, GroupEntity]:
    return {
        item.counterparty_id: item
        for item in session.scalars(
            sa.select(GroupEntity).where(GroupEntity.workspace_id == workspace.id, GroupEntity.archived_at.is_(None))
        )
    }


#: Организационная форма внутри названия счёта: «Kaspi Юр. ТОО "Тумар Офис"»,
#: «Депозит TOO "…"» (латиницей - так тоже пишут).
_FORM_INSIDE = re.compile(r"(?:^|[\s.,(])(тоо|too|ип|ао|llp|ооо)(?=[\s\"«“'])", re.IGNORECASE)


def legal_tail(name: str) -> str:
    """Имя после организационной формы, ключом: «Kaspi Юр. ТОО "Тумар Офис"» → «тумарофис».

    «Товарищество с ограниченной ответственностью» - тоже форма. Формы нет -
    пусто: имя без формы может быть чем угодно, хоть «Пополнение».
    """
    text = _expand(name)
    forms = list(_FORM_INSIDE.finditer(text))
    return party_key(text[forms[-1].end(1):]) if forms else ""


def entities_of_accounts(session: Session, workspace: Workspace, names: Iterable[str]) -> dict[str, GroupEntity]:
    """Чьё наше юрлицо названо в счёте: «Kaspi Юр. ТОО "Тумар Офис"» → Тумар Офис.

    Имя после формы сравнивается целиком - с именем, полным наименованием и
    написаниями нашего юрлица: «Тумар» внутри «Тумар Офис» - не Тумар, а «ТОО
    "Тумар Маркетинг"» без такого юрлица - никто. Ни одного или два
    юрлица под одно имя - предложения нет, решает человек.
    """
    own = _own_entities(session, workspace)
    if not own:
        return {}
    texts: dict[uuid.UUID, list[str]] = {party_id: [entity.full_name] for party_id, entity in own.items()}
    for party in session.scalars(sa.select(Counterparty).where(Counterparty.id.in_(list(own)))):
        texts[party.id] += [party.name, party.full_name or ""]
    for party_id, alias in session.execute(
        sa.select(CounterpartyName.counterparty_id, CounterpartyName.name).where(
            CounterpartyName.counterparty_id.in_(list(own))
        )
    ):
        texts[party_id].append(alias)
    keys: dict[str, set[uuid.UUID]] = {}
    for party_id, items in texts.items():
        for text in items:
            key = bare_key(text)
            if key:
                keys.setdefault(key, set()).add(party_id)
    out: dict[str, GroupEntity] = {}
    for name in names:
        tail = legal_tail(name or "")
        hits = keys.get(tail, set()) if tail else set()
        if len(hits) == 1:
            out[name] = own[next(iter(hits))]
    return out


def _hit(
    session: Session,
    party: Counterparty,
    own: dict[uuid.UUID, GroupEntity],
    counts: dict[uuid.UUID, int],
    regimes: dict[uuid.UUID, list[EntityTaxPeriod]],
) -> dict[str, Any]:
    """Строка списка участников. У нашего юрлица - режим на сегодня."""
    entity = own.get(party.id)
    out: dict[str, Any] = {
        "id": str(party.id),
        "name": party.name,
        "full_name": party.full_name,
        "bin": party.bin,
        "kind": party.kind,
        "own": (entity.code or party.name) if entity is not None else None,
        "contracts": counts.get(party.id, 0),
    }
    if entity is not None:
        period = tax.regime_on(session, party.id, date.today(), regimes)
        out["regime"] = tax.period_out(period) if period is not None else None
    return out


def _live_parties(session: Session, workspace: Workspace) -> list[Counterparty]:
    return list(
        session.scalars(
            sa.select(Counterparty).where(
                Counterparty.workspace_id == workspace.id,
                Counterparty.archived_at.is_(None),
                Counterparty.merged_into_id.is_(None),
            )
        )
    )


#: Списки «Участников»: у кого есть договоры (и наши юрлица), наши юрлица, без БИН.
SHOWS = ("all", "own", "no_bin")


def listing(
    session: Session, workspace: Workspace, access: Access, *, q: str = "", show: str = "all", limit: int = 60
) -> dict[str, Any]:
    """Участники списком: чаще всего в договорах - первыми. С `q` - поиск по любому написанию.

    Без поиска в списке те, у кого есть открытые человеку договоры, и наши
    юрлица (в конце): контрагент, оставшийся без договоров, находится поиском.
    """
    if show not in SHOWS:
        raise ParticipantError("Список: все, наши юрлица или без БИН")
    text = (q or "").strip()
    if text:
        hits = search(session, workspace, access, text, limit=200)
        if show == "own":
            hits = [item for item in hits if item["own"] is not None]
        elif show == "no_bin":
            hits = [item for item in hits if not item["bin"]]
        return {"items": hits[:limit], "total": len(hits)}
    own = _own_entities(session, workspace)
    counts = _contract_counts(session, workspace, access)
    chosen = [party for party in _live_parties(session, workspace) if counts.get(party.id) or party.id in own]
    if show == "own":
        chosen = [party for party in chosen if party.id in own]
        chosen.sort(key=lambda party: (own[party.id].position, (own[party.id].code or party.name).lower()))
    else:
        if show == "no_bin":
            chosen = [party for party in chosen if not party.bin]
        # Наши юрлица - стороны почти всех договоров и встали бы первыми; ищут
        # же клиентов. Юрлица - в конце и своей плиткой.
        chosen.sort(key=lambda party: (party.id in own, -counts.get(party.id, 0), party.name.lower()))
    regimes: dict[uuid.UUID, list[EntityTaxPeriod]] = {}
    return {"items": [_hit(session, party, own, counts, regimes) for party in chosen[:limit]], "total": len(chosen)}


def overview(session: Session, workspace: Workspace, access: Access) -> dict[str, Any]:
    """Числа плиток «Участников» и что человеку можно: править, сводить."""
    own = _own_entities(session, workspace)
    counts = _contract_counts(session, workspace, access)
    shown = [party for party in _live_parties(session, workspace) if counts.get(party.id) or party.id in own]
    book, _error = book_module.current(session, workspace.id, wait=0)
    return {
        "all": len(shown),
        "own": sum(1 for party in shown if party.id in own),
        "no_bin": sum(1 for party in shown if not party.bin),
        "twins": len(duplicates(session, workspace, access)),
        "proposals": len(bin_proposals(session, workspace, access, book)),
        "book": book is not None,
        "access": {"edit": bool(access.edit), "setup": bool(access.setup)},
    }


def _visible_contracts(
    session: Session, workspace: Workspace, access: Access, party_ids: Iterable[uuid.UUID] | None = None
) -> list[Contract]:
    """Живые договоры, открытые человеку. `party_ids` - только с этими сторонами; `None` - все."""
    query = sa.select(Contract).where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None))
    if party_ids is not None:
        ids = list(set(party_ids))
        if not ids:
            return []
        query = query.where(sa.or_(Contract.customer_id.in_(ids), Contract.executor_id.in_(ids)))
    contracts = list(session.scalars(query))
    registry = Registry(session, workspace)
    people = people_of(session, [item.id for item in contracts])
    return [item for item in contracts if visible_to(item, registry, access, people.get(item.id, []))]


def _contract_counts(
    session: Session, workspace: Workspace, access: Access, party_ids: Iterable[uuid.UUID] | None = None
) -> dict[uuid.UUID, int]:
    counts: dict[uuid.UUID, int] = {}
    for contract in _visible_contracts(session, workspace, access, party_ids):
        for party_id in {contract.customer_id, contract.executor_id} - {None}:
            counts[party_id] = counts.get(party_id, 0) + 1
    return counts


# ── Досье ────────────────────────────────────────────────────────────────────


def _value(values: dict[uuid.UUID, ListValue], value_id: uuid.UUID | None) -> dict[str, Any] | None:
    value = values.get(value_id) if value_id else None
    if value is None:
        return None
    return {"id": str(value.id), "value": value.value, "meaning": value.meaning or {}}


def aliases_of(session: Session, party_id: uuid.UUID) -> list[dict[str, Any]]:
    return [
        {"id": str(item.id), "name": item.name, "source": item.source, "source_title": SOURCE_TITLES.get(item.source, item.source)}
        for item in session.scalars(
            sa.select(CounterpartyName)
            .where(CounterpartyName.counterparty_id == party_id)
            .order_by(CounterpartyName.source, CounterpartyName.created_at)
        )
    ]


def dossier(session: Session, workspace: Workspace, access: Access, party_id: uuid.UUID) -> dict[str, Any]:
    """Всё об участнике: имена, БИН, договоры у всех наших юрлиц, что требует внимания."""
    party = _party(session, workspace, party_id)
    if party.merged_into_id is not None:
        # Старая ссылка на сведённого - туда, куда его свели.
        return {"merged_into": str(party.merged_into_id), "name": party.name}
    registry = Registry(session, workspace)
    contracts = _visible_contracts(session, workspace, access, [party.id])
    values = {item.id: item for item in session.scalars(sa.select(ListValue).where(ListValue.workspace_id == workspace.id))}
    other_ids = {pid for item in contracts for pid in (item.customer_id, item.executor_id) if pid and pid != party.id}
    names = {item.id: item.name for item in session.scalars(sa.select(Counterparty).where(Counterparty.id.in_(other_ids)))} if other_ids else {}
    own = {item.counterparty_id: item for item in session.scalars(sa.select(GroupEntity).where(GroupEntity.workspace_id == workspace.id))}
    entity = own.get(party.id)
    groups: dict[str, dict[str, Any]] = {}
    for contract in sorted(contracts, key=lambda item: (item.signed_at or datetime.min.date(), item.number or ""), reverse=True):
        as_customer = contract.customer_id == party.id
        other_id = contract.executor_id if as_customer else contract.customer_id
        other_entity = own.get(other_id) if other_id else None
        group_key = str(other_id) if other_id else "none"
        group = groups.setdefault(
            group_key,
            {
                "party_id": str(other_id) if other_id else None,
                "name": (other_entity.code or names.get(other_id, "")) if other_entity else names.get(other_id, "") if other_id else "",
                "full_name": names.get(other_id, "") if other_id else "",
                "own": other_entity is not None,
                "contracts": [],
            },
        )
        status = _value(values, contract.status_id)
        group["contracts"].append(
            {
                "id": str(contract.id),
                "number": contract.number,
                "signed_at": contract.signed_at.isoformat() if contract.signed_at else None,
                "role": "customer" if as_customer else "executor",
                "type": _value(values, contract.type_id),
                "subject": _value(values, contract.subject_id),
                "status": status,
                "billing": contract.billing,
                "amount": format(contract.amount, "f") if contract.amount is not None else None,
                "end_date": contract.end_date.isoformat() if contract.end_date else None,
                "departments": [registry.departments[dep].code for dep in contract.department_ids if dep in registry.departments],
            }
        )
    issues: list[dict[str, Any]] = []
    if not party.bin:
        issues.append({"code": "no_bin", "text": "БИН или ИИН не указан"})
    twins = [group for group in duplicates(session, workspace, access) if any(item["id"] == str(party.id) for item in group["members"])]
    for group in twins:
        others = [item["name"] for item in group["members"] if item["id"] != str(party.id)]
        issues.append({"code": "twin", "text": f"Похож на {', '.join('«' + name + '»' for name in others)}", "group": group})
    # Сводка - уже прочитанная: досье не ждёт Google.
    book, _error = book_module.current(session, workspace.id, wait=0) if not party.bin else (None, "")
    proposals = (
        [item for item in bin_proposals(session, workspace, access, book) if item["party_id"] == str(party.id)]
        if not party.bin
        else []
    )
    out: dict[str, Any] = {
        "id": str(party.id),
        "name": party.name,
        "full_name": party.full_name,
        "bin": party.bin,
        "kind": party.kind,
        "role": party.role,
        "aliases": aliases_of(session, party.id),
        "groups": list(groups.values()),
        "contracts": len(contracts),
        "issues": issues,
        "proposals": proposals,
        "own": None,
    }
    if entity is not None:
        out["own"] = {
            "code": entity.code,
            "full_name": entity.full_name,
            "archived": entity.archived_at is not None,
            "tax": [tax.period_out(item) for item in tax.periods(session, party.id)],
            "accounts": [
                {"id": str(item.id), "name": item.name, "number": item.number}
                for item in session.scalars(
                    sa.select(Account).where(Account.workspace_id == workspace.id, Account.group_entity_id == party.id)
                )
            ],
            "bases": [
                {"id": str(item.id), "title": item.title, "url": item.url, "state": item.state}
                for item in session.scalars(
                    sa.select(OnecBase).where(OnecBase.workspace_id == workspace.id, OnecBase.group_entity_id == party.id)
                )
            ],
        }
    return out


# ── БИН и имена ──────────────────────────────────────────────────────────────


def owner_of_bin(session: Session, workspace: Workspace, number: str, *, but: uuid.UUID | None = None) -> Counterparty | None:
    query = sa.select(Counterparty).where(
        Counterparty.workspace_id == workspace.id,
        Counterparty.bin == number,
        Counterparty.archived_at.is_(None),
    )
    if but is not None:
        query = query.where(Counterparty.id != but)
    return session.scalars(query).first()


def set_bin(
    session: Session, workspace: Workspace, access: Access, actor: Actor, party_id: uuid.UUID, raw: str, *, source: str = "manual"
) -> dict[str, Any]:
    """Записать БИН/ИИН участнику. Пусто - снять. Номер чужого участника - отказ с предложением свести."""
    _require_edit(access)
    party = _party(session, workspace, party_id)
    before = {"bin": party.bin, "kind": party.kind}
    if not (raw or "").strip():
        party.bin, party.kind = "", ""
    else:
        checked = packs.country_of(workspace).ids.check(raw, party.name)
        if not checked.ok:
            raise ParticipantError(checked.error)
        other = owner_of_bin(session, workspace, checked.number, but=party.id)
        if other is not None:
            raise ParticipantError(
                f"БИН {checked.number} уже записан у «{other.name}» - это, похоже, один участник: сведите их"
            )
        party.bin, party.kind = checked.number, checked.kind
    entity = session.get(GroupEntity, party.id)
    if entity is not None:
        entity.bin = party.bin
    session.flush()
    _touch(
        session,
        workspace,
        session.scalars(sa.select(Contract.id).where(sa.or_(Contract.customer_id == party.id, Contract.executor_id == party.id))),
    )
    history.write(
        session, workspace, kind="participant.bin", entity="counterparty", entity_id=party.id,
        title=(f"{party.name}: БИН {party.bin}" if party.bin else f"{party.name}: БИН снят")
        + (f" (из источника: {SOURCE_TITLES.get(source, source)})" if source != "manual" else ""),
        before=before, after={"bin": party.bin, "kind": party.kind, "source": source}, actor=actor.email,
    )
    return {"id": str(party.id), "bin": party.bin, "kind": party.kind}


def update(session: Session, workspace: Workspace, access: Access, actor: Actor, party_id: uuid.UUID, data: dict[str, Any]) -> dict[str, Any]:
    """Полное наименование участника. Короткое имя меняется в карточке договора, как и раньше."""
    _require_edit(access)
    party = _party(session, workspace, party_id)
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    if "full_name" in data:
        value = " ".join(str(data["full_name"] or "").split())
        if value != party.full_name:
            before["full_name"], after["full_name"] = party.full_name, value
            party.full_name = value
            if value:
                remember(session, workspace, party.id, value, source="manual")
    if after:
        history.write(
            session, workspace, kind="participant.update", entity="counterparty", entity_id=party.id,
            title=f"{party.name}: полное наименование «{party.full_name or '-'}»",
            before=before, after=after, actor=actor.email,
        )
    return {"id": str(party.id), "full_name": party.full_name}


def remember(session: Session, workspace: Workspace, party_id: uuid.UUID, name: Any, *, source: str) -> bool:
    """Запомнить написание. Повтор ничего не делает; новое - `True`."""
    text = " ".join(str(name or "").split())
    key = party_key(text)
    if not key:
        return False
    exists = session.scalar(
        sa.select(CounterpartyName.id).where(CounterpartyName.counterparty_id == party_id, CounterpartyName.normalized == key)
    )
    if exists is not None:
        return False
    session.add(CounterpartyName(workspace_id=workspace.id, counterparty_id=party_id, name=text, normalized=key, source=source))
    session.flush()
    return True


def add_alias(session: Session, workspace: Workspace, access: Access, actor: Actor, party_id: uuid.UUID, name: str, source: str = "manual") -> dict[str, Any]:
    _require_edit(access)
    party = _party(session, workspace, party_id)
    if source not in SOURCES:
        raise ParticipantError("Источник написания: реестр, банк, 1С или вручную")
    if not remember(session, workspace, party.id, name, source=source):
        raise ParticipantError("Такое написание у участника уже есть")
    history.write(
        session, workspace, kind="participant.alias", entity="counterparty", entity_id=party.id,
        title=f"{party.name}: написание «{name.strip()}» ({SOURCE_TITLES[source]})",
        after={"alias": name.strip(), "source": source}, actor=actor.email,
    )
    return {"aliases": aliases_of(session, party.id)}


def remove_alias(session: Session, workspace: Workspace, access: Access, actor: Actor, party_id: uuid.UUID, alias_id: uuid.UUID) -> dict[str, Any]:
    _require_edit(access)
    party = _party(session, workspace, party_id)
    alias = session.get(CounterpartyName, alias_id)
    if alias is None or alias.counterparty_id != party.id:
        raise ParticipantError("Такого написания у участника нет")
    session.delete(alias)
    session.flush()
    history.write(
        session, workspace, kind="participant.alias.remove", entity="counterparty", entity_id=party.id,
        title=f"{party.name}: написание «{alias.name}» убрано", before={"alias": alias.name, "source": alias.source},
        actor=actor.email,
    )
    return {"aliases": aliases_of(session, party.id)}


# ── Двойники ─────────────────────────────────────────────────────────────────


def _distinct_marks(party: Counterparty) -> set[str]:
    return {str(item) for item in (party.details or {}).get("distinct", [])}


def duplicates(session: Session, workspace: Workspace, access: Access) -> list[dict[str, Any]]:
    """Похоже на одного участника: тот же БИН или то же имя без формы и кавычек.

    Имя без формы совпадает у «ТОО Х» и «Х» - а у «ТОО Х» и «ИП Х» формы
    разные, и это разные лица. Пары, которые человек отметил «это разные»,
    больше не предлагаются.
    """
    parties = list(
        session.scalars(
            sa.select(Counterparty).where(
                Counterparty.workspace_id == workspace.id,
                Counterparty.archived_at.is_(None),
                Counterparty.merged_into_id.is_(None),
            )
        )
    )
    by_id = {party.id: party for party in parties}
    aliases: dict[uuid.UUID, list[str]] = {}
    for party_id, name in session.execute(
        sa.select(CounterpartyName.counterparty_id, CounterpartyName.name).where(CounterpartyName.workspace_id == workspace.id)
    ):
        if party_id in by_id:
            aliases.setdefault(party_id, []).append(name)

    parent = {party.id: party.id for party in parties}

    def find(item: uuid.UUID) -> uuid.UUID:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    reasons: dict[frozenset[uuid.UUID], str] = {}

    def join(a: uuid.UUID, b: uuid.UUID, why: str) -> None:
        if a == b:
            return
        pa, pb = by_id[a], by_id[b]
        if str(b) in _distinct_marks(pa) or str(a) in _distinct_marks(pb):
            return
        if pa.bin and pb.bin and pa.bin != pb.bin:
            return
        reasons.setdefault(frozenset((a, b)), why)
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    by_bin: dict[str, list[uuid.UUID]] = {}
    by_bare: dict[str, list[tuple[uuid.UUID, str]]] = {}
    for party in parties:
        if party.bin:
            by_bin.setdefault(party.bin, []).append(party.id)
        for name in [party.name, *aliases.get(party.id, [])]:
            key = bare_key(name)
            if len(key) >= 3:
                by_bare.setdefault(key, []).append((party.id, form_of(name)))
    for ids in by_bin.values():
        for other in ids[1:]:
            join(ids[0], other, "тот же БИН")
    for items in by_bare.values():
        for index, (a, form_a) in enumerate(items):
            for b, form_b in items[index + 1:]:
                if a == b:
                    continue
                if form_a and form_b and form_a != form_b:
                    continue  # «ТОО Х» и «ИП Х» - разные лица
                join(a, b, "одно имя без формы и кавычек")

    clusters: dict[uuid.UUID, list[uuid.UUID]] = {}
    for party in parties:
        clusters.setdefault(find(party.id), []).append(party.id)
    groups = [ids for ids in clusters.values() if len(ids) > 1]
    if not groups:
        return []
    counts = _contract_counts(session, workspace, access, [pid for ids in groups for pid in ids])
    own = set(session.scalars(sa.select(GroupEntity.counterparty_id).where(GroupEntity.workspace_id == workspace.id)))
    out = []
    for ids in groups:
        members = sorted(ids, key=lambda pid: (not by_id[pid].bin, pid not in own, -counts.get(pid, 0), by_id[pid].position, by_id[pid].name))
        why = sorted({reason for pair, reason in reasons.items() if pair <= set(ids)})
        out.append(
            {
                "key": "-".join(sorted(str(pid) for pid in ids)),
                "why": why,
                "keep": str(members[0]),
                "members": [
                    {
                        "id": str(pid),
                        "name": by_id[pid].name,
                        "bin": by_id[pid].bin,
                        "own": pid in own,
                        "contracts": counts.get(pid, 0),
                        "aliases": len(aliases.get(pid, [])),
                    }
                    for pid in members
                ],
            }
        )
    out.sort(key=lambda group: (-sum(item["contracts"] for item in group["members"]), group["members"][0]["name"]))
    return out


def mark_distinct(session: Session, workspace: Workspace, access: Access, actor: Actor, ids: list[uuid.UUID]) -> dict[str, Any]:
    """«Это разные» - группа больше не предлагается."""
    _require_edit(access)
    parties = [_party(session, workspace, pid) for pid in ids]
    if len(parties) < 2:
        raise ParticipantError("Нужно хотя бы два участника")
    for party in parties:
        details = dict(party.details or {})
        marks = set(str(item) for item in details.get("distinct", []))
        marks.update(str(other.id) for other in parties if other.id != party.id)
        details["distinct"] = sorted(marks)
        party.details = details
    session.flush()
    history.write(
        session, workspace, kind="participant.distinct", entity="counterparty", entity_id=parties[0].id,
        title="это разные участники: " + ", ".join(f"«{party.name}»" for party in parties),
        after={"ids": [str(party.id) for party in parties]}, actor=actor.email,
    )
    return {"ok": True}


# ── Сведение ─────────────────────────────────────────────────────────────────


@dataclass
class MergeLog:
    """Что переехало от сведённого участника - ровно для возврата."""

    keep: uuid.UUID
    drop: uuid.UUID
    moved: dict[str, list[str]] = field(default_factory=dict)
    #: Написания сведённого, которые у оставшегося уже были, - удалены (их данные).
    dropped_aliases: list[dict[str, Any]] = field(default_factory=list)
    #: Написание-имя сведённого, добавленное оставшемуся.
    added_alias: str | None = None
    #: Наше юрлицо переехало от сведённого: его прежние поля.
    entity: dict[str, Any] | None = None
    keep_before: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "keep": str(self.keep),
            "drop": str(self.drop),
            "moved": self.moved,
            "dropped_aliases": self.dropped_aliases,
            "added_alias": self.added_alias,
            "entity": self.entity,
            "keep_before": self.keep_before,
        }


def _move(session: Session, log: MergeLog, name: str, model: Any, column: Any, keep: uuid.UUID, drop: uuid.UUID) -> list[uuid.UUID]:
    ids = list(session.scalars(sa.select(model.id).where(column == drop)))
    if ids:
        session.execute(sa.update(model).where(model.id.in_(ids)).values({column.key: keep}))
        log.moved[name] = [str(item) for item in ids]
    return ids


def merge_into(session: Session, workspace: Workspace, *, keep: uuid.UUID, drop: uuid.UUID) -> MergeLog:
    """Перенести всё от `drop` к `keep` и сдать `drop` в архив. Без прав и журнала - их ставит вызывающий."""
    if keep == drop:
        raise ParticipantError("Это один и тот же участник")
    kept, dropped = _party(session, workspace, keep), _party(session, workspace, drop)
    if dropped.merged_into_id is not None or kept.merged_into_id is not None:
        raise ParticipantError("Один из участников уже сведён в другого")
    if kept.bin and dropped.bin and kept.bin != dropped.bin:
        raise ParticipantError(f"У них разные БИН ({kept.bin} и {dropped.bin}) - это разные лица")
    kept_entity, dropped_entity = session.get(GroupEntity, keep), session.get(GroupEntity, drop)
    if kept_entity is not None and dropped_entity is not None:
        raise ParticipantError("Оба - наши юрлица; юрлица группы не сводятся")
    log = MergeLog(keep=keep, drop=drop, keep_before={"bin": kept.bin, "kind": kept.kind, "full_name": kept.full_name})

    touched = _move(session, log, "contracts.executor", Contract, Contract.executor_id, keep, drop)
    touched += _move(session, log, "contracts.customer", Contract, Contract.customer_id, keep, drop)
    _move(session, log, "operations", Operation, Operation.counterparty_id, keep, drop)
    _move(session, log, "invoices", Invoice, Invoice.counterparty_id, keep, drop)
    _move(session, log, "recurrences", Recurrence, Recurrence.counterparty_id, keep, drop)

    keep_keys = set(session.scalars(sa.select(CounterpartyName.normalized).where(CounterpartyName.counterparty_id == keep)))
    for alias in list(session.scalars(sa.select(CounterpartyName).where(CounterpartyName.counterparty_id == drop))):
        if alias.normalized in keep_keys:
            log.dropped_aliases.append({"name": alias.name, "normalized": alias.normalized, "source": alias.source})
            session.delete(alias)
        else:
            alias.counterparty_id = keep
            keep_keys.add(alias.normalized)
            log.moved.setdefault("names", []).append(str(alias.id))
    session.flush()
    if party_key(dropped.name) not in keep_keys and party_key(dropped.name) != party_key(kept.name):
        alias = CounterpartyName(
            workspace_id=workspace.id, counterparty_id=keep, name=dropped.name, normalized=party_key(dropped.name), source="manual"
        )
        session.add(alias)
        session.flush()
        log.added_alias = str(alias.id)

    if dropped_entity is not None:
        log.entity = {
            "code": dropped_entity.code, "full_name": dropped_entity.full_name, "bin": dropped_entity.bin,
            "vat_payer": dropped_entity.vat_payer, "position": dropped_entity.position,
            "archived": dropped_entity.archived_at.isoformat() if dropped_entity.archived_at else None,
        }
        session.add(
            GroupEntity(
                counterparty_id=keep, workspace_id=workspace.id, code=dropped_entity.code,
                full_name=dropped_entity.full_name, bin=dropped_entity.bin or kept.bin,
                vat_payer=dropped_entity.vat_payer, position=dropped_entity.position, archived_at=dropped_entity.archived_at,
            )
        )
        session.flush()
        ids = list(session.scalars(sa.select(Account.id).where(Account.group_entity_id == drop)))
        if ids:
            session.execute(sa.update(Account).where(Account.id.in_(ids)).values(group_entity_id=keep))
            log.moved["accounts"] = [str(item) for item in ids]
        _move(session, log, "onec_bases", OnecBase, OnecBase.group_entity_id, keep, drop)
        _move(session, log, "tax_periods", EntityTaxPeriod, EntityTaxPeriod.entity_id, keep, drop)
        session.execute(sa.delete(GroupEntity).where(GroupEntity.counterparty_id == drop))

    if not kept.bin and dropped.bin:
        kept.bin, kept.kind = dropped.bin, dropped.kind
    if not kept.full_name and dropped.full_name:
        kept.full_name = dropped.full_name
    dropped.merged_into_id = keep
    dropped.archived_at = _now()
    session.flush()
    _touch(session, workspace, touched)
    _schema_changed(session, workspace)
    return log


def merge(session: Session, workspace: Workspace, access: Access, actor: Actor, *, keep: uuid.UUID, drop: uuid.UUID) -> dict[str, Any]:
    """Свести двух участников - с точкой восстановления и событием журнала."""
    from app.finance.contracts import restore

    _require_setup(access)
    kept, dropped = _party(session, workspace, keep), _party(session, workspace, drop)
    names = (kept.name, dropped.name)
    log = merge_into(session, workspace, keep=keep, drop=drop)
    moved = len(log.moved.get("contracts.executor", [])) + len(log.moved.get("contracts.customer", []))
    title = f"«{names[1]}» сведён в «{names[0]}»" + (f": {_contracts_word(moved)}" if moved else "")
    point = restore._point(session, workspace, actor, "party_merge", f"сведены участники: {title}", log.payload())
    history.write(
        session, workspace, kind="participant.merge", entity="counterparty", entity_id=keep,
        title=title, before={"drop": str(drop), "name": names[1]}, after={"keep": str(keep), "moved": {k: len(v) for k, v in log.moved.items()}},
        actor=actor.email,
    )
    return {"keep": str(keep), "moved": {key: len(value) for key, value in log.moved.items()}, "point": str(point.id), "title": title}


def undo_merge(session: Session, workspace: Workspace, payload: dict[str, Any], done: list[str], skipped: list[str]) -> None:
    """Вернуть сведение: разнести обратно ровно то, что переехало, и вернуть участника из архива."""
    keep, drop = uuid.UUID(payload["keep"]), uuid.UUID(payload["drop"])
    kept, dropped = session.get(Counterparty, keep), session.get(Counterparty, drop)
    if kept is None or dropped is None:
        skipped.append("один из участников удалён насовсем - вернуть нечего")
        return
    if dropped.merged_into_id != keep:
        skipped.append(f"«{dropped.name}» уже не сведён в «{kept.name}»")
        return
    moved = payload.get("moved") or {}
    back = 0

    def put_back(name: str, model: Any, column: Any, target: uuid.UUID) -> list[uuid.UUID]:
        ids = [uuid.UUID(item) for item in moved.get(name, [])]
        if not ids:
            return []
        still = list(session.scalars(sa.select(model.id).where(model.id.in_(ids), column == keep)))
        if still:
            session.execute(sa.update(model).where(model.id.in_(still)).values({column.key: target}))
        if len(still) < len(ids):
            skipped.append(f"{len(ids) - len(still)} записей ({name}) после сведения поменяли - не тронуты")
        return still

    touched = put_back("contracts.executor", Contract, Contract.executor_id, drop)
    touched += put_back("contracts.customer", Contract, Contract.customer_id, drop)
    back += len(touched)
    put_back("operations", Operation, Operation.counterparty_id, drop)
    put_back("invoices", Invoice, Invoice.counterparty_id, drop)
    put_back("recurrences", Recurrence, Recurrence.counterparty_id, drop)
    put_back("names", CounterpartyName, CounterpartyName.counterparty_id, drop)
    if payload.get("added_alias"):
        session.execute(sa.delete(CounterpartyName).where(CounterpartyName.id == uuid.UUID(payload["added_alias"])))
    for item in payload.get("dropped_aliases") or []:
        exists = session.scalar(
            sa.select(CounterpartyName.id).where(CounterpartyName.counterparty_id == drop, CounterpartyName.normalized == item["normalized"])
        )
        if exists is None:
            session.add(CounterpartyName(workspace_id=workspace.id, counterparty_id=drop, name=item["name"], normalized=item["normalized"], source=item["source"]))
    entity = payload.get("entity")
    if entity:
        archived = datetime.fromisoformat(entity["archived"]) if entity.get("archived") else None
        session.add(
            GroupEntity(
                counterparty_id=drop, workspace_id=workspace.id, code=entity.get("code", ""),
                full_name=entity.get("full_name", ""), bin=entity.get("bin", ""), vat_payer=bool(entity.get("vat_payer")),
                position=int(entity.get("position") or 0), archived_at=archived,
            )
        )
        session.flush()
        put_back("accounts", Account, Account.group_entity_id, drop)
        put_back("onec_bases", OnecBase, OnecBase.group_entity_id, drop)
        put_back("tax_periods", EntityTaxPeriod, EntityTaxPeriod.entity_id, drop)
        if session.get(GroupEntity, keep) is not None:
            session.execute(sa.delete(GroupEntity).where(GroupEntity.counterparty_id == keep))
    before = payload.get("keep_before") or {}
    if "bin" in before and kept.bin != before.get("bin") and dropped.bin == kept.bin:
        kept.bin, kept.kind = before.get("bin", ""), before.get("kind", "")
    if "full_name" in before and not before.get("full_name"):
        kept.full_name = ""
    dropped.merged_into_id = None
    dropped.archived_at = None
    session.flush()
    _touch(session, workspace, touched)
    _schema_changed(session, workspace)
    done.append(f"«{dropped.name}» снова отдельно" + (f": {_contracts_word(back)} вернулись" if back else ""))


# ── БИН из источников ────────────────────────────────────────────────────────


def _rejected(party: Counterparty) -> set[str]:
    return {str(item) for item in (party.details or {}).get("bin_rejected", [])}


#: Буквы имени человека - кириллица с казахскими, латиница.
_NAME_WORD = re.compile(r"[a-zа-яёәіңғүұқөһ]+")


def short_name(name: str) -> tuple[str, str] | None:
    """Фамилия и первая буква имени человека: «КАСЫМОВ Е.» и «Касымов Ерлан» - («касымов», «е»).

    Только для имени человека из двух-трёх слов без формы собственности: у
    ТОО «фамилии» нет, и «Алтын Дала» не должно сойтись с «Алтын Арман».
    """
    if form_of(name):
        return None
    words = _NAME_WORD.findall(norm(_expand(name)))
    if len(words) not in (2, 3) or len(words[0]) < 2:
        return None
    return words[0], words[1][0]


def bin_proposals(session: Session, workspace: Workspace, access: Access, book: book_module.Book | None = None) -> list[dict[str, Any]]:
    """Однозначные БИН для участников без БИН: из 1С, строк банка в сводке и выписок.

    Однозначно - когда у участника из всех источников ровно один номер и этот
    номер не записан у другого участника. Иначе предложение с пометкой
    «спорно» и всеми номерами - выбирает человек.

    Выписка: слушатель платит с карты, банк пишет «КАСЫМОВ Е.» и ИИН, а в
    таблице он «Касымов Ерлан» без ИИН (полигон, У9). По имени такая оплата
    договора не находит; фамилия и инициал единственного участника без БИН
    (`short_name`) - предложение записать ему ИИН плательщика, и оплата сядет
    по ИИН. Плательщик, чьё имя и так совпадает с участником, оплату находит
    сам - его не предлагаем.
    """
    ids = packs.country_of(workspace).ids
    from app.finance.onec import match as onec_match

    parties = {
        party.id: party
        for party in session.scalars(
            sa.select(Counterparty).where(
                Counterparty.workspace_id == workspace.id, Counterparty.archived_at.is_(None), Counterparty.bin == ""
            )
        )
    }
    if not parties:
        return []
    found: dict[uuid.UUID, dict[str, list[str]]] = {}

    def note(party_id: uuid.UUID | None, number: str, evidence: str) -> None:
        if party_id not in parties or not ids.checksum_ok(number) or number in _rejected(parties[party_id]):
            return
        found.setdefault(party_id, {}).setdefault(number, [])
        if evidence not in found[party_id][number]:
            found[party_id][number].append(evidence)

    # 1С: документ, сопоставленный с договором, - БИН его клиента.
    docs = list(
        session.scalars(
            sa.select(OnecDocument).where(OnecDocument.workspace_id == workspace.id, OnecDocument.party_bin != "")
        )
    )
    if docs:
        bases = {base.id: base for base in session.scalars(sa.select(OnecBase).where(OnecBase.workspace_id == workspace.id))}
        index = onec_match.build_index(session, workspace.id)
        resolved = onec_match.resolve(docs, bases, index)
        contracts = {item.id: item for item in index.contracts.values()}
        for doc in docs:
            match = resolved.matches.get(doc.id)
            if match is None or match.state != "ok" or match.contract_id not in contracts:
                continue
            info = contracts[match.contract_id]
            base = bases.get(doc.base_id)
            # Продажа: клиент - заказчик договора; покупка 1С не читает.
            if base is not None and base.group_entity_id == info.executor_id:
                note(info.customer_id, packs.digits(doc.party_bin), f"1С: «{doc.party_full_name or doc.party_name}»")

    # Сводная: имя в банке с подписанным БИН у строки, нашедшей договор.
    if book is not None:
        matcher = book_module.Matcher(session, workspace.id)
        customers = dict(session.execute(sa.select(Contract.id, Contract.customer_id).where(Contract.workspace_id == workspace.id)).all())
        for row in book.rows:
            numbers = ids.found_in(row.bank_name) + ids.found_in(row.onec_name)
            if not numbers:
                continue
            match = matcher.match(row)
            if match.state != "found":
                continue
            for number in numbers:
                note(customers.get(match.contract_id), number, f"сводная, имя в банке: «{row.bank_name[:90]}»")

    # Выписка: плательщик с ИИН - фамилия и инициал участника договора без БИН.
    from app.finance.models import Operation

    sides = {
        party_id
        for pair in session.execute(
            sa.select(Contract.customer_id, Contract.executor_id).where(
                Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None)
            )
        )
        for party_id in pair
        if party_id in parties
    }
    by_short: dict[tuple[str, str], list[uuid.UUID]] = {}
    for party_id in sides:
        key = short_name(parties[party_id].name)
        if key is not None:
            by_short.setdefault(key, []).append(party_id)
    if by_short:
        payers = session.execute(
            sa.select(Counterparty.id, Counterparty.name, Counterparty.bin, Counterparty.details).where(
                Counterparty.workspace_id == workspace.id,
                Counterparty.id.in_(
                    sa.select(Operation.counterparty_id).where(
                        Operation.workspace_id == workspace.id, Operation.kind == "income", Operation.deleted_at.is_(None)
                    )
                ),
            )
        )
        for payer_id, name, bin_value, details in payers:
            number = packs.digits(bin_value or (details or {}).get("bin") or "")
            key = short_name(name)
            if not number or key is None or payer_id in sides:
                continue
            fitting = [party_id for party_id in by_short.get(key, []) if party_key(parties[party_id].name) != party_key(name)]
            if len(fitting) == 1 and len(by_short.get(key, [])) == 1:
                note(fitting[0], number, f"выписка: «{name}»")

    out = []
    for party_id, numbers in found.items():
        party = parties[party_id]
        taken = {number: owner_of_bin(session, workspace, number, but=party_id) for number in numbers}
        clean = [number for number, owner in taken.items() if owner is None]
        out.append(
            {
                "party_id": str(party_id),
                "name": party.name,
                "numbers": [
                    {
                        "bin": number,
                        "kind": ids.kind_of(number, party.name),
                        "evidence": evidence[:4],
                        "taken_by": {"id": str(taken[number].id), "name": taken[number].name} if taken[number] else None,
                    }
                    for number, evidence in numbers.items()
                ],
                "clear": len(numbers) == 1 and len(clean) == 1,
            }
        )
    out.sort(key=lambda item: (not item["clear"], item["name"].lower()))
    return out


def reject_bin(session: Session, workspace: Workspace, access: Access, actor: Actor, party_id: uuid.UUID, number: str) -> dict[str, Any]:
    """«Не тот номер» - больше не предлагать его этому участнику."""
    _require_edit(access)
    party = _party(session, workspace, party_id)
    clean = packs.digits(number)
    details = dict(party.details or {})
    details["bin_rejected"] = sorted(set(details.get("bin_rejected", [])) | {clean})
    party.details = details
    session.flush()
    history.write(
        session, workspace, kind="participant.bin.reject", entity="counterparty", entity_id=party.id,
        title=f"{party.name}: БИН {clean} - не его", after={"bin": clean}, actor=actor.email,
    )
    return {"ok": True}


# ── Написания и коды из сводки ───────────────────────────────────────────────


#: Доля строк кода у одного юрлица, при которой код его, если имя подтверждает.
CODE_MAJORITY = 0.8
_WORDS = re.compile(r"[0-9a-zа-я]+")


def _code_owner(
    code: str, counts: dict[uuid.UUID, int], entities: dict[uuid.UUID, GroupEntity], names: dict[uuid.UUID, str]
) -> tuple[uuid.UUID | None, str]:
    """Чей код нашего юрлица в таблице.

    1. Код равен имени нашего юрлица («ТС», «Тумар Сервис») - его, даже если
       часть строк с этим кодом нашла договоры других юрлиц: это переводы
       клиентов, которых нет в реестре, и их покажет тень.
    2. Все строки с кодом нашли договоры одного юрлица - его.
    3. У одного юрлица не меньше 80% строк, и в его имени есть слово кода
       («ТМ Плюс» → «Тумар Плюс») - его.
    Иначе - спорно: решает человек.
    """
    key = party_key(code)
    by_name = [eid for eid, entity in entities.items() if key in (party_key(names.get(eid, "")), party_key(entity.code))]
    if len(by_name) == 1:
        return by_name[0], "name"
    if len(counts) == 1:
        return next(iter(counts)), "rows"
    total = sum(counts.values())
    best, rows = max(counts.items(), key=lambda item: item[1])
    # Слово кода говорит о юрлице, только если оно есть в имени ровно одного
    # из наших юрлиц: общее для группы слово («Тумар» у всех) ничего не решает.
    words_of = {eid: set(_WORDS.findall(norm(names.get(eid, "")))) for eid in entities}
    distinct = {word for word in _WORDS.findall(norm(code)) if sum(word in words for words in words_of.values()) == 1}
    if total and rows / total >= CODE_MAJORITY and distinct & words_of.get(best, set()):
        return best, "majority"
    return None, ""


@dataclass
class Harvest:
    """Что собрано из сводки: написания клиентов и коды наших юрлиц."""

    rows: int = 0
    matched: int = 0
    aliases: dict[str, int] = field(default_factory=dict)
    codes: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    #: Написания, общие для нескольких клиентов (банк-посредник), - не записаны.
    shared: int = 0


def harvest(session: Session, workspace: Workspace, access: Access, actor: Actor, book: book_module.Book, *, apply: bool) -> dict[str, Any]:
    """Написания заказчика, имени в банке и в 1С - клиенту договора строки; коды наших юрлиц - юрлицам.

    Написание пишется, только если строка однозначно нашла договор (номер и
    клиент) и это написание - одного клиента: «Филиал "Южная столица" АО
    "Kaspi Bank"» в колонке имени в банке стоит у семи клиентов, платящих через
    Kaspi, и написанием ни одного из них не является (25.10.2026). Код юрлица
    ставится, если все строки с этим кодом нашли договоры одного и того же
    нашего юрлица, а у юрлица кода ещё нет. Без `apply` - только показать, что
    будет записано.
    """
    _require_setup(access, "Собирает написания из сводки")
    matcher = book_module.Matcher(session, workspace.id)
    contracts = {
        contract_id: (customer_id, executor_id)
        for contract_id, customer_id, executor_id in session.execute(
            sa.select(Contract.id, Contract.customer_id, Contract.executor_id).where(Contract.workspace_id == workspace.id)
        )
    }
    entities = {item.counterparty_id: item for item in session.scalars(
        sa.select(GroupEntity).where(GroupEntity.workspace_id == workspace.id, GroupEntity.archived_at.is_(None))
    )}
    names = dict(session.execute(sa.select(Counterparty.id, Counterparty.name).where(Counterparty.workspace_id == workspace.id)).all())
    result = Harvest()
    wanted: dict[tuple[uuid.UUID, str], str] = {}
    codes: dict[str, dict[uuid.UUID, int]] = {}
    for row in book.rows:
        result.rows += 1
        match = matcher.match(row)
        if match.state != "found" or match.contract_id not in contracts:
            continue
        result.matched += 1
        customer_id, executor_id = contracts[match.contract_id]
        if customer_id is not None:
            for name, source in ((row.customer, "registry"), (row.bank_name, "bank"), (row.onec_name, "1c")):
                key = party_key(name)
                if key and key != party_key(names.get(customer_id, "")):
                    wanted.setdefault((customer_id, key), f"{source}\u0000{' '.join(name.split())}")
        if executor_id in entities and row.own:
            counts = codes.setdefault(norm(row.own), {})
            counts[executor_id] = counts.get(executor_id, 0) + 1

    existing = {
        (party_id, normalized)
        for party_id, normalized in session.execute(
            sa.select(CounterpartyName.counterparty_id, CounterpartyName.normalized).where(CounterpartyName.workspace_id == workspace.id)
        )
    }
    owners: dict[str, set[uuid.UUID]] = {}
    for party_id, key in wanted:
        owners.setdefault(key, set()).add(party_id)
    # Не «имя другого участника»: контрагент, которого завела выписка, - это и
    # есть банковское написание клиента, и такая проверка не дала бы связать ни одно.
    for (party_id, key), packed in wanted.items():
        if (party_id, key) in existing:
            continue
        if len(owners[key]) > 1:
            result.shared += 1
            continue
        source, name = packed.split("\u0000", 1)
        result.aliases[source] = result.aliases.get(source, 0) + 1
        if apply:
            session.add(CounterpartyName(workspace_id=workspace.id, counterparty_id=party_id, name=name, normalized=key, source=source))
    by_code_label = {norm(row.own): row.own for row in book.rows if row.own}
    taken_codes = {norm(item.code): item.counterparty_id for item in entities.values() if item.code}
    for code, counts in codes.items():
        label = by_code_label.get(code, code)
        entity_id, how = _code_owner(code, counts, entities, names)
        if entity_id is None:
            result.conflicts.append(
                {
                    "code": label,
                    "entities": [
                        {"id": str(eid), "name": names.get(eid, ""), "rows": n}
                        for eid, n in sorted(counts.items(), key=lambda item: -item[1])
                    ],
                }
            )
            continue
        entity = entities[entity_id]
        if entity.code or code in taken_codes:
            continue
        taken_codes[code] = entity_id
        result.codes.append(
            {"entity_id": str(entity_id), "name": names.get(entity_id, ""), "code": label, "rows": counts.get(entity_id, 0), "how": how}
        )
        if apply:
            entity.code = label
    if apply:
        session.flush()
        _schema_changed(session, workspace)
        history.write(
            session, workspace, kind="participant.harvest", entity="counterparty",
            title=(
                f"из сводки: написаний - {sum(result.aliases.values())}, кодов юрлиц - {len(result.codes)}"
            ),
            after={"aliases": result.aliases, "codes": result.codes}, actor=actor.email,
        )
    return {
        "rows": result.rows,
        "matched": result.matched,
        "aliases": result.aliases,
        "shared": result.shared,
        "codes": result.codes,
        "conflicts": result.conflicts,
        "applied": apply,
    }


__all__ = [
    "MergeLog",
    "ParticipantError",
    "add_alias",
    "aliases_of",
    "bare_key",
    "bin_proposals",
    "dossier",
    "duplicates",
    "form_of",
    "SHOWS",
    "harvest",
    "listing",
    "mark_distinct",
    "merge",
    "merge_into",
    "overview",
    "owner_of_bin",
    "reject_bin",
    "remember",
    "remove_alias",
    "search",
    "set_bin",
    "undo_merge",
    "update",
]
