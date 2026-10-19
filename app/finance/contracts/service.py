"""Договоры: чтение, правка, стороны, соглашения, замечания, живой режим.

Одна дорога для листа, карточки и файла
───────────────────────────────────────
Правка ячейки листа и правка поля карточки - один и тот же `patch`: значение
приходит сырым текстом («1 500,50», «ТОО Атриум плюс», «01.02.2024») или
готовым идентификатором, а разбирается здесь теми же функциями, что и
загрузка Excel. Иначе «1 500,50» в листе однажды стало бы числом 150050, а в
файле осталось бы 1500.50, и объяснить расхождение было бы нечем (урок
табличного вида журнала).

Сторона и сумма не меняются молча
─────────────────────────────────
Правка исполнителя, заказчика или суммы у существующего договора требует
режима: `fix` - опечатка, меняется текущее значение; `from_date` - пишется
соглашение, старое значение действует до даты. Без режима - `ModeRequired`,
и вопрос задаётся у поля. Так одно поведение держат и лист, и карточка, и
любой будущий клиент API: перевод клиента на другое ТОО не может стереть
историю ни через какую дверь. Договор, заведённый этим же человеком сегодня,
правится без вопроса - это ещё набор, а не изменение.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

import orjson
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.layout import norm
from app.finance import history
from app.finance.accounts_model import FinanceUser
from app.finance.contracts import views as views_module
from app.finance.contracts.fields import (
    CHOICES,
    COMPUTED_FIELDS,
    DERIVED_FIELDS,
    ENTITY,
    FIELD_BY_KEY,
    LIVE_FIELDS,
    MODE_FIELDS,
    SNAPSHOT_FIELDS,
    SUMMARY_FIELDS,
    SYSTEM_KEYS,
    SYSTEM_LISTS,
    bump,
    current,
    economic_role_values,
    ensure_registry,
    fields_of,
    fill_of,
    number_key,
    party_key,
    subject_meaning,
)
from app.finance.contracts.models import (
    BILLING_KINDS,
    END_KINDS,
    Contract,
    ContractAmendment,
    ContractDepartment,
    ContractPerson,
    CounterpartyName,
    Department,
    Employee,
    EntityField,
    EntityView,
    GroupEntity,
    ListValue,
)
from app.finance.importing import DateReading, parse_date, parse_money
from app.finance.models import POSITION_STEP, Counterparty, Workspace
from app.finance.service import FinanceError, check_date, check_money

#: Часовой пояс компании. Казахстан с 2024 года живёт в одном поясе, UTC+5;
#: «сегодня» для правила «заведён сегодня» и для применения соглашений
#: считается здесь, а не по UTC - иначе в полночь по Алматы договор
#: пять часов оставался бы «вчерашним».
COMPANY_TZ = timezone(timedelta(hours=5))
DMY = DateReading(order="dmy", evidence="дата в карточке или листе")

#: Поля, значения которых - идентификаторы из справочников.
LIST_KEYS = frozenset(SYSTEM_LISTS)
DATE_KEYS = frozenset({"signed_at", "planned_end_at", "end_date"})
TEXT_KEYS = frozenset(
    {"number", "folder_url", "amendments_text", "amendments_summary_text", "note", "amount_terms"}
)
#: Что поле договора значит для замечаний, истории и выдачи. Отделов здесь
#: нет: их у договора список (`Contract.department_rows`), как людей.
COLUMN_OF = {
    "executor": "executor_id",
    "customer": "customer_id",
    "type": "type_id",
    "subject": "subject_id",
    "status": "status_id",
    "economic_role": "economic_role_id",
}
#: Как режется список в одной ячейке - «Тимур, Салтанат», «ОБО, НО,⏎ ЮО, HR».
_LIST_SPLIT = re.compile(r"[,;\n/]+")
_NUMERIC_MONEY = re.compile(r"^[\s\d.,'  ()+\-]*\d[\s\d.,'  ()+\-]*(тг|тенге|₸|kzt)?\.?$", re.IGNORECASE)


def today() -> date:
    return datetime.now(COMPANY_TZ).date()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ModeRequired(FinanceError):
    """Правка стороны или суммы без ответа «опечатка или с даты»."""

    def __init__(self, fields: Sequence[str]):
        super().__init__("Это изменение стороны или суммы - скажите, опечатка это или изменение с даты")
        self.fields = list(fields)


class FieldConflict(FinanceError):
    """Поле успели поменять после того, как его прочитали."""

    def __init__(self, contract: Contract, conflicts: Sequence[str]):
        super().__init__("Это поле успели поменять - на экране свежее значение")
        self.contract = contract
        self.conflicts = list(conflicts)


class NotFound(FinanceError):
    """Договора нет - или он не открыт этому человеку. Ответ одинаковый."""


class NotInList(FinanceError):
    """Значения нет в закрытом списке поля.

    Закрытый список не заводит новое из напечатанного: опечатка в статусе
    («им») раньше становилась ещё одним статусом, и договор с ним выпадал из
    всех листов, где отбор шёл по статусу.

    `kind`: `missing` - не нашлось; `ambiguous` - подходит несколько;
    `archived` - нашлось только в архиве.
    """

    def __init__(self, text: str, kind: str = "missing"):
        super().__init__(text)
        self.kind = kind


_ORG_PREFIX = re.compile(r"^(тоо|ип|ао)")


def _pick_closed(
    text: str,
    items: Sequence[Any],
    names: Any,
    *,
    key: Any,
    title: str,
    missing: str,
    archived: Sequence[Any] = (),
) -> Any:
    """Одно из `items` по напечатанному: точное написание, иначе однозначное начало.

    Начало - это «Дей» → «Действующий», «Наталья П.» → «Наталья Петровна»,
    «Omar» → «Omar Development & Consulting». Кандидатов больше одного - отказ со
    списком: выбирать между ними за человека нельзя (правило «не угадывать»).
    """
    needle = key(text)
    if not needle:
        # «ип» без имени - не пустое значение, а ничего не найдено: пустым
        # оно стёрло бы поле.
        raise NotInList(missing)
    exact =[item for item in items if any(key(name) == needle for name in names(item) if name)]
    if len(exact) == 1:
        return exact[0]
    pool = exact or [
        item for item in items if any(key(name).startswith(needle) for name in names(item) if name)
    ]
    if len(pool) == 1:
        return pool[0]
    if pool:
        shown = ", ".join(f"«{next(n for n in names(item) if n)}»" for item in pool[:3])
        more = f" и ещё {len(pool) - 3}" if len(pool) > 3 else ""
        raise NotInList(
            f"{title}: под «{text}» подходит несколько - {shown}{more}. Выберите из списка", "ambiguous"
        )
    if any(any(key(name) == needle for name in names(item) if name) for item in archived):
        raise NotInList(f"{title}: «{text}» в корзине - выберите действующее значение", "archived")
    raise NotInList(missing)


def _list_key(value: Any) -> str:
    return norm(value)


def _person_key(value: Any) -> str:
    # «Наталья П.» - начало «Наталья Петровна», если точку не считать буквой.
    return norm(str(value or "").replace(".", " ")).strip()


def _own_key(value: Any) -> str:
    # «Omar» - начало «Omar Development», в каком бы виде ни была записана
    # организационная форма: «ТОО «BBC Astana»» и «BBC Astana» - одно имя.
    return _ORG_PREFIX.sub("", party_key(value))


# ── Права ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Access:
    """Что человеку можно в реестре.

    Собирается из прав отдела и человека (`access_of`, `app/finance/access.py`);
    сервис с первого дня спрашивает только этот объект, поэтому приход
    настоящих прав его не изменил.
    """

    view: bool = False
    edit: bool = False
    setup: bool = False
    #: `all` / `department` / `own` - какие договоры видны.
    rows: str = "all"
    department_ids: frozenset[uuid.UUID] = frozenset()
    #: Отделы, договоры которых видны вдобавок к `rows`, - только чтение.
    view_departments: frozenset[uuid.UUID] = frozenset()
    employee_id: uuid.UUID | None = None
    #: Юрлица, договоры которых видны; пусто - все.
    entity_ids: frozenset[uuid.UUID] = frozenset()
    hidden: frozenset[str] = frozenset()
    readonly: frozenset[str] = frozenset()
    #: Владелец или администратор: видит и правит всё, включая доли.
    admin: bool = False
    #: Отдел, которым человек руководит (право «Сотрудники и права» своего
    #: отдела); `None` - не начальник. Начальник видит доли людей в договорах
    #: своего отдела (`shares.py`).
    head_department: uuid.UUID | None = None
    #: Начальник видит и доли отделов в договорах своего отдела - пока
    #: администратор не снял (`Rights.people_shares`).
    head_shares: bool = True
    #: «В договорах включено всё»: правит, все договоры, без ограничения по
    #: юрлицам. Начальнику с таким правом открыты и доли отделов.
    full_contracts: bool = False

    def can_edit_field(self, key: str) -> bool:
        return self.edit and key not in self.hidden and key not in self.readonly


def access_of(member: Any) -> Access:
    """Права на реестр из снимка вошедшего (`Member.rights`).

    * владелец и администратор - всё, включая загрузку и настройку реестра;
    * сотрудник - уровень раздела «Договоры» (видит / правит), область строк
      (все / своего отдела / где он ответственный), юрлица и поля: «нет» -
      поле скрыто и не сериализуется вовсе, «видит» - только чтение.
    """
    rights = getattr(member, "rights", None)
    if rights is None:
        return Access()
    if rights.is_admin:
        return Access(view=True, edit=True, setup=True, admin=True, full_contracts=True)
    level = rights.level("contracts")
    if level == "none":
        return Access()
    head = rights.department_id if rights.people_rows == "department" and rights.can("people") else None
    hidden = frozenset(key for key in rights.fields if rights.field_level(key) == "none")
    # «Оплачено/Остаток по выписке» - суммы из журнала операций: кому журнал
    # не открыт, тому и эти поля.
    if rights.level("journal") == "none":
        hidden = hidden | frozenset(LIVE_FIELDS)
    readonly = frozenset(key for key in rights.fields if rights.field_level(key) == "view")
    return Access(
        view=True,
        edit=level == "edit",
        setup=False,
        rows=rights.contract_rows,
        department_ids=frozenset({rights.department_id}) if rights.department_id else frozenset(),
        view_departments=getattr(rights, "contract_departments", frozenset()),
        employee_id=rights.employee_id,
        entity_ids=rights.contract_entities,
        hidden=hidden,
        readonly=readonly,
        head_department=head,
        head_shares=rights.people_shares,
        full_contracts=level == "edit" and rights.contract_rows == "all" and not rights.contract_entities,
    )


# ── Контекст реестра ─────────────────────────────────────────────────────────


@dataclass
class Resolved:
    """Сторона, найденная по тексту. `ambiguous` - кандидатов больше одного."""

    party: Counterparty | None
    created: bool = False
    ambiguous: list[Counterparty] = field(default_factory=list)


_UNSET: Any = object()


class Registry:
    """Справочники реестра одной компании, собранные один раз на запрос."""

    def __init__(self, session: Session, workspace: Workspace):
        ensure_registry(session, workspace)
        self.session = session
        self.workspace = workspace
        self.fields: list[EntityField] = fields_of(session, workspace.id)
        self.field_by_key = {item.key: item for item in self.fields}
        values = session.scalars(
            sa.select(ListValue).where(ListValue.workspace_id == workspace.id)
        ).all()
        self.values: dict[uuid.UUID, ListValue] = {item.id: item for item in values}
        self.values_by_field: dict[str, dict[str, ListValue]] = {}
        for item in values:
            self.values_by_field.setdefault(item.field_key, {})[item.normalized] = item
        departments = session.scalars(
            sa.select(Department).where(Department.workspace_id == workspace.id)
        ).all()
        self.departments: dict[uuid.UUID, Department] = {item.id: item for item in departments}
        self.department_by_name: dict[str, Department] = {}
        for item in departments:
            self.department_by_name[item.normalized_name] = item
            if item.title:
                self.department_by_name.setdefault(norm(item.title), item)
        # Юрлицо в архиве - больше не «наше» для правил листов и подстановок.
        # В архив уходит только юрлицо без договоров (setup.update_entity).
        self.own: dict[uuid.UUID, GroupEntity] = {
            item.counterparty_id: item
            for item in session.scalars(
                sa.select(GroupEntity).where(
                    GroupEntity.workspace_id == workspace.id, GroupEntity.archived_at.is_(None)
                )
            )
        }
        self.views: list[EntityView] = list(
            session.scalars(
                sa.select(EntityView)
                .where(
                    EntityView.workspace_id == workspace.id,
                    EntityView.entity == ENTITY,
                    EntityView.archived_at.is_(None),
                )
                .order_by(EntityView.position, EntityView.created_at)
            )
        )
        self.economic = economic_role_values(session, workspace.id)
        self._parties: dict[uuid.UUID, Counterparty] | None = None
        self._party_cache: dict[uuid.UUID, Counterparty] = {}
        self._party_index: dict[str, dict[str, set[uuid.UUID]]] | None = None
        self._employees: dict[uuid.UUID, Employee] | None = None
        self._employee_by_name: dict[str, Employee] | None = None
        self._employee_cache: dict[uuid.UUID, Employee] = {}
        self._summary: Any = _UNSET
        self._aliases: dict[uuid.UUID, list[str]] | None = None
        self._summary_matches: dict[uuid.UUID, Any] = {}

    # - сводка оплат -

    @property
    def summary(self) -> Any:
        """Прочитанная сводка компании (`summary.peek`) или `None`.

        Без похода в Google: сборка реестра не ждёт сеть. Сводку читает
        `/contracts/summary`, а реестр берёт то, что уже прочитано.
        """
        if self._summary is _UNSET:
            from app.finance.contracts import summary as summary_module

            self._summary = summary_module.peek(self.workspace.id)
        return self._summary

    def aliases(self, party_id: uuid.UUID | None) -> list[str]:
        """Подтверждённые написания стороны - ключами `party_key`."""
        if party_id is None:
            return []
        if self._aliases is None:
            self._aliases = {}
            for normalized, owner in self.session.execute(
                sa.select(CounterpartyName.normalized, CounterpartyName.counterparty_id).where(
                    CounterpartyName.workspace_id == self.workspace.id
                )
            ):
                self._aliases.setdefault(owner, []).append(normalized)
        return self._aliases.get(party_id, [])

    def summary_of(self, contract: Contract) -> Any:
        """Что сводка знает о договоре (`summary.Match`) или `None` - сводки нет."""
        index = self.summary
        if index is None:
            return None
        hit = self._summary_matches.get(contract.id)
        if hit is None:
            ours, theirs = contract.executor_id, contract.customer_id
            if not self.is_own(ours) and self.is_own(theirs):
                ours, theirs = theirs, ours
            party = self.parties_for([theirs]).get(theirs) if theirs else None
            names = ([party.name] if party is not None else []) + self.aliases(theirs)
            entity = self.own.get(ours) if ours else None
            hit = index.match(contract.number, names, entity.code if entity is not None else "")
            self._summary_matches[contract.id] = hit
        return hit

    # - стороны -

    @property
    def parties(self) -> dict[uuid.UUID, Counterparty]:
        """Все контрагенты компании - для разбора текста в сторону.

        Ответу они не нужны: сборка ответа берёт `parties_for` - только
        стороны отданных договоров.
        """
        if self._parties is None:
            rows = self.session.scalars(
                sa.select(Counterparty).where(Counterparty.workspace_id == self.workspace.id)
            ).all()
            self._parties = {item.id: item for item in rows}
        return self._parties

    def parties_for(self, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, Counterparty]:
        """Только названные стороны.

        Опрос `changes` идёт раз в две секунды из каждой открытой вкладки, и
        при любой чужой правке каждая вкладка собирает ответ. Читать ради
        двух сторон всех контрагентов компании (у BBC их тысячи) - значит
        умножить одну правку на число открытых вкладок.
        """
        wanted = {item for item in ids if item}
        if self._parties is not None:
            return {pid: self._parties[pid] for pid in wanted if pid in self._parties}
        missing = wanted - self._party_cache.keys()
        if missing:
            for row in self.session.scalars(
                sa.select(Counterparty).where(
                    Counterparty.workspace_id == self.workspace.id, Counterparty.id.in_(missing)
                )
            ):
                self._party_cache[row.id] = row
        return {pid: self._party_cache[pid] for pid in wanted if pid in self._party_cache}

    def _index(self) -> dict[str, dict[str, set[uuid.UUID]]]:
        if self._party_index is None:
            by_norm: dict[str, set[uuid.UUID]] = {}
            by_key: dict[str, set[uuid.UUID]] = {}
            for party in self.parties.values():
                if party.archived_at is not None:
                    continue
                by_norm.setdefault(party.normalized_name, set()).add(party.id)
                by_key.setdefault(party_key(party.name), set()).add(party.id)
            for entity in self.own.values():
                for text in (entity.code, entity.full_name):
                    if text:
                        by_key.setdefault(party_key(text), set()).add(entity.counterparty_id)
            aliases = self.session.execute(
                sa.select(CounterpartyName.normalized, CounterpartyName.counterparty_id).where(
                    CounterpartyName.workspace_id == self.workspace.id
                )
            )
            for normalized, party_id in aliases:
                by_key.setdefault(normalized, set()).add(party_id)
            self._party_index = {"norm": by_norm, "key": by_key}
        return self._party_index

    def is_own(self, party_id: uuid.UUID | None) -> bool:
        return party_id is not None and party_id in self.own

    def resolve_party(self, raw: Any, *, slot: str, create: bool = True) -> Resolved:
        """Сторона по тексту или идентификатору.

        Порядок: идентификатор → точное написание → псевдоним или ключ
        (кавычки, регистр, пробелы) → код или полное имя нашего юрлица.
        Нашлось больше одного - не выбираем наугад: отдаём кандидатов. Не
        нашлось ничего - заводим контрагента с ролью по слоту.
        """
        if isinstance(raw, dict):
            raw = raw.get("id") or raw.get("name")
        text = str(raw or "").strip()
        if not text:
            return Resolved(None)
        found = self._by_id(text)
        if found is not None:
            return Resolved(found)
        index = self._index()
        for bucket, key in (("norm", norm(text)), ("key", party_key(text))):
            ids = index[bucket].get(key) or set()
            if len(ids) == 1:
                return Resolved(self.parties.get(next(iter(ids))))
            if len(ids) > 1:
                own = [pid for pid in ids if pid in self.own]
                if len(own) == 1:
                    return Resolved(self.parties.get(own[0]))
                return Resolved(None, ambiguous=[self.parties[pid] for pid in ids if pid in self.parties])
        if not create:
            return Resolved(None)
        party = Counterparty(
            workspace_id=self.workspace.id,
            role="supplier" if slot == "executor" else "client",
            name=text,
            normalized_name=norm(text),
            position=self._next_party_position(),
        )
        self.session.add(party)
        self.session.flush()
        self.parties[party.id] = party
        index["norm"].setdefault(party.normalized_name, set()).add(party.id)
        index["key"].setdefault(party_key(text), set()).add(party.id)
        return Resolved(party, created=True)

    def _by_id(self, text: str) -> Counterparty | None:
        try:
            party_id = uuid.UUID(text)
        except ValueError:
            return None
        party = self.parties.get(party_id)
        return party if party is not None and party.workspace_id == self.workspace.id else None

    def _next_party_position(self) -> int:
        top = max((item.position or 0 for item in self.parties.values()), default=0)
        return top + POSITION_STEP

    # - списки -

    def resolve_value(self, field_key: str, raw: Any, *, create: bool = True) -> ListValue | None:
        if isinstance(raw, dict):
            raw = raw.get("id") or raw.get("value")
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            found = self.values.get(uuid.UUID(text))
            if found is not None and found.field_key == field_key:
                return found
        except ValueError:
            pass
        bucket = self.values_by_field.setdefault(field_key, {})
        key = norm(text)
        if key in bucket:
            return bucket[key]
        if not create:
            return None
        meaning = subject_meaning(text) if field_key == "subject" else {}
        top = max((item.position for item in bucket.values()), default=0)
        value = ListValue(
            workspace_id=self.workspace.id,
            field_key=field_key,
            value=text,
            normalized=key,
            meaning=meaning,
            position=top + POSITION_STEP,
        )
        self.session.add(value)
        self.session.flush()
        bucket[key] = value
        self.values[value.id] = value
        bump(self.session, self.workspace.id, "schema")
        return value

    def resolve_department(self, raw: Any, *, create: bool = True) -> Department | None:
        if isinstance(raw, dict):
            raw = raw.get("id") or raw.get("code")
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            found = self.departments.get(uuid.UUID(text))
            if found is not None:
                return found
        except ValueError:
            pass
        key = norm(text)
        if key in self.department_by_name:
            return self.department_by_name[key]
        if not create:
            return None
        department = Department(
            workspace_id=self.workspace.id,
            code=text,
            normalized_name=key,
            title=text,
            position=(len(self.departments) + 1) * POSITION_STEP,
        )
        self.session.add(department)
        self.session.flush()
        self.departments[department.id] = department
        self.department_by_name[key] = department
        bump(self.session, self.workspace.id, "schema")
        return department

    # - люди -

    def _load_employees(self) -> None:
        if self._employees is None:
            rows = self.session.scalars(
                sa.select(Employee).where(Employee.workspace_id == self.workspace.id)
            ).all()
            self._employees = {item.id: item for item in rows}
            self._employee_by_name = {item.normalized_name: item for item in rows}

    @property
    def employees(self) -> dict[uuid.UUID, Employee]:
        self._load_employees()
        assert self._employees is not None
        return self._employees

    def employees_for(self, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, Employee]:
        """Только названные сотрудники - по той же причине, что `parties_for`."""
        wanted = {item for item in ids if item}
        if self._employees is not None:
            return {eid: self._employees[eid] for eid in wanted if eid in self._employees}
        missing = wanted - self._employee_cache.keys()
        if missing:
            for row in self.session.scalars(
                sa.select(Employee).where(Employee.workspace_id == self.workspace.id, Employee.id.in_(missing))
            ):
                self._employee_cache[row.id] = row
        return {eid: self._employee_cache[eid] for eid in wanted if eid in self._employee_cache}

    def resolve_people(self, raw: Any, *, create: bool = True) -> list[Employee]:
        """Ответственные: список идентификаторов или текст «Елжас, Тимур»."""
        self._load_employees()
        assert self._employees is not None and self._employee_by_name is not None
        items: list[Any]
        if raw is None or raw == "":
            return []
        if isinstance(raw, (list, tuple)):
            items = list(raw)
        else:
            items = [part for part in re.split(r"[,;\n/]+", str(raw))]
        out: list[Employee] = []
        for item in items:
            if isinstance(item, dict):
                item = item.get("id") or item.get("name")
            text = str(item or "").strip()
            if not text:
                continue
            employee = None
            try:
                employee = self._employees.get(uuid.UUID(text))
            except ValueError:
                employee = self._employee_by_name.get(norm(text))
            if employee is None and create:
                employee = Employee(
                    workspace_id=self.workspace.id,
                    full_name=text,
                    normalized_name=norm(text),
                    position=(len(self._employees) + 1) * POSITION_STEP,
                )
                self.session.add(employee)
                self.session.flush()
                self._employees[employee.id] = employee
                self._employee_by_name[employee.normalized_name] = employee
            if employee is not None and employee not in out:
                out.append(employee)
        return out

    # - ручной ввод по способу заполнения -

    def fill(self, key: str) -> str:
        """Способ заполнения поля (`fields.fill_of`); у поля без списка - пусто."""
        item = self.field_by_key.get(key)
        return fill_of(item) if item is not None else ""

    def title(self, key: str) -> str:
        item = self.field_by_key.get(key)
        return item.title if item is not None else key

    def pick_value(self, field_key: str, raw: Any) -> ListValue | None:
        """Значение закрытого списка - без заведения нового."""
        if isinstance(raw, dict):
            raw = raw.get("id") or raw.get("value")
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            found = self.values.get(uuid.UUID(text))
        except ValueError:
            found = None
        if found is not None and found.field_key == field_key:
            return found
        values = list(self.values_by_field.get(field_key, {}).values())
        title = self.title(field_key)
        return _pick_closed(
            text,
            sorted((item for item in values if item.archived_at is None), key=lambda item: item.position),
            lambda item: (item.value,),
            key=_list_key,
            title=title,
            missing=f"«{text}» нет в списке «{title}» - список пополняют в настройке реестра",
            archived=[item for item in values if item.archived_at is not None],
        )

    def pick_department(self, raw: Any) -> Department | None:
        """Отдел из списка отделов - без заведения нового."""
        if isinstance(raw, dict):
            raw = raw.get("id") or raw.get("code")
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            found = self.departments.get(uuid.UUID(text))
        except ValueError:
            found = None
        if found is not None:
            return found
        items = sorted(self.departments.values(), key=lambda item: item.position)
        return _pick_closed(
            text,
            [item for item in items if item.archived_at is None],
            lambda item: (item.code, item.title),
            key=_list_key,
            title=self.title("department"),
            missing=f"Отдела «{text}» нет в списке - отделы заводят в личном кабинете, вкладка «Люди»",
            archived=[item for item in items if item.archived_at is not None],
        )

    def input_departments(self, raw: Any, *, closed: bool) -> list[Department]:
        """Отделы договора - списком, как люди в «Ответственном лице».

        «HR, ЮО» и «ОБО, НО,⏎ ЮО, HR» - несколько отделов. Идентификатор
        принимается любой: так карточка пересылает уже стоящие, даже ушедшие в
        архив. В закрытом списке незнакомая часть - отказ с её написанием, в
        открытом - новый отдел. Повтор отдел не удваивает.
        """
        if raw is None or raw == "":
            return []
        items = list(raw) if isinstance(raw, (list, tuple)) else _LIST_SPLIT.split(str(raw))
        out: list[Department] = []
        for item in items:
            if isinstance(item, dict):
                item = item.get("id") or item.get("code")
            text = str(item or "").strip()
            if not text:
                continue
            department = self.pick_department(text) if closed else self.resolve_department(text)
            if department is not None and department not in out:
                out.append(department)
        return out

    def input_people(self, raw: Any, *, closed: bool, title: str) -> list[Employee]:
        """Ответственные из листа или карточки.

        Идентификатор принимается любой - так карточка пересылает уже стоящих
        людей, даже ушедших в архив. Текст ищется среди действующих: полное
        имя, «Наталья П.» (так лист и список показывают людей коротко) или
        однозначное начало. Не нашёлся - в закрытом списке отказ, в открытом -
        новый человек. Похожих несколько - отказ в обоих: третий «Асхат» из
        короткого имени хуже вопроса.
        """
        self._load_employees()
        assert self._employees is not None and self._employee_by_name is not None
        if raw is None or raw == "":
            return []
        items = list(raw) if isinstance(raw, (list, tuple)) else re.split(r"[,;\n/]+", str(raw))
        active = sorted(
            (item for item in self._employees.values() if item.archived_at is None),
            key=lambda item: (item.position, item.full_name),
        )
        archived = [item for item in self._employees.values() if item.archived_at is not None]
        out: list[Employee] = []
        for item in items:
            if isinstance(item, dict):
                item = item.get("id") or item.get("name")
            text = str(item or "").strip()
            if not text:
                continue
            employee: Employee | None = None
            try:
                employee = self._employees.get(uuid.UUID(text))
                if employee is None:
                    raise NotInList(f"{title}: такого сотрудника нет")
            except ValueError:
                try:
                    employee = _pick_closed(
                        text,
                        active,
                        lambda person: (person.full_name,),
                        key=_person_key,
                        title=title,
                        missing=f"«{text}» нет среди сотрудников - их заводят в личном кабинете, вкладка «Люди»",
                        archived=archived,
                    )
                except NotInList as exc:
                    if closed or exc.kind != "missing":
                        raise
                    employee = self.resolve_people([text])[0]
            if employee is not None and employee not in out:
                out.append(employee)
        return out

    def pick_own(self, raw: Any, *, title: str) -> Counterparty | None:
        """Сторона только из наших юрлиц: идентификатор, имя, код или полное имя."""
        if isinstance(raw, dict):
            raw = raw.get("id") or raw.get("name")
        text = str(raw or "").strip()
        if not text:
            return None
        own = [
            party
            for entity in sorted(self.own.values(), key=lambda row: (row.position, row.code))
            if (party := self.parties.get(entity.counterparty_id)) is not None
        ]
        if not own:
            raise NotInList(
                f"{title}: наших юрлиц ещё нет - их заводят в настройке реестра, вкладка «Наши юрлица»"
            )
        found = self._by_id(text)
        if found is not None:
            if self.is_own(found.id):
                return found
            raise NotInList(f"«{found.name}» - не наше юрлицо, а в «{title}» стоят только наши")
        return _pick_closed(
            text,
            own,
            lambda party: (party.name, self.own[party.id].code, self.own[party.id].full_name),
            key=_own_key,
            title=title,
            missing=f"«{text}» - не наше юрлицо, а в «{title}» стоят только наши",
        )

    def check_closed(self, key: str, raw: Any, *, other_own: bool = False) -> None:
        """Проверить значение закрытого поля, ничего не записывая.

        Правка стороны сначала спрашивает «опечатка или с даты» - и отказ
        «не наше юрлицо» после ответа выглядел бы издёвкой. Поэтому закрытые
        поля проверяются до вопроса. `other_own` - напротив уже стоит наше
        юрлицо: эта сторона может быть чужой (см. `_OTHER_SIDE`).
        """
        fill = self.fill(key)
        if fill not in ("list", "own") or (fill == "own" and other_own):
            return
        item = self.field_by_key.get(key)
        kind = item.type if item is not None else ""
        title = self.title(key)
        if fill == "own":
            self.pick_own(raw, title=title)
        elif key == "department":
            self.input_departments(raw, closed=True)
        elif kind == "department":
            self.pick_department(raw)
        elif kind == "person":
            self.input_people(raw, closed=True, title=title)
        elif kind == "multi_list":
            parts = raw if isinstance(raw, list) else re.split(r"[,;\n]+", str(raw or ""))
            for part in parts:
                self.pick_value(key, part)
        else:
            self.pick_value(key, raw)

    # - смыслы -

    def meaning(self, value_id: uuid.UUID | None) -> dict[str, Any]:
        value = self.values.get(value_id) if value_id else None
        return dict(value.meaning or {}) if value is not None else {}

    def roles_of(self, contract: Contract) -> dict[str, str]:
        """Подписи сторон договора: по виду, а если у вида нет - по предмету."""
        for source in (contract.type_id, contract.subject_id):
            roles = self.meaning(source).get("roles")
            if roles:
                return dict(roles)
        return {}


# ── Разбор значений ──────────────────────────────────────────────────────────


def looks_numeric(text: str) -> bool:
    """Похоже ли на число. «20% по разовым» - нет, «1 200 000 тг» - да."""
    return bool(_NUMERIC_MONEY.match(text.strip())) if text and text.strip() else False


def read_money(raw: Any, *, field: str) -> tuple[Decimal | None, str]:
    """Сумма из ячейки или поля: (число, условие текстом).

    Числом становится только то, что выглядит числом. Иначе текст целиком
    уходит в условие суммы: вычистить «20% по разовым, 40% по абон.» до цифр
    значило бы записать договор на 2040 тенге.
    """
    if raw is None or raw == "":
        return None, ""
    if isinstance(raw, (int, float, Decimal)) and not isinstance(raw, bool):
        return check_money(raw, field=field), ""
    text = str(raw).strip()
    if not text:
        return None, ""
    if not looks_numeric(text):
        return None, text
    money = parse_money(text)
    if money is None:
        return None, text
    if money.negative:
        raise FinanceError(f"{field}: сумма договора не бывает отрицательной")
    return check_money(money.value, field=field), ""


#: Слова вокруг даты, которые её не меняют: «до 15.07.2026», «15.07.2026 г.».
#: В реестре BBC «Планируемый срок» так записан в 35 строках из 415.
_DATE_QUALIFIERS = re.compile(
    r"^\s*(?:до|по|с|от|срок|не\s+позднее)\s+|\s*(?:г\.?|года?)\s*$", re.IGNORECASE
)


def read_date(raw: Any, *, field: str) -> date | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return check_date(raw.date(), field=field)
    if isinstance(raw, date):
        return check_date(raw, field=field)
    value = parse_date(raw, DMY)
    if value is None and isinstance(raw, str):
        value = parse_date(_DATE_QUALIFIERS.sub("", raw).strip(), DMY)
    if value is None:
        raise FinanceError(f"{field}: «{raw}» не похоже на дату")
    return check_date(value, field=field)


#: Ключ в `attrs`: значения из файла, которые не прочитались как значение
#: поля. Договор заводится, текст не теряется, у договора - замечание.
RAW_KEY = "__raw__"


def _plain(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


# ── Значения договора ────────────────────────────────────────────────────────


def people_of(session: Session, contract_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    if not contract_ids:
        return {}
    rows = session.execute(
        sa.select(ContractPerson.contract_id, ContractPerson.employee_id)
        .where(ContractPerson.contract_id.in_(list(contract_ids)))
        .order_by(ContractPerson.position)
    )
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for contract_id, employee_id in rows:
        out.setdefault(contract_id, []).append(employee_id)
    return out


def age_months(signed_at: date | None, on: date | None = None) -> int | None:
    """Полных «месяцев» от даты договора - как `ROUNDDOWN((TODAY()-дата)/30)`
    книги «Разовые»: по тридцать дней, а не календарных. Так считали отборы
    «до 2 мес» / «6+ мес», и один договор не должен стоять в разных листах
    книги и приложения."""
    if signed_at is None:
        return None
    return max(0, ((on or today()) - signed_at).days // 30)


def value_of(contract: Contract, key: str, people: Sequence[uuid.UUID] = ()) -> Any:
    """Значение поля договора в том виде, в каком его отдаёт API."""
    if key in COLUMN_OF:
        return _plain(getattr(contract, COLUMN_OF[key]))
    if key == "people":
        return [str(item) for item in people]
    if key == "department":
        return [str(item) for item in contract.department_ids]
    if key == "amount":
        return _plain(contract.amount)
    if key in DATE_KEYS:
        return _plain(getattr(contract, key))
    if key in ("paid_snapshot", "remaining_snapshot"):
        return (contract.file_snapshot or {}).get(key.replace("_snapshot", ""))
    if key in LIVE_FIELDS or key in SUMMARY_FIELDS:
        # Считаются из журнала (`payments.py`) или сводки (`summary.py`) и
        # приходят своим запросом.
        return None
    if key == "age_months":
        return age_months(contract.signed_at)
    if key in SYSTEM_KEYS:
        return getattr(contract, key, None)
    return (contract.attrs or {}).get(key)


def facts_of(contract: Contract, registry: Registry, people: Sequence[uuid.UUID]) -> dict[str, Any]:
    """Всё, по чему правило листа может отобрать договор."""
    facts: dict[str, Any] = {
        key: value_of(contract, key, people)
        for key in (*COLUMN_OF, "people", "department", "billing", "end_kind", "number", "note", "amount_terms")
    }
    for key, value in (contract.attrs or {}).items():
        facts.setdefault(key, value)
    own_executor = registry.is_own(contract.executor_id)
    own_customer = registry.is_own(contract.customer_id)
    facts["executor_is_own"] = own_executor
    facts["customer_is_own"] = own_customer
    facts["intra_group"] = own_executor and own_customer
    facts["phase"] = registry.meaning(contract.status_id).get("phase")
    facts["economic"] = registry.meaning(contract.economic_role_id).get("system")
    facts["age_months"] = age_months(contract.signed_at)
    # «Оплачено/Остаток (сводка)» - для листов вроде «Остатки»; нет сводки
    # или договор в ней не нашёлся - фактов нет, и условие «> 0» не проходит.
    match = registry.summary_of(contract)
    if match is not None and match.state == "found":
        facts["summary_paid"] = match.paid
        if contract.amount is not None:
            facts["summary_remaining"] = contract.amount - match.paid
    # Подписи рядом с идентификаторами: «предмет содержит „аренд“» сравнивает
    # текст значения, а не его id (views._check, условие `contains`).
    for key in LIST_KEYS:
        value_id = getattr(contract, f"{key}_id", None)
        item = registry.values.get(value_id) if value_id else None
        if item is not None:
            facts[f"{key}__text"] = item.value
    for key, raw in (contract.attrs or {}).items():
        field_def = registry.field_by_key.get(key)
        if field_def is None or field_def.type not in ("list", "multi_list"):
            continue
        texts = []
        for item_id in raw if isinstance(raw, list) else [raw]:
            try:
                item = registry.values.get(uuid.UUID(str(item_id)))
            except ValueError:
                continue
            if item is not None:
                texts.append(item.value)
        if texts:
            facts[f"{key}__text"] = texts
    departments = [registry.departments[item] for item in contract.department_ids if item in registry.departments]
    if departments:
        facts["department__text"] = [f"{item.code} {item.title or ''}".strip() for item in departments]
    names = registry.parties_for([contract.executor_id, contract.customer_id])
    for slot in ("executor", "customer"):
        party = names.get(getattr(contract, f"{slot}_id"))
        if party is not None:
            facts[f"{slot}__text"] = party.name
    return facts


# ── Замечания ────────────────────────────────────────────────────────────────


@dataclass
class NumberIndex:
    """Номер договора → пары сторон, у которых он стоит."""

    by_key: dict[str, list[tuple[uuid.UUID, frozenset[uuid.UUID | None]]]] = field(default_factory=dict)

    def add(self, contract: Contract) -> None:
        if contract.number_key and contract.deleted_at is None:
            self.by_key.setdefault(contract.number_key, []).append(
                (contract.id, frozenset({contract.executor_id, contract.customer_id}))
            )

    def others(self, contract: Contract) -> list[uuid.UUID]:
        """Договоры с тем же номером, но другими сторонами."""
        mine = frozenset({contract.executor_id, contract.customer_id})
        return [
            other_id
            for other_id, parties in self.by_key.get(contract.number_key or "", [])
            if other_id != contract.id and parties != mine
        ]


def number_index_for(session: Session, workspace_id: uuid.UUID, keys: Iterable[str]) -> NumberIndex:
    wanted = sorted({key for key in keys if key})
    index = NumberIndex()
    if not wanted:
        return index
    rows = session.scalars(
        sa.select(Contract).where(
            Contract.workspace_id == workspace_id,
            Contract.number_key.in_(wanted),
            Contract.deleted_at.is_(None),
        )
    )
    for row in rows:
        index.add(row)
    return index


#: Поля, у которых пустота уже названа своим замечанием («Не указан
#: исполнитель»): «обязательное» не должно говорить о них второй раз.
_OWN_EMPTY_ISSUES = frozenset({"executor", "customer", "signed_at"})


def issues_of(
    contract: Contract,
    registry: Registry,
    numbers: NumberIndex,
    party_names: dict[uuid.UUID, str],
    people: Sequence[uuid.UUID] | None = None,
) -> list[dict[str, Any]]:
    """Замечания к договору. Код, поле, текст и ссылка на то, о чём речь.

    `acknowledged` помечает замечания, отмеченные «так и должно быть», - но
    только с той же ссылкой: сменили номер, и новое «номер уже есть» снова
    горит.
    """
    out: list[dict[str, Any]] = []
    acked = contract.acknowledged or {}

    def add(code: str, field_key: str, text: str, ref: str = "", others: Sequence[uuid.UUID] = ()) -> None:
        mark = acked.get(code)
        item: dict[str, Any] = {
            "code": code,
            "field": field_key,
            "text": text,
            "ref": ref,
            "acknowledged": bool(mark) and (mark.get("ref", "") == ref),
        }
        # Договоры, о которых замечание («номер уже есть у …»): подсказка
        # листа ведёт к ним - где он, чей, открыть.
        if others:
            item["others"] = [str(other) for other in others]
        out.append(item)

    if contract.executor_id is None:
        add("no_executor", "executor", "Не указан исполнитель")
    if contract.customer_id is None:
        add("no_customer", "customer", "Не указан заказчик")
    if (
        contract.executor_id is not None
        and contract.customer_id is not None
        and not registry.is_own(contract.executor_id)
        and not registry.is_own(contract.customer_id)
    ):
        add("no_own_party", "executor", "Ни одна сторона не наше юрлицо")
    if contract.signed_at is None:
        add("no_signed_at", "signed_at", "Нет даты договора")
    if contract.amount is None and contract.amount_terms and contract.billing != "terms":
        add("amount_unclear", "amount", f"Непонятная сумма: «{contract.amount_terms[:80]}»")
    others = numbers.others(contract)
    if others:
        other = numbers_other_party(others[0], contract, numbers, party_names, registry)
        add(
            "number_taken",
            "number",
            f"Номер {contract.number.strip()} уже есть у {other}" if other else
            f"Номер {contract.number.strip()} уже есть у другого контрагента",
            ref=contract.number_key,
            others=others,
        )
    if contract.status_id is not None:
        meaning = registry.meaning(contract.status_id)
        if not meaning.get("phase") and not meaning.get("handover"):
            value = registry.values.get(contract.status_id)
            add("status_unknown", "status", f"Статус «{value.value if value else ''}» без смысла")
    if contract.type_id is not None and not registry.meaning(contract.type_id):
        value = registry.values.get(contract.type_id)
        add("type_unknown", "type", f"Вид «{value.value if value else ''}» без смысла - неясно, как начислять")
    if contract.end_date is not None and contract.end_kind in ("", "unknown"):
        add("end_kind_unknown", "end_date", "Непонятно, расторжение это или исполнение")
    unread = (contract.attrs or {}).get(RAW_KEY) or {}
    for key, text in unread.items():
        title = registry.field_by_key[key].title if key in registry.field_by_key else key
        shown = str(text)[:60]
        fill = registry.fill(key)
        if fill == "own":
            message = f"«{shown}» - не наше юрлицо, а в «{title}» стоят только наши"
        elif fill == "list":
            message = f"«{shown}» нет в списке «{title}»"
        else:
            message = f"«{shown}» в поле «{title}» не прочитано"
        add(f"unread_{key}", key, message, ref=str(text))
    # «Обязательное» в настройке полей - замечание у пустого поля. Не запрет:
    # договор набирают по ячейке, и первая же ячейка новой строки иначе
    # упиралась бы в пустые остальные.
    for item in registry.fields:
        if not item.required or item.hidden or item.key in _OWN_EMPTY_ISSUES or item.key in unread:
            continue
        if item.key in SNAPSHOT_FIELDS or item.key in COMPUTED_FIELDS:
            continue
        if item.key == "people" and people is None:
            continue
        value = value_of(contract, item.key, people or ())
        if value in (None, "", []):
            add(f"required_{item.key}", item.key, f"Не заполнено: «{item.title}»")
    return out


def numbers_other_party(
    other_id: uuid.UUID,
    contract: Contract,
    numbers: NumberIndex,
    party_names: dict[uuid.UUID, str],
    registry: Registry,
) -> str:
    for candidate_id, parties in numbers.by_key.get(contract.number_key or "", []):
        if candidate_id != other_id:
            continue
        for party_id in parties:
            if party_id is not None and not registry.is_own(party_id):
                return party_names.get(party_id, "")
    return ""


# ── Производные: начисление, смысл, смысл даты окончания ─────────────────────


def _derive(contract: Contract, registry: Registry, changed: set[str]) -> None:
    """Пересчитать подставляемые значения там, где их не задал человек.

    Порядок смысла: финансирование по предмету или виду сильнее сторон (заём
    между нашим ТОО и клиентом - не выручка); обе стороны наши - оборот
    внутри группы (агентский между ТОО не складывается с клиентским); наш
    только заказчик - расход; наш исполнитель - смысл вида или выручка.
    """
    provenance = dict(contract.provenance or {})
    touched = changed & {"type", "subject", "executor", "customer", "amount", "amount_terms"}
    if touched and provenance.get("billing") != "manual":
        billing, source = "", ""
        for key, value_id in (("subject", contract.subject_id), ("type", contract.type_id)):
            candidate = registry.meaning(value_id).get("billing")
            if candidate in BILLING_KINDS:
                billing, source = candidate, key
                break
        if not billing and contract.amount is None and contract.amount_terms:
            billing, source = "terms", "amount"
        if billing or provenance.get("billing") in (None, "type", "subject", "amount"):
            contract.billing = billing
            provenance["billing"] = source or provenance.get("billing") or ""
    if touched and provenance.get("economic_role") != "manual":
        system, source = _economic(contract, registry)
        value = registry.economic.get(system) if system else None
        contract.economic_role_id = value.id if value is not None else None
        provenance["economic_role"] = source
    if changed & {"end_date", "status", "billing", "type"} and provenance.get("end_kind") != "manual":
        contract.end_kind = _end_kind(contract, registry)
        provenance["end_kind"] = "status" if contract.end_kind else ""
    contract.provenance = provenance


def _economic(contract: Contract, registry: Registry) -> tuple[str, str]:
    by_meaning, source = "", ""
    for key, value_id in (("subject", contract.subject_id), ("type", contract.type_id)):
        candidate = registry.meaning(value_id).get("economic_role")
        if candidate:
            by_meaning, source = candidate, key
            break
    own_executor = registry.is_own(contract.executor_id)
    own_customer = registry.is_own(contract.customer_id)
    if by_meaning == "financing":
        return "financing", source
    if own_executor and own_customer:
        return "intra_group", "parties"
    if own_customer:
        return "expense", "parties"
    if own_executor:
        return (by_meaning or "revenue"), (source if by_meaning else "parties")
    return by_meaning, source


def _end_kind(contract: Contract, registry: Registry) -> str:
    """Смысл даты окончания по статусу и виду; не выводится - «не ясен».

    Недействующий (расторгнут) → расторжение; исполнен и вся сумма (разовый)
    → исполнение. Остальное - не ясен: действующий договор с датой в этой
    колонке - вопрос к человеку, а не повод выбрать смысл наугад.
    """
    if contract.end_date is None:
        return ""
    phase = registry.meaning(contract.status_id).get("phase")
    if phase == "terminated":
        return "terminated"
    if phase == "fulfilled" and contract.billing == "total":
        return "fulfilled"
    return "unknown"


# ── Выдача ───────────────────────────────────────────────────────────────────


def _short_name(user: FinanceUser | None) -> str:
    if user is None:
        return ""
    name = (user.full_name or "").strip()
    if not name:
        return user.email or ""
    parts = name.split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1][0]}."
    return parts[0]


def _uuids(raw: Iterable[Any]) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for item in raw:
        try:
            out.append(uuid.UUID(str(item)))
        except ValueError:
            continue
    return out


class Output:
    """Сборщик ответа: договоры, стороны, люди - с учётом прав."""

    def __init__(self, session: Session, registry: Registry, access: Access):
        self.session = session
        self.registry = registry
        self.access = access
        self._users: dict[uuid.UUID, FinanceUser] = {}

    def users(self, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, FinanceUser]:
        wanted = {item for item in ids if item and item not in self._users}
        if wanted:
            for user in self.session.scalars(sa.select(FinanceUser).where(FinanceUser.id.in_(wanted))):
                self._users[user.id] = user
        return self._users

    def contracts(
        self, contracts: Sequence[Contract], *, numbers: NumberIndex | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        registry = self.registry
        people = people_of(self.session, [item.id for item in contracts])
        if numbers is None:
            numbers = number_index_for(
                self.session, registry.workspace.id, [item.number_key for item in contracts]
            )
        users = self.users(
            [item.created_by for item in contracts] + [item.updated_by for item in contracts]
        )
        party_ids: set[uuid.UUID] = set()
        employee_ids: set[uuid.UUID] = set()
        for item in contracts:
            party_ids.update(pid for pid in (item.executor_id, item.customer_id) if pid)
            employee_ids.update(people.get(item.id, []))
        # Имена нужны сторонам этих договоров и договоров с тем же номером
        # («номер уже есть у ТОО «Бета»») - не всем контрагентам компании.
        number_parties = {
            pid for entries in numbers.by_key.values() for _cid, pair in entries for pid in pair if pid
        }
        party_names = {
            pid: party.name for pid, party in registry.parties_for(party_ids | number_parties).items()
        }
        visible = [
            item.key
            for item in registry.fields
            if item.key not in self.access.hidden
        ]
        # Доля отдела книги («Разовые ЮО» - доля ЮО, `shares.py`) - тем, кому
        # открыта сумма договора. Модуль долей импортирует этот, поэтому здесь.
        from app.finance.contracts.shares import book_shares

        book_departments = (
            [] if "amount" in self.access.hidden else _uuids(views_module.book_departments(registry.views).values())
        )
        out: list[dict[str, Any]] = []
        for item in contracts:
            if not visible_to(item, registry, self.access, people.get(item.id, [])):
                continue
            mine = people.get(item.id, [])
            # Пустые значения не отдаются: клиент считает отсутствующее пустым,
            # а список на пять сотен договоров худеет вдвое.
            values = {
                key: value
                for key in visible
                if (value := value_of(item, key, mine)) not in (None, "", [])
            }
            updated_by = users.get(item.updated_by) if item.updated_by else None
            out.append(
                {
                    "id": str(item.id),
                    "seq": item.seq,
                    "values": values,
                    "provenance": item.provenance or {},
                    "issues": [
                        issue
                        for issue in issues_of(item, registry, numbers, party_names, mine)
                        if issue["field"] not in self.access.hidden
                    ],
                    "views": views_module.membership(facts_of(item, registry, mine), registry.views),
                    **({"roles": roles} if (roles := registry.roles_of(item)) else {}),
                    # Кто вписал отдел: сотрудник убирает только вписанный им
                    # самим (`_check_departments_kept`). Одно на всех зрителей -
                    # общий ответ реестра (`SharedBuild`) этим не ломается.
                    **(
                        {"departments_by": by}
                        if (by := {str(row.department_id): str(row.added_by) for row in item.department_rows if row.added_by})
                        else {}
                    ),
                    # Одно на всех зрителей, как и сумма договора.
                    **(
                        {"department_share": share}
                        if book_departments and (share := book_shares(item, book_departments))
                        else {}
                    ),
                    # Договор «другого отдела»: виден, правка - отказ сервера.
                    **({"readonly": True} if read_only_for(item, registry, self.access, mine) else {}),
                    "file_snapshot": (
                        {}
                        if "paid_snapshot" in self.access.hidden
                        else (item.file_snapshot or {})
                    ),
                    "position": item.position,
                    "source": item.source,
                    "created_by": str(item.created_by) if item.created_by else None,
                    "created_at": _plain(item.created_at),
                    "updated_by": (
                        {"id": str(updated_by.id), "short_name": _short_name(updated_by)}
                        if updated_by is not None
                        else None
                    ),
                    "updated_at": _plain(item.updated_at),
                    "deleted": item.deleted_at is not None,
                }
            )
        return out, self.parties(party_ids), self.people(employee_ids)

    def parties(self, ids: Iterable[uuid.UUID]) -> dict[str, Any]:
        registry = self.registry
        found = registry.parties_for(ids)
        out: dict[str, Any] = {}
        for party_id, party in found.items():
            own = registry.own.get(party_id)
            out[str(party_id)] = {
                "id": str(party_id),
                "name": party.name,
                "own": own is not None,
                "code": own.code if own else "",
                "bin": (own.bin if own and own.bin else (party.details or {}).get("bin", "")),
            }
        return out

    def people(self, ids: Iterable[uuid.UUID]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for employee_id, employee in self.registry.employees_for(ids).items():
            out[str(employee_id)] = {
                "id": str(employee_id),
                "name": employee.full_name,
                "department_id": str(employee.department_id) if employee.department_id else None,
            }
        return out


def visible_to(
    contract: Contract,
    registry: Registry,
    access: Access,
    people: Sequence[uuid.UUID],
    *,
    write: bool = False,
) -> bool:
    """Открыт ли договор этому человеку - по строкам и юрлицам.

    `write` - открыт ли он на правку: договор «другого отдела»
    (`view_departments`) виден, но не правится.

    Отделов у договора список, и «своего отдела» - любой из них: договор
    «HR, ЮО» - и договор ЮО. Начальник отдела видит все договоры, где стоит
    его отдел, даже при узкой своей области («где ответственный»), но сверх
    неё - только на чтение (решение владельца 30.09.2026).
    """
    if not access.view or (write and not access.edit):
        return False
    # Договор без сторон - черновик, у которого юрлица ещё нет: по юрлицу его
    # не отнести ни к «своим», ни к чужим. Отсекай его отбор - он пропадал бы
    # у автора сразу после создания, до того как тот выберет сторону.
    parties = {contract.executor_id, contract.customer_id} - {None}
    if access.entity_ids and parties and not (parties & set(access.entity_ids)):
        return False
    departments = set(contract.department_ids)
    if access.rows == "department":
        own = bool(departments & access.department_ids)
    elif access.rows == "own":
        own = access.employee_id is not None and access.employee_id in people
    else:
        return True
    if own or write:
        return own
    if access.head_department is not None and access.head_department in departments:
        return True
    return bool(departments & access.view_departments)


def read_only_for(
    contract: Contract, registry: Registry, access: Access, people: Sequence[uuid.UUID]
) -> bool:
    """Договор виден, но править его нельзя: он из «другого отдела» или открыт
    начальнику сверх его области."""
    return (
        access.edit
        and (bool(access.view_departments) or access.head_department is not None)
        and not visible_to(contract, registry, access, people, write=True)
    )


class ReadOnlyContract(FinanceError):
    """Договор открыт только на просмотр - отказ правке словами."""


def _check_write(contract: Contract, registry: Registry, access: Access, people: Sequence[uuid.UUID]) -> None:
    if not visible_to(contract, registry, access, people, write=True):
        codes = [registry.departments[item].code for item in contract.department_ids if item in registry.departments]
        where = (f"отдела {codes[0]}" if len(codes) == 1 else f"отделов {', '.join(codes)}") if codes else "другого отдела"
        raise ReadOnlyContract(f"Договор {where} открыт вам только на просмотр")


def ensure_writable(session: Session, registry: Registry, access: Access, contract: Contract) -> list[uuid.UUID]:
    """Договор виден и открыт на правку - иначе отказ. Возвращает его людей."""
    people_now = list(people_of(session, [contract.id]).get(contract.id, []))
    if not visible_to(contract, registry, access, people_now):
        raise NotFound("Договор не найден")
    _check_write(contract, registry, access, people_now)
    return people_now


# ── Чтение ───────────────────────────────────────────────────────────────────


def _live_contracts(session: Session, workspace_id: uuid.UUID):
    return (
        sa.select(Contract)
        .where(Contract.workspace_id == workspace_id, Contract.deleted_at.is_(None))
        .order_by(Contract.position, Contract.created_at)
    )


def list_all(
    session: Session,
    workspace: Workspace,
    access: Access,
    *,
    seq_now: int | None = None,
    schema_now: int | None = None,
) -> dict[str, Any]:
    # Номер - до выборки строк, не после: правка, закоммиченная между ними,
    # иначе попала бы под курсор и не пришла бы клиенту ни здесь, ни опросом.
    # Счётчик держит блокировку до коммита, поэтому всё с номером ≤ seq_now
    # уже видно следующему чтению.
    if seq_now is None:
        seq_now = current(session, workspace.id, "contracts")
    if schema_now is None:
        schema_now = current(session, workspace.id, "schema")
    registry = Registry(session, workspace)
    contracts = list(session.scalars(_live_contracts(session, workspace.id)))
    numbers = NumberIndex()
    for item in contracts:
        numbers.add(item)
    output = Output(session, registry, access)
    items, parties, people = output.contracts(contracts, numbers=numbers)
    return {
        "contracts": items,
        "parties": parties,
        "people": people,
        "seq": seq_now,
        "schema_rev": schema_now,
        # По какой сводке посчитаны листы вроде «Остатки»: клиент сверяет с
        # `/contracts/summary` и перечитывает реестр, если сводка обновилась.
        "summary_rev": registry.summary.rev if registry.summary is not None else "",
    }


#: Готовые ответы «весь реестр» - байтами, общие для одинаковых прав.
#:
#: «Утро понедельника»: 20 человек открывают реестр на 10 477 договоров
#: разом. Сборка - около 5 с чистого Python на запрос, и двадцать одинаковых
#: сборок толкались за GIL: медиана ответа 47 с, пятеро получили 500, память
#: 240 → 978 МБ (стресс-прогон 24.09). Ответ зависит только от компании,
#: номера изменений, номера схемы и прав - одинаковые запросы ждут одну
#: сборку и берут готовые байты.
#:
#: Держится недолго (`LIST_CACHE_TTL`): переименование стороны или
#: сотрудника номер договоров не двигает, и старое имя не должно жить
#: дольше минуты. На компанию и набор прав - одна запись, всего не больше
#: `LIST_CACHE_MAX`: 13 МБ на запись у реестра в 10 000 договоров.
LIST_CACHE_TTL = 60.0
LIST_CACHE_MAX = 4


class SharedBuild:
    """Одна сборка на одинаковые запросы; готовые байты держатся недолго.

    Первый запрос с ключом собирает ответ, остальные с тем же ключом ждут его
    и берут готовое - вместо того чтобы собирать то же самое параллельно и
    толкаться за GIL. `slot` - какие записи вытесняет новая (у списка -
    прежние номера той же компании и тех же прав).
    """

    def __init__(self, ttl: float, limit: int, slot: Any = None):
        self.ttl, self.limit, self.slot = ttl, limit, slot
        self._done: dict[tuple[Any, ...], tuple[bytes, float]] = {}
        self._building: dict[tuple[Any, ...], threading.Lock] = {}
        self._guard = threading.Lock()

    def _fresh(self, key: tuple[Any, ...]) -> bytes | None:
        hit = self._done.get(key)
        if hit is not None and time.monotonic() - hit[1] < self.ttl:
            return hit[0]
        return None

    def get(self, key: tuple[Any, ...], build: Any) -> bytes:
        with self._guard:
            body = self._fresh(key)
            if body is not None:
                return body
            building = self._building.setdefault(key, threading.Lock())
        with building:
            with self._guard:
                body = self._fresh(key)
                if body is not None:
                    return body
            body = build()
            with self._guard:
                if self.slot is not None:
                    mine = self.slot(key)
                    for old in [k for k in self._done if self.slot(k) == mine]:
                        del self._done[old]
                self._done[key] = (body, time.monotonic())
                while len(self._done) > self.limit:
                    self._done.pop(next(iter(self._done)))
                self._building.pop(key, None)
            return body


_list_builds = SharedBuild(LIST_CACHE_TTL, LIST_CACHE_MAX, slot=lambda key: (key[0], key[3]))

#: Опрос: 50 вкладок после каждой правки приходят с одним и тем же курсором и
#: собирали один и тот же ответ 50 раз (стресс-прогон 24.09: пустой опрос под
#: нагрузкой ждал 0,5 с в очереди за GIL, правка - 0,84 с). Ответ на пару
#: (since, seq_now) одинаков для одинаковых прав: строки, закоммиченные после
#: seq_now, придут следующим опросом - курсор ответа и есть seq_now.
_change_builds = SharedBuild(10.0, 256)


def _json_default(value: Any) -> Any:
    plain = _plain(value)
    if plain is value:
        raise TypeError(f"не JSON: {type(value).__name__}")
    return plain


def _dump(payload: Any) -> bytes:
    return orjson.dumps(payload, default=_json_default)


def _who(access: Access) -> Access:
    """Права как ключ общей сборки. Сотрудник нужен только при области «где
    ответственный»: иначе люди с одинаковыми правами не делили бы сборку."""
    return access if access.rows == "own" else replace(access, employee_id=None)


def _summary_rev(workspace_id: uuid.UUID) -> str:
    from app.finance.contracts import summary as summary_module

    index = summary_module.peek(workspace_id)
    return index.rev if index is not None else ""


def list_all_bytes(session: Session, workspace: Workspace, access: Access) -> bytes:
    """Весь реестр готовым JSON - одна сборка на одинаковые запросы."""
    seq_now = current(session, workspace.id, "contracts")
    schema_now = current(session, workspace.id, "schema")
    # Сводка и сегодняшний день - тоже часть ответа: «Остатки» зависят от
    # оплат в книге, «до 2 мес» - от даты. Номер изменений их не двигает.
    key = (workspace.id, seq_now, schema_now, _who(access), _summary_rev(workspace.id), today())
    return _list_builds.get(
        key, lambda: _dump(list_all(session, workspace, access, seq_now=seq_now, schema_now=schema_now))
    )


def changes_bytes(session: Session, workspace: Workspace, access: Access, since: int) -> bytes:
    """Опрос готовым JSON: пустой - сразу, с изменениями - одна сборка на курсор."""
    seq_now = current(session, workspace.id, "contracts")
    schema_now = current(session, workspace.id, "schema")
    if seq_now <= since:
        return _dump(
            {"contracts": [], "removed": [], "parties": {}, "people": {}, "seq": seq_now, "schema_rev": schema_now}
        )
    key = (workspace.id, since, seq_now, schema_now, _who(access), _summary_rev(workspace.id), today())
    return _change_builds.get(
        key, lambda: _dump(changes(session, workspace, access, since, seq_now=seq_now, schema_now=schema_now))
    )


def changes(
    session: Session,
    workspace: Workspace,
    access: Access,
    since: int,
    *,
    seq_now: int | None = None,
    schema_now: int | None = None,
) -> dict[str, Any]:
    """Договоры, изменённые после `since`, - вместе с их сторонами и людьми.

    Опрос идёт раз в две секунды от каждой открытой вкладки, и почти всегда
    ответ «ничего не менялось». Поэтому сначала два дешёвых чтения счётчиков,
    и только если номер сдвинулся - сборка справочников и договоров.
    """
    if seq_now is None:
        seq_now = current(session, workspace.id, "contracts")
    if schema_now is None:
        schema_now = current(session, workspace.id, "schema")
    if seq_now <= since:
        return {
            "contracts": [],
            "removed": [],
            "parties": {},
            "people": {},
            "seq": seq_now,
            "schema_rev": schema_now,
        }
    registry = Registry(session, workspace)
    rows = list(
        session.scalars(
            sa.select(Contract)
            .where(Contract.workspace_id == workspace.id, Contract.seq > since)
            .order_by(Contract.seq)
        )
    )
    output = Output(session, registry, access)
    live = [item for item in rows if item.deleted_at is None]
    items, parties, people = output.contracts(live)
    removed = [str(item.id) for item in rows if item.deleted_at is not None]
    # Договор, ушедший из видимости (сменили отдел), для этого человека - убран.
    shown = {item["id"] for item in items}
    removed.extend(str(item.id) for item in live if str(item.id) not in shown)
    # Курсор - номер, прочитанный ДО выборки. Строки, закоммиченные после него,
    # могли попасть в выборку - придут ещё раз следующим опросом, это не
    # страшно; перечитанный здесь номер перескочил бы через них навсегда.
    return {
        "contracts": items,
        "removed": removed,
        "parties": parties,
        "people": people,
        "seq": seq_now,
        "schema_rev": schema_now,
    }


def get_contract(
    session: Session, workspace: Workspace, contract_id: uuid.UUID, *, for_update: bool = False
) -> Contract:
    """Договор компании. `for_update` - с блокировкой строки до конца транзакции.

    Блокировка нужна каждой записи: без неё два одновременных запроса к одному
    полю оба читали старый `field_seq`, оба проходили проверку конфликта, и
    побеждал последний - правка первого пропадала молча (найдено прогоном двух
    окон). С блокировкой второй ждёт первого, читает свежий номер поля и
    получает 409. На SQLite `FOR UPDATE` не существует и тихо опускается.
    """
    if for_update:
        contract = session.scalar(
            sa.select(Contract).where(Contract.id == contract_id).with_for_update()
        )
        if contract is not None:
            session.refresh(contract)
    else:
        contract = session.get(Contract, contract_id)
    if contract is None or contract.workspace_id != workspace.id or contract.deleted_at is not None:
        raise NotFound("Договор не найден")
    return contract


def one(session: Session, workspace: Workspace, access: Access, contract: Contract) -> dict[str, Any]:
    registry = Registry(session, workspace)
    output = Output(session, registry, access)
    items, parties, people = output.contracts([contract])
    if not items:
        raise NotFound("Договор не найден")
    return {"contract": items[0], "parties": parties, "people": people}


# ── Запись ───────────────────────────────────────────────────────────────────


@dataclass
class Mode:
    """Ответ на «опечатка или изменение с даты»."""

    kind: str  # fix | from_date
    effective_from: date | None = None
    number: str = ""
    signed_at: date | None = None
    summary: str = ""

    @classmethod
    def parse(cls, raw: Any) -> Mode | None:
        if not raw:
            return None
        if not isinstance(raw, dict) or raw.get("kind") not in ("fix", "from_date"):
            raise FinanceError("Режим правки: ждём «fix» или «from_date»")
        mode = cls(kind=raw["kind"])
        if mode.kind == "from_date":
            mode.effective_from = read_date(raw.get("effective_from"), field="С какой даты")
            if mode.effective_from is None:
                raise FinanceError("Изменение с даты: укажите дату")
            mode.number = str(raw.get("number") or "").strip()
            mode.signed_at = read_date(raw.get("signed_at"), field="Дата соглашения")
            mode.summary = str(raw.get("summary") or "").strip()
        return mode


@dataclass
class Actor:
    user_id: uuid.UUID | None
    email: str


def _needs_mode(contract: Contract, actor: Actor) -> bool:
    """Спрашивать ли режим: договор существует и заведён не этим человеком сегодня.

    Загруженный из файла договор спрашивает всегда: он «заведён сегодня»
    только формально, а на деле это история, и перевод клиента на другое ТОО
    в день загрузки стёр бы её так же молча, как ячейка Excel.
    """
    if contract.id is None or contract.created_at is None:
        return False
    if contract.source == "import":
        return True
    created = contract.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    same_day = created.astimezone(COMPANY_TZ).date() == today()
    return not (same_day and actor.user_id is not None and contract.created_by == actor.user_id)


def _set_field(
    contract: Contract,
    key: str,
    raw: Any,
    registry: Registry,
    *,
    lists_out: dict[str, list[Any]],
    strict: bool = False,
) -> Any:
    """Разобрать значение поля и записать в договор. Возвращает новое значение API.

    `strict` - ручной ввод (лист, карточка, API): поле заполняется по своему
    способу (`Registry.fill`), закрытый список не заводит новое из опечатки.
    Загрузка Excel идёт без него - там незнакомое решает протокол разбора.

    Списки - людей (`people`) и отделов (`departments`) - только разбираются в
    `lists_out`: пишет их вызывающий, когда прочитаны все поля строки
    (`_write_people`, `_write_departments`).
    """
    title = registry.field_by_key.get(key).title if key in registry.field_by_key else key
    fill = registry.fill(key) if strict else ""
    if key in SNAPSHOT_FIELDS:
        raise FinanceError(f"«{title}» - как было в файле, только чтение")
    if key in LIVE_FIELDS:
        raise FinanceError(f"«{title}» считается по выписке - только чтение")
    if key in SUMMARY_FIELDS:
        raise FinanceError(f"«{title}» берётся из книги-сводки - только чтение")
    if key in DERIVED_FIELDS:
        raise FinanceError(f"«{title}» считается от даты договора - только чтение")
    if key in ("executor", "customer") and fill == "own" and not registry.is_own(
        getattr(contract, COLUMN_OF[_OTHER_SIDE[key]])
    ):
        party = registry.pick_own(raw, title=title)
        setattr(contract, COLUMN_OF[key], party.id if party else None)
    elif key in ("executor", "customer"):
        resolved = registry.resolve_party(raw, slot=key)
        if resolved.ambiguous:
            names = ", ".join(item.name for item in resolved.ambiguous[:3])
            raise FinanceError(f"{title}: подходит несколько - {names}. Выберите в карточке")
        setattr(contract, COLUMN_OF[key], resolved.party.id if resolved.party else None)
    elif key in ("type", "subject", "status", "economic_role"):
        value = registry.pick_value(key, raw) if fill == "list" else registry.resolve_value(key, raw)
        setattr(contract, COLUMN_OF[key], value.id if value else None)
    elif key == "department":
        lists_out["departments"] = registry.input_departments(raw, closed=fill == "list")
    elif key == "people":
        lists_out["people"] = (
            registry.input_people(raw, closed=fill == "list", title=title)
            if strict
            else registry.resolve_people(raw)
        )
    elif key == "amount":
        amount, terms = read_money(raw, field=title)
        contract.amount = amount
        if terms or amount is not None:
            contract.amount_terms = terms
    elif key in DATE_KEYS:
        setattr(contract, key, read_date(raw, field=title))
    elif key == "number":
        contract.number = str(raw or "").strip()
        contract.number_key = number_key(contract.number)
    elif key == "billing":
        text = str(raw or "").strip()
        if text and text not in BILLING_KINDS:
            raise FinanceError("Начисление: в месяц, вся сумма или условие")
        contract.billing = text
    elif key == "end_kind":
        text = str(raw or "").strip()
        if text and text not in END_KINDS:
            raise FinanceError("Смысл даты окончания: расторжение, исполнение или не ясен")
        contract.end_kind = text
    elif key == "currency":
        contract.currency = (str(raw or "").strip().upper() or "KZT")[:3]
    elif key in TEXT_KEYS:
        setattr(contract, key, str(raw or "").strip())
    else:
        contract.attrs = {**(contract.attrs or {}), key: _custom_value(registry, key, raw, fill=fill, strict=strict)}
    # Значение прочиталось - прежний непрочитанный текст этого поля больше не
    # замечание.
    raw_texts = (contract.attrs or {}).get(RAW_KEY) or {}
    if key in raw_texts:
        rest = {k: v for k, v in raw_texts.items() if k != key}
        attrs = {k: v for k, v in (contract.attrs or {}).items() if k != RAW_KEY}
        contract.attrs = {**attrs, **({RAW_KEY: rest} if rest else {})}
    return None


def _custom_value(registry: Registry, key: str, raw: Any, *, fill: str = "", strict: bool = False) -> Any:
    """Значение своего поля по его типу и способу заполнения."""
    item = registry.field_by_key.get(key)
    if item is None:
        raise FinanceError(f"Поля «{key}» в реестре нет")
    if raw is None or raw == "" or raw == []:
        return None
    kind = item.type
    if kind in ("text", "url"):
        return str(raw).strip()
    if kind in ("number", "money"):
        amount, terms = read_money(raw, field=item.title)
        if amount is None:
            raise FinanceError(f"{item.title}: «{terms}» не похоже на число")
        return format(amount, "f")
    if kind == "date":
        value = read_date(raw, field=item.title)
        return value.isoformat() if value else None
    if kind == "bool":
        return str(raw).strip().lower() in ("1", "true", "да", "yes", "✓", "истина")
    closed = fill == "list"
    if kind == "list":
        value = registry.pick_value(key, raw) if closed else registry.resolve_value(key, raw)
        return str(value.id) if value else None
    if kind == "multi_list":
        parts = raw if isinstance(raw, list) else re.split(r"[,;\n]+", str(raw))
        values = [registry.pick_value(key, part) if closed else registry.resolve_value(key, part) for part in parts]
        return [str(value.id) for value in values if value is not None]
    if kind == "person":
        people = (
            registry.input_people(raw, closed=closed, title=item.title) if strict else registry.resolve_people(raw)
        )
        return [str(employee.id) for employee in people]
    if kind == "party":
        if fill == "own":
            party = registry.pick_own(raw, title=item.title)
            return str(party.id) if party else None
        resolved = registry.resolve_party(raw, slot="customer")
        return str(resolved.party.id) if resolved.party else None
    if kind == "department":
        department = registry.pick_department(raw) if closed else registry.resolve_department(raw)
        return str(department.id) if department else None
    return str(raw)


def _write_people(session: Session, contract: Contract, people: list[Employee]) -> None:
    """Ответственные договора по порядку - с их долями.

    Доля принадлежит человеку в договоре, а не строке: правка поля
    «Ответственное лицо» (дописали третьего, поменяли порядок) переписывает
    строки заново, и без переноса долей каждая такая правка молча стирала бы
    распределение, сделанное начальником (`shares.py`). Ушедший из поля
    уходит вместе со своей долей.
    """
    kept = {
        employee_id: (amount, percent)
        for employee_id, amount, percent in session.execute(
            sa.select(ContractPerson.employee_id, ContractPerson.share_amount, ContractPerson.share_percent).where(
                ContractPerson.contract_id == contract.id
            )
        )
    }
    session.execute(sa.delete(ContractPerson).where(ContractPerson.contract_id == contract.id))
    for index, employee in enumerate(people):
        amount, percent = kept.get(employee.id, (None, None))
        session.add(
            ContractPerson(
                contract_id=contract.id,
                employee_id=employee.id,
                position=index,
                share_amount=amount,
                share_percent=percent,
            )
        )


def _write_departments(contract: Contract, departments: list[Department], by: uuid.UUID | None) -> None:
    """Отделы договора по порядку - с их долями, как `_write_people`.

    Доля принадлежит отделу в договоре: дописали отдел или поменяли порядок -
    доли остальных на месте. Ушедший из поля уходит вместе со своей долей
    (кому это можно - `_check_departments_kept`). У нового отдела запоминается,
    кто его вписал (`by`).
    """
    kept = {row.department_id: row for row in contract.department_rows}
    rows: list[ContractDepartment] = []
    for index, department in enumerate(departments):
        row = kept.get(department.id) or ContractDepartment(department_id=department.id, added_by=by)
        row.position = index
        rows.append(row)
    contract.department_rows = rows


def _check_departments_kept(
    access: Access, registry: Registry, contract: Contract, departments: list[Department], by: uuid.UUID | None
) -> None:
    """Убрать отдел из договора - администратор или владелец (решение владельца 30.09.2026).

    Сотрудник убирает только отдел, который вписал сам и у которого ещё нет
    доли: свою ошибку он исправляет сам, а вписанное другими, пришедшее из книг
    и уже разделённое деньгами - нет. Везде: карточка, ячейка листа, вставка,
    «Вернуть», доли отделов. Замена отдела (HR → ЮО) - тоже «убрать».
    """
    if access.admin:
        return
    wanted = {item.id for item in departments}
    gone = [row for row in contract.department_rows if row.department_id not in wanted]

    def code(row: ContractDepartment) -> str:
        department = registry.departments.get(row.department_id)
        return department.code if department is not None else "?"

    foreign = [code(row) for row in gone if by is None or row.added_by != by]
    if foreign:
        what = f"Отдел {foreign[0]}" if len(foreign) == 1 else f"Отделы {', '.join(foreign)}"
        whom = "его" if len(foreign) == 1 else "их"
        raise PermissionError(f"{what} в договор вписали не вы - убрать {whom} может администратор или владелец")
    shared = [code(row) for row in gone if row.share_amount is not None or row.share_percent is not None]
    if shared:
        what = f"У отдела {shared[0]} уже есть доля" if len(shared) == 1 else f"У отделов {', '.join(shared)} уже есть доли"
        whom = "его" if len(shared) == 1 else "их"
        raise PermissionError(f"{what} - убрать {whom} может администратор или владелец")


def _label(registry: Registry, key: str, value: Any) -> str:
    """Значение поля словами - для истории: «BBCA», «500 000», «01.02.2024»."""
    if value in (None, "", []):
        return "-"
    try:
        if key in ("executor", "customer"):
            party_id = uuid.UUID(str(value))
            party = registry.parties_for([party_id]).get(party_id)
            return party.name if party else str(value)
        if key in LIST_KEYS:
            item = registry.values.get(uuid.UUID(str(value)))
            return item.value if item else str(value)
        if key == "department":
            ids = [uuid.UUID(str(item)) for item in (value if isinstance(value, (list, tuple)) else [value])]
            return ", ".join(registry.departments[item].code if item in registry.departments else str(item) for item in ids) or "-"
        if key == "people":
            found = registry.employees_for(uuid.UUID(str(item)) for item in value)
            names = [found[uuid.UUID(str(item))].full_name for item in value if uuid.UUID(str(item)) in found]
            return ", ".join(names) or "-"
        if key == "amount":
            return f"{Decimal(str(value)):,.2f}".replace(",", " ").replace(".00", "")
        if key in DATE_KEYS:
            return date.fromisoformat(str(value)).strftime("%d.%m.%Y")
        if key in CHOICES:
            # До 30.09.2026 в журнал уходил ключ: «смысл даты окончания: - → terminated».
            return dict(CHOICES[key]).get(str(value), str(value))
    except (ValueError, KeyError):
        return str(value)
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "…"


def _history_title(registry: Registry, before: dict[str, Any], after: dict[str, Any], prefix: str) -> str:
    parts = []
    for key in after:
        title = registry.field_by_key[key].title if key in registry.field_by_key else key
        parts.append(f"{title.lower()}: {_label(registry, key, before.get(key))} → {_label(registry, key, after.get(key))}")
    text = f"{prefix} · " + "; ".join(parts) if parts else prefix
    return text if len(text) <= 400 else text[:397] + "…"


def _check_access(registry: Registry, access: Access, keys: Iterable[str]) -> None:
    for key in keys:
        if key not in registry.field_by_key:
            raise FinanceError(f"Поля «{key}» в реестре нет")
        if key in access.hidden:
            # Отказ без подписи поля: она не должна доезжать до того, от кого
            # поле спрятано.
            raise PermissionError("Это поле вам не открыто")
        if not access.can_edit_field(key):
            title = registry.field_by_key[key].title
            raise PermissionError(f"«{title}» вам можно только смотреть")


def _finish(
    session: Session,
    registry: Registry,
    contract: Contract,
    actor: Actor,
    changed: Iterable[str],
) -> None:
    seq = bump(session, registry.workspace.id, "contracts")
    contract.seq = seq
    field_seq = dict(contract.field_seq or {})
    for key in changed:
        field_seq[key] = seq
    contract.field_seq = field_seq
    contract.updated_by = actor.user_id
    contract.updated_at = _now()
    session.flush()


def _next_position(session: Session, workspace_id: uuid.UUID) -> int:
    top = session.scalar(
        sa.select(sa.func.max(Contract.position)).where(Contract.workspace_id == workspace_id)
    )
    return int(top or 0) + POSITION_STEP


def _position_before(session: Session, workspace_id: uuid.UUID, before: uuid.UUID | None) -> int:
    """Место перед договором `before` в порядке реестра; места нет - в конец.

    Строка, вставленная посреди листа и ставшая договором, должна и после
    пересборки стоять там, где её вставили, а не уезжать в конец блока.
    """
    if before is None:
        return _next_position(session, workspace_id)
    upper = session.scalar(
        sa.select(Contract.position).where(Contract.id == before, Contract.workspace_id == workspace_id)
    )
    if upper is None:
        return _next_position(session, workspace_id)
    lower = session.scalar(
        sa.select(sa.func.max(Contract.position)).where(
            Contract.workspace_id == workspace_id, Contract.position < upper
        )
    )
    lower = int(lower) if lower is not None else int(upper) - POSITION_STEP
    if int(upper) - lower < 2:
        return _next_position(session, workspace_id)
    return (lower + int(upper)) // 2


def create(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    values: dict[str, Any],
    *,
    view_key: str | None = None,
    block: int | None = None,
    source: str = "app",
    before: uuid.UUID | None = None,
) -> Contract:
    """Завести договор. Подстановки блока ставит сервер.

    Пустая строка кармана блока «АРЕНДА» заводит договор уже с видом, предметом,
    начислением и смыслом этого блока - человек печатает только своё.
    `before` - договор, перед которым встать в порядке реестра (строка,
    вставленная посреди листа).
    """
    if not access.edit:
        raise PermissionError("Заводить договоры вам не открыто")
    registry = Registry(session, workspace)
    contract = Contract(
        workspace_id=workspace.id,
        source=source,
        position=_position_before(session, workspace.id, before),
        created_by=actor.user_id,
        created_at=_now(),
        attrs={},
        provenance={},
        acknowledged={},
        field_seq={},
        file_snapshot={},
    )
    session.add(contract)
    session.flush()
    defaults = _block_defaults(registry, view_key, block)
    lists: dict[str, list[Any]] = {}
    changed: set[str] = set()
    for key, raw in defaults.items():
        _set_field(contract, key, raw, registry, lists_out=lists)
        changed.add(key)
    provenance = dict(contract.provenance or {})
    for key in ("billing", "economic_role"):
        if key in defaults:
            provenance[key] = "block"
    contract.provenance = provenance
    user_keys = _party_order(registry, [key for key in values if key not in ("id",)])
    _check_access(registry, access, user_keys)
    for key in user_keys:
        try:
            _set_field(contract, key, values[key], registry, lists_out=lists, strict=True)
        except NotInList:
            # Строка листа с «им» в статусе всё равно договор: он заводится, а
            # напечатанное остаётся замечанием у поля - ««им» нет в списке». Отказ
            # целиком стёр бы набранную строку из-за одной ячейки.
            _keep_unread(contract, key, values[key])
            continue
        changed.add(key)
        if key in ("billing", "economic_role", "end_kind"):
            contract.provenance = {**(contract.provenance or {}), key: "manual"}
    _derive(contract, registry, changed | {"type", "subject", "executor", "customer", "end_date"})
    # Договор, заведённый при узкой области строк, остаётся в этой области.
    # Иначе он пропадал у автора сразу после создания, и следующая правка поля
    # получала «Договор не найден»: «где ответственный» - автор становится
    # ответственным, «своего отдела» - к отделам договора дописывается его
    # отдел (до 30.09.2026 отдел был один и напечатанный заменялся).
    if access.rows == "own" and access.employee_id is not None:
        listed = lists.get("people")
        if listed is None:
            listed = [
                employee
                for employee_id in people_of(session, [contract.id]).get(contract.id, [])
                if (employee := session.get(Employee, employee_id)) is not None
            ]
        if all(employee.id != access.employee_id for employee in listed):
            author = session.get(Employee, access.employee_id)
            if author is not None:
                lists["people"] = [*listed, author]
                changed.add("people")
    if access.rows == "department" and access.department_ids:
        chosen = lists.get("departments")
        if chosen is None:
            chosen = [registry.departments[item] for item in contract.department_ids if item in registry.departments]
        own = [registry.departments[item] for item in access.department_ids if item in registry.departments]
        if own and not any(item.id in access.department_ids for item in chosen):
            lists["departments"] = [*chosen, own[0]]
            changed.add("department")
    if "people" in lists:
        _write_people(session, contract, lists["people"])
    if "departments" in lists:
        _write_departments(contract, lists["departments"], actor.user_id)
    _finish(session, registry, contract, actor, changed | {"billing", "economic_role", "end_kind"})
    history.write(
        session,
        workspace,
        kind="contract.create",
        entity="contract",
        entity_id=contract.id,
        title=_history_title(registry, {}, {}, f"договор заведён {contract.number or ''}".strip()),
        after={key: value_of(contract, key, [e.id for e in lists.get("people", [])]) for key in changed},
        actor=actor.email,
    )
    return contract


#: «Наша сторона - только наши юрлица» относится к той стороне, где стоит
#: наше юрлицо. В реестре BBC есть и покупки: в листе «Заказчик ГК» наше ТОО -
#: заказчик, а исполнитель - «Халык Актив». Если напротив уже стоит наше
#: юрлицо, эта сторона может быть чужой: иначе покупку нельзя было бы завести
#: руками вовсе.
_OTHER_SIDE = {"executor": "customer", "customer": "executor"}


def _party_order(registry: Registry, keys: Iterable[str]) -> list[str]:
    """Сначала сторона, открытая для всех, потом закрытая на наши: закрытой
    нужно знать, наша ли сторона напротив."""
    return sorted(keys, key=lambda key: 1 if key in _OTHER_SIDE and registry.fill(key) == "own" else 0)


def _other_own(registry: Registry, contract: Contract, key: str, values: dict[str, Any]) -> bool:
    """Стоит ли напротив стороны `key` наше юрлицо - после этой правки."""
    other = _OTHER_SIDE.get(key)
    if other is None:
        return False
    if other not in values:
        return registry.is_own(getattr(contract, COLUMN_OF[other]))
    try:
        # Тем же поиском, что и сама закрытая сторона: «BBCA», «omar».
        return registry.pick_own(values[other], title=registry.title(other)) is not None
    except NotInList:
        return False


def _keep_unread(contract: Contract, key: str, raw: Any) -> None:
    """Запомнить непринятое значение поля - оно станет замечанием `unread_<поле>`."""
    text = ", ".join(str(item) for item in raw) if isinstance(raw, (list, tuple)) else str(raw or "")
    text = text.strip()
    if not text:
        return
    attrs = dict(contract.attrs or {})
    attrs[RAW_KEY] = {**(attrs.get(RAW_KEY) or {}), key: text[:200]}
    contract.attrs = attrs


def _block_defaults(registry: Registry, view_key: str | None, block: int | None) -> dict[str, Any]:
    if not view_key:
        return {}
    view = next((item for item in registry.views if item.key == view_key), None)
    if view is None or block is None or not (0 <= block < len(view.blocks or [])):
        return {}
    defaults = dict((view.blocks[block] or {}).get("defaults") or {})
    allowed = {"type", "subject", "status", "billing", "economic_role", "department"}
    return {key: value for key, value in defaults.items() if key in allowed and value not in (None, "")}


def patch(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    contract_id: uuid.UUID,
    values: dict[str, Any],
    *,
    known_seq: int | None,
    mode: Mode | None = None,
) -> Contract:
    """Правка полей договора.

    Конфликт - по полю: 409 только если одно из присланных полей менялось
    после `known_seq`. Правка другого поля того же договора не мешает.
    """
    registry = Registry(session, workspace)
    contract = get_contract(session, workspace, contract_id, for_update=True)
    people_now = people_of(session, [contract.id]).get(contract.id, [])
    if not visible_to(contract, registry, access, people_now):
        raise NotFound("Договор не найден")
    _check_write(contract, registry, access, people_now)
    keys = _party_order(registry, values)
    _check_access(registry, access, keys)
    for key in keys:
        registry.check_closed(key, values[key], other_own=_other_own(registry, contract, key, values))
    if "department" in values:
        wanted = registry.input_departments(values["department"], closed=registry.fill("department") == "list")
        _check_departments_kept(access, registry, contract, wanted, actor.user_id)
    if known_seq is not None:
        field_seq = contract.field_seq or {}
        conflicts = [key for key in keys if int(field_seq.get(key, 0)) > known_seq]
        if conflicts:
            raise FieldConflict(contract, conflicts)

    tracked = set(keys) | DERIVED
    before = {key: value_of(contract, key, people_now) for key in tracked}
    # Режим спрашивается только там, где значение правда меняется.
    moded = _probe_changes(contract, registry, values, before)
    if moded and _needs_mode(contract, actor) and mode is None:
        raise ModeRequired(moded)
    dated = mode is not None and mode.kind == "from_date"

    lists: dict[str, list[Any]] = {}
    touched: set[str] = set()
    for key in keys:
        if dated and key in moded:
            continue
        _set_field(contract, key, values[key], registry, lists_out=lists, strict=True)
        if key in DERIVED:
            contract.provenance = {**(contract.provenance or {}), key: "manual"}
        touched.add(key)
    if "people" in lists:
        _write_people(session, contract, lists["people"])
        session.flush()
    if "departments" in lists:
        _write_departments(contract, lists["departments"], actor.user_id)
        session.flush()

    amended: set[str] = set()
    if dated:
        for key in moded:
            amendment = _amend(session, registry, contract, actor, key, values[key], mode)
            amended.add(key)
            if amendment.applied_at is not None:
                touched.add(key)
    _derive(contract, registry, touched)
    after_people = [e.id for e in lists["people"]] if "people" in lists else people_now
    after = {key: value_of(contract, key, after_people) for key in tracked}
    real = {key for key in tracked if after[key] != before[key]}
    if not real and not amended:
        return contract
    _finish(session, registry, contract, actor, real)
    logged = real - amended
    if logged:
        history.write(
            session,
            workspace,
            kind="contract.update",
            entity="contract",
            entity_id=contract.id,
            title=_history_title(
                registry,
                {key: before[key] for key in logged},
                {key: after[key] for key in logged},
                f"договор {contract.number or ''}".strip()
                + (" · опечатка" if mode is not None and mode.kind == "fix" else ""),
            ),
            before={key: before[key] for key in logged},
            after={key: after[key] for key in logged},
            actor=actor.email,
        )
    return contract


#: Поля, которые система подставляет сама, пока их не задал человек.
DERIVED = frozenset({"billing", "economic_role", "end_kind"})


def _probe_changes(
    contract: Contract, registry: Registry, values: dict[str, Any], before: dict[str, Any]
) -> list[str]:
    """Какие из присланных полей стороны и суммы правда меняют значение.

    Сравнение без записи: «BBC» в ячейке, где уже стоит BBC, - не изменение,
    и спрашивать «опечатка или с даты» на нём было бы шумом.
    """
    out: list[str] = []
    for key in MODE_FIELDS:
        if key not in values:
            continue
        raw = values[key]
        if key == "amount":
            try:
                amount, terms = read_money(raw, field="Сумма")
            except FinanceError:
                out.append(key)
                continue
            if amount != contract.amount or (amount is None and terms != (contract.amount_terms or "")):
                out.append(key)
            continue
        text = str(raw.get("id") if isinstance(raw, dict) else raw or "").strip()
        resolved = registry.resolve_party(raw, slot=key, create=False)
        new_id = str(resolved.party.id) if resolved.party else ("new" if text else None)
        if new_id != before.get(key):
            out.append(key)
    return out


def _amend(
    session: Session,
    registry: Registry,
    contract: Contract,
    actor: Actor,
    key: str,
    raw: Any,
    mode: Mode,
) -> ContractAmendment:
    """Соглашение «с даты»: старое значение действует до даты.

    Дата сегодня или раньше - значение договора становится новым сразу. Дата в
    будущем - договор держит старое, а соглашение стоит в ленте «впереди»;
    в свой день его применит `apply_due`.
    """
    before_value = value_of(contract, key)
    scratch = Contract(attrs={}, provenance={}, file_snapshot={})
    _set_field(scratch, key, raw, registry, lists_out={}, strict=True)
    after_value = value_of(scratch, key)
    effect = key if key in ("executor", "customer", "amount") else "other"
    amendment = ContractAmendment(
        workspace_id=registry.workspace.id,
        contract_id=contract.id,
        number=mode.number,
        signed_at=mode.signed_at,
        summary=mode.summary,
        effect=effect,
        effective_from=mode.effective_from,
        before={key: before_value},
        after={key: after_value, **({"amount_terms": scratch.amount_terms} if key == "amount" else {})},
        origin="change",
        position=_next_amendment_position(session, contract.id),
        created_by=actor.user_id,
    )
    session.add(amendment)
    if mode.effective_from is not None and mode.effective_from <= today():
        _apply_amendment(contract, amendment, registry)
    session.flush()
    history.write(
        session,
        registry.workspace,
        kind="contract.amendment",
        entity="contract",
        entity_id=contract.id,
        title=(
            f"договор {contract.number or ''} · изменение с {mode.effective_from:%d.%m.%Y}: "
            f"{_label(registry, key, before_value)} → {_label(registry, key, after_value)}"
        ).replace("  ", " "),
        before={key: before_value},
        after={key: after_value, "effective_from": _plain(mode.effective_from)},
        actor=actor.email,
    )
    return amendment


def _apply_amendment(contract: Contract, amendment: ContractAmendment, registry: Registry) -> None:
    after = amendment.after or {}
    if amendment.effect in ("executor", "customer"):
        value = after.get(amendment.effect)
        setattr(contract, COLUMN_OF[amendment.effect], uuid.UUID(value) if value else None)
    elif amendment.effect == "amount":
        value = after.get("amount")
        contract.amount = Decimal(value) if value not in (None, "") else None
        contract.amount_terms = after.get("amount_terms", "") or ""
    elif amendment.effect == "end_date":
        value = after.get("end_date")
        contract.end_date = date.fromisoformat(value) if value else None
    amendment.applied_at = _now()


def _next_amendment_position(session: Session, contract_id: uuid.UUID) -> int:
    top = session.scalar(
        sa.select(sa.func.max(ContractAmendment.position)).where(
            ContractAmendment.contract_id == contract_id
        )
    )
    return int(top or 0) + 1


def remove(
    session: Session, workspace: Workspace, access: Access, actor: Actor, contract_id: uuid.UUID
) -> None:
    if not access.edit:
        raise PermissionError("Удалять договоры вам не открыто")
    registry = Registry(session, workspace)
    contract = get_contract(session, workspace, contract_id, for_update=True)
    people_now = people_of(session, [contract.id]).get(contract.id, [])
    if not visible_to(contract, registry, access, people_now):
        raise NotFound("Договор не найден")
    _check_write(contract, registry, access, people_now)
    contract.deleted_at = _now()
    _finish(session, registry, contract, actor, [])
    history.write(
        session,
        workspace,
        kind="contract.delete",
        entity="contract",
        entity_id=contract.id,
        title=f"договор удалён в корзину {contract.number or ''}".strip(),
        before={"deleted_at": None},
        after={"deleted_at": _plain(contract.deleted_at)},
        actor=actor.email,
    )


def acknowledge(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    contract_id: uuid.UUID,
    code: str,
    *,
    on: bool,
) -> Contract:
    """«Так и должно быть» у замечания - или снять отметку."""
    if not access.edit:
        raise PermissionError("Отмечать замечания вам не открыто")
    registry = Registry(session, workspace)
    contract = get_contract(session, workspace, contract_id, for_update=True)
    numbers = number_index_for(session, workspace.id, [contract.number_key])
    named = {pid for entries in numbers.by_key.values() for _cid, pair in entries for pid in pair if pid}
    names = {pid: party.name for pid, party in registry.parties_for(named).items()}
    people_now = people_of(session, [contract.id]).get(contract.id, [])
    # Отметка - правка договора: чужой или «только просмотр» её не ставит.
    if not visible_to(contract, registry, access, people_now):
        raise NotFound("Договор не найден")
    _check_write(contract, registry, access, people_now)
    current_issues = {
        issue["code"]: issue for issue in issues_of(contract, registry, numbers, names, people_now)
    }
    acked = dict(contract.acknowledged or {})
    if on:
        issue = current_issues.get(code)
        if issue is None:
            raise FinanceError("Такого замечания у договора нет")
        acked[code] = {"by": actor.email, "at": _plain(_now()), "ref": issue.get("ref", "")}
    else:
        acked.pop(code, None)
    contract.acknowledged = acked
    _finish(session, registry, contract, actor, [])
    history.write(
        session,
        workspace,
        kind="contract.acknowledge",
        entity="contract",
        entity_id=contract.id,
        title=(
            f"договор {contract.number or ''} · «так и должно быть»: {code}"
            if on
            else f"договор {contract.number or ''} · отметка снята: {code}"
        ),
        after={"code": code, "on": on},
        actor=actor.email,
    )
    return contract


def history_of(
    session: Session,
    workspace: Workspace,
    contract_id: uuid.UUID,
    *,
    limit: int = 50,
    before: datetime | None = None,
    hide: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """История договора: кто, когда, что было и что стало.

    `hide` - виды событий, которых этому человеку не показывать: доли
    исполнителей видят не все, а запись о них несёт суммы (`shares.py`).
    """
    from app.finance.models import ActionLog

    get_contract(session, workspace, contract_id)
    query = (
        sa.select(ActionLog)
        .where(ActionLog.workspace_id == workspace.id, ActionLog.entity_id == contract_id)
        .order_by(ActionLog.at.desc())
        .limit(min(max(limit, 1), 200))
    )
    hidden = list(hide)
    if hidden:
        query = query.where(ActionLog.kind.not_in(hidden))
    if before is not None:
        query = query.where(ActionLog.at < before)
    entries = list(session.scalars(query))
    # Автор - именем сотрудника, а не логином: юрист входит по номеру, и в
    # истории договора стояло «+77477683832 · сумма: …» вместо «Жанель».
    users = {entry.user_id for entry in entries if entry.user_id is not None}
    names = (
        {
            user_id: name
            for user_id, name in session.execute(
                sa.select(Employee.user_id, Employee.full_name).where(
                    Employee.workspace_id == workspace.id, Employee.user_id.in_(users)
                )
            )
            if name
        }
        if users
        else {}
    )
    return [
        {
            "id": str(entry.id),
            "at": _plain(entry.at),
            "actor": names.get(entry.user_id, entry.actor) if entry.user_id is not None else entry.actor,
            "kind": entry.kind,
            "title": entry.title,
            "before": entry.before or {},
            "after": entry.after or {},
        }
        for entry in entries
    ]


# ── Соглашения, чей день настал ──────────────────────────────────────────────


def apply_due(session: Session) -> int:
    """Применить соглашения «с даты», чей день настал. Возвращает их число.

    Один запрос по индексу `(applied_at, effective_from)` на все компании;
    ничего не держится в памяти между вызовами. Зовётся фоновой задачей раз в
    час и при старте.
    """
    due = list(
        session.scalars(
            sa.select(ContractAmendment)
            .where(
                ContractAmendment.applied_at.is_(None),
                ContractAmendment.effective_from.is_not(None),
                ContractAmendment.effective_from <= today(),
                ContractAmendment.effect.in_(("executor", "customer", "amount", "end_date")),
            )
            .order_by(ContractAmendment.effective_from, ContractAmendment.position)
            .limit(500)
        )
    )
    applied = 0
    registries: dict[uuid.UUID, Registry] = {}
    for amendment in due:
        contract = session.scalar(
            sa.select(Contract).where(Contract.id == amendment.contract_id).with_for_update()
        )
        if contract is None or contract.deleted_at is not None:
            amendment.applied_at = _now()
            continue
        workspace = session.get(Workspace, contract.workspace_id)
        if workspace is None:
            continue
        registry = registries.get(workspace.id) or Registry(session, workspace)
        registries[workspace.id] = registry
        before = value_of(contract, amendment.effect)
        _apply_amendment(contract, amendment, registry)
        _derive(contract, registry, {amendment.effect})
        _finish(session, registry, contract, Actor(None, "система"), [amendment.effect])
        history.write(
            session,
            workspace,
            kind="contract.amendment.apply",
            entity="contract",
            entity_id=contract.id,
            title=(
                f"договор {contract.number or ''} · по соглашению {amendment.number or ''} "
                f"с {amendment.effective_from:%d.%m.%Y}"
            ).replace("  ", " "),
            before={amendment.effect: before},
            after={amendment.effect: value_of(contract, amendment.effect)},
            actor="система",
        )
        applied += 1
    return applied


__all__ = [
    "Access",
    "Actor",
    "COMPANY_TZ",
    "FieldConflict",
    "Mode",
    "ModeRequired",
    "NotFound",
    "Output",
    "Registry",
    "access_of",
    "acknowledge",
    "apply_due",
    "changes",
    "create",
    "get_contract",
    "issues_of",
    "list_all",
    "looks_numeric",
    "one",
    "patch",
    "read_date",
    "read_money",
    "remove",
    "today",
    "value_of",
]
