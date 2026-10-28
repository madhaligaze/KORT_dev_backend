"""Виды - сохранённые вопросы к ядру (этап 2, шаг 6).

Вид = отбор + меры + группировки + форма + владелец. «Реестр», вид отдела,
«Долги», «Не выставлены» - виды, а не разделы, зашитые в код, и колонка
разделов собирается из закреплённых видов. Новая компания начинает с
«Реестра» и «Долгов»; всё остальное - сохранённый вид: свой или из шаблона
отрасли («Разовые договоры по возрасту долга» отдел сохраняет себе).

Источник вида
─────────────
* `contracts` - договоры листами: вид - книга листов (`entity_views.book` -
  ключ вида), формы «карточки» и «таблица». Реестр - вид с ключом `""`:
  его главный лист держит все договоры.
* `debts` - «Долги» (`core/debts.py`): сторона, основание и группа - в
  параметрах, отбор вида сужает договоры.
* `unbilled` - «Не выставлены» (`contracts/bills.py`): юрлицо и отдел - в
  параметрах.

Отбор вида
──────────
Правило того же языка, что у листа (`contracts/views.py`). Договор стоит в
листе вида, если подходит под отбор вида и под правило блока листа; у вида с
отбором блок без правила - весь вид. Пустой отбор - все договоры, а
листы, как прежде, отбирают каждый своё.

Меры
────
Меры - итоги ядра, посчитанные на день, а не поля договора и не формулы
человека (формулы вернули бы сверки): «Оплачено», «Остаток» (сумма минус
оплачено), «Срок, мес» (полные тридцатидневные месяцы от даты договора - так
считали отборы по возрасту) и «Доля отдела» - сумма договора долей
отдела-владельца вида. Ключи колонок - прежние ключи полей (`summary_paid`,
`summary_remaining`, `age_months`): на них стоят листы, личный вид и права
компаний. Переименовать - миграция, а не косметика.

«Оплачено» - из сводной таблицы, если юрлицо договора на ней; юрлицо на KORT
(«Сверка → Переход») или компания без сводной - оплаты KORT из регистра
итогов (`Registry.summary_of`).

Владелец и колонка
──────────────────
Владелец - компания, отдел или человек. Править вид может администратор,
начальник отдела-владельца, человек - свой. `pin` - кому вид стоит в колонке:
всем, кто видит его данные (`all`), владельцу (`owner`: людям отдела или
самому человеку) или никому (вид открывается из «Ещё»).
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.finance import history
from app.finance.contracts import views as rules
from app.finance.contracts.models import Department, EntityView, ListValue
from app.finance.core.models import VIEW_PINS, VIEW_SOURCES, View
from app.finance.models import Workspace
from app.finance import packs
from app.finance.packs import Template
from app.finance.service import FinanceError

REGISTRY_KEY = ""
POSITION_STEP = 10


# ── Каталоги ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Source:
    title: str
    #: Право, которое открывает данные вида (`app/finance/access.py`).
    resource: str
    forms: tuple[str, ...]
    groups: tuple[str, ...]


SOURCES: dict[str, Source] = {
    "contracts": Source("Договоры", "contracts", ("cards", "table"), ("people", "age")),
    "debts": Source("Долги", "reports.debts", ("list",), ()),
    "unbilled": Source("Не выставлены", "invoices", ("list",), ()),
}
FORM_TITLES = {"cards": "Карточки", "table": "Таблица", "list": "Список"}
GROUP_TITLES = {"people": "По сотрудникам", "age": "По возрасту"}

#: Параметры «Долгов»: сторона, основание, группа (`core/debts.py`).
DEBT_SIDES = ("receivable", "payable", "intra")
DEBT_BASES = ("accruals", "acts")
DEBT_GROUPS = ("client", "entity", "department", "person")


@dataclass(frozen=True)
class Measure:
    key: str
    title: str
    #: `money`, `number`; `share` - не колонка, а способ считать сумму.
    type: str
    note: str = ""


MEASURES: tuple[Measure, ...] = (
    Measure("share", "Доля отдела", "share", "сумма договора - доля отдела-владельца вида"),
    Measure("summary_paid", "Оплачено", "money", "оплаты договора: из сводной таблицы или KORT"),
    Measure("summary_remaining", "Остаток", "money", "сумма договора минус оплачено"),
    Measure("age_months", "Срок, мес", "number", "полных месяцев по 30 дней от даты договора"),
)
MEASURE_BY_KEY = {item.key: item for item in MEASURES}
#: Меры, которые бывают колонкой листа и условием отбора.
COLUMN_MEASURES = tuple(item.key for item in MEASURES if item.type != "share")


@dataclass(frozen=True)
class MeasureField:
    """Мера колонкой листа - для кода, который спрашивает о колонке как о поле
    (`registry.field_by_key`: тип для выгрузки, подпись для журнала, проверка
    правила листа). Не строка `entity_fields`: в карточке и настройке полей её нет."""

    key: str
    type: str
    title: str
    system: bool = True
    hidden: bool = False
    fill: str = ""
    position: int = 0
    names: tuple[str, ...] = ()
    id: None = None
    archived_at: None = None


def measure_fields() -> dict[str, MeasureField]:
    return {item.key: MeasureField(item.key, item.type, item.title) for item in MEASURES if item.key in COLUMN_MEASURES}


# ── Шаблоны ──────────────────────────────────────────────────────────────────


#: Шаблоны ядра: у каждого источника свой вид. Шаблоны сверх них («Разовые
#: договоры по возрасту долга») приносит пакет отрасли (`templates_of`).
CORE_TEMPLATES: dict[str, Template] = {
    "registry": Template("registry", "Реестр", "contracts", "все договоры компании", forms=("cards", "table")),
    "debts": Template(
        "debts", "Долги", "debts", "кто сколько должен по договорам на любой день",
        forms=("list",), params={"side": "receivable", "basis": "accruals", "group": "client"},
    ),
    "unbilled": Template("unbilled", "Не выставлены", "unbilled", "счета, которые пора выставить", forms=("list",)),
}


def templates_of(workspace: Workspace | None) -> dict[str, Template]:
    """Шаблоны ядра и отрасли компании."""
    industry = packs.industry_of(workspace)
    return {**CORE_TEMPLATES, **{item.key: item for item in industry.templates}}


#: Шаблоны отрасли по умолчанию - для кода без компании.
TEMPLATES: dict[str, Template] = templates_of(None)


# ── Кто смотрит ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Viewer:
    """Кто спрашивает: учётка, отдел, начальник ли, уровни прав по разделам."""

    user_id: uuid.UUID | None
    admin: bool = False
    department_id: uuid.UUID | None = None
    head_department: uuid.UUID | None = None
    levels: Mapping[str, str] = field(default_factory=dict)

    def sees(self, resource: str) -> bool:
        return self.admin or self.levels.get(resource, "none") != "none"


def viewer_of(member: Any) -> Viewer:
    rights = getattr(member, "rights", None)
    if rights is None:
        return Viewer(user_id=getattr(member, "user_id", None))
    head = rights.department_id if rights.people_rows == "department" and rights.can("people") else None
    return Viewer(
        user_id=getattr(member, "user_id", None),
        admin=rights.is_admin,
        department_id=rights.department_id,
        head_department=head,
        levels={source.resource: rights.level(source.resource) for source in SOURCES.values()},
    )


def can_see(viewer: Viewer, view: View) -> bool:
    if view.archived_at is not None or not viewer.sees(SOURCES[view.source].resource):
        return False
    return view.owner_user_id is None or view.owner_user_id == viewer.user_id


def pinned_for(viewer: Viewer, view: View) -> bool:
    if view.pin == "all":
        return True
    if view.pin != "owner":
        return False
    if view.owner_user_id is not None:
        return view.owner_user_id == viewer.user_id
    if view.owner_department_id is not None:
        return view.owner_department_id == viewer.department_id
    return True


def can_edit(viewer: Viewer, view: View) -> bool:
    if view.owner_user_id is not None:
        return view.owner_user_id == viewer.user_id
    if viewer.admin:
        return True
    return view.owner_department_id is not None and view.owner_department_id == viewer.head_department


def _check_owner(viewer: Viewer, department_id: uuid.UUID | None, user_id: uuid.UUID | None) -> None:
    if user_id is not None:
        if user_id != viewer.user_id:
            raise PermissionError("Личный вид сохраняют только себе")
        return
    if viewer.admin:
        return
    if department_id is not None and department_id == viewer.head_department:
        return
    raise PermissionError(
        "Вид компании сохраняет администратор, вид отдела - начальник отдела; себе - любой"
    )


# ── Засев ────────────────────────────────────────────────────────────────────


def ensure(session: Session, workspace: Workspace) -> None:
    """Виды новой компании - «Реестр» и «Долги»; реестр есть всегда.

    Засевается один раз: компания, которая убрала «Долги», не получит их
    снова. Реестр убрать нельзя - его вид досевается всегда.
    """
    keys = set(session.scalars(sa.select(View.key).where(View.workspace_id == workspace.id)))
    if keys and REGISTRY_KEY in keys:
        return
    try:
        with session.begin_nested():
            templates = templates_of(workspace)
            wanted = packs.industry_of(workspace).default_views if not keys else ("registry",)
            for index, key in enumerate(wanted):
                template = templates[key]
                session.add(
                    View(
                        workspace_id=workspace.id,
                        key=REGISTRY_KEY if key == "registry" else _free_key(template.title, keys),
                        title=template.title,
                        source=template.source,
                        filter={},
                        measures=list(template.measures),
                        groups=list(template.groups),
                        forms=list(template.forms),
                        params=dict(template.params),
                        pin="all",
                        position=(index + 1) * POSITION_STEP,
                        template=key,
                    )
                )
            session.flush()
    except IntegrityError:
        # Соседний запрос успел засеять - берём его результат.
        session.expire_all()


_SLUG = re.compile(r"[^0-9a-z]+")
_TRANSLIT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
        "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
        "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya", "ә": "a", "ғ": "g", "қ": "k", "ң": "n", "ө": "o", "ұ": "u", "ү": "u",
        "һ": "h", "і": "i",
    }
)


def _free_key(title: str, taken: set[str]) -> str:
    """Ключ вида из названия: «Долги ЮО» → `dolgi_yuo`; занят - с номером."""
    base = _SLUG.sub("_", (title or "").lower().translate(_TRANSLIT)).strip("_")[:40] or "vid"
    key, index = base, 2
    while key in taken or key == REGISTRY_KEY:
        key = f"{base}_{index}"
        index += 1
    taken.add(key)
    return key


# ── Чтение ───────────────────────────────────────────────────────────────────


def all_of(session: Session, workspace_id: uuid.UUID, *, archived: bool = False) -> list[View]:
    query = sa.select(View).where(View.workspace_id == workspace_id)
    if not archived:
        query = query.where(View.archived_at.is_(None))
    return list(session.scalars(query.order_by(View.position, View.created_at)))


def books(session: Session, workspace_id: uuid.UUID) -> dict[str, View]:
    """Виды договоров - книги листов: ключ вида → вид (и убранные: их листы ещё в базе)."""
    return {
        view.key: view
        for view in session.scalars(
            sa.select(View).where(View.workspace_id == workspace_id, View.source == "contracts")
        )
    }


def share_department(view: View | None) -> uuid.UUID | None:
    """Отдел, долей которого вид показывает сумму договора: владелец, если у вида есть мера доли."""
    if view is None or view.owner_department_id is None:
        return None
    return view.owner_department_id if "share" in (view.measures or []) else None


def owner_out(view: View, departments: Mapping[uuid.UUID, Department]) -> dict[str, Any]:
    if view.owner_user_id is not None:
        return {"kind": "user", "id": str(view.owner_user_id)}
    if view.owner_department_id is not None:
        department = departments.get(view.owner_department_id)
        return {
            "kind": "department",
            "id": str(view.owner_department_id),
            "code": department.code if department is not None else "",
            "title": department.title if department is not None else "",
        }
    return {"kind": "company"}


def out(view: View, viewer: Viewer | None, departments: Mapping[uuid.UUID, Department]) -> dict[str, Any]:
    return {
        "id": str(view.id),
        "key": view.key,
        "title": view.title,
        "source": view.source,
        "owner": owner_out(view, departments),
        "filter": view.filter or {"any": []},
        "measures": list(view.measures or []),
        "groups": list(view.groups or []),
        "forms": list(view.forms or []),
        "params": dict(view.params or {}),
        "pin": view.pin,
        "position": view.position,
        "template": view.template,
        **(
            {"pinned": pinned_for(viewer, view), "editable": can_edit(viewer, view)}
            if viewer is not None
            else {}
        ),
    }


def _departments(session: Session, workspace_id: uuid.UUID) -> dict[uuid.UUID, Department]:
    return {
        item.id: item
        for item in session.scalars(sa.select(Department).where(Department.workspace_id == workspace_id))
    }


def listing(session: Session, workspace: Workspace, viewer: Viewer) -> dict[str, Any]:
    """Виды, которые человек видит, с отметкой «в колонке»; шаблоны и каталоги - для настройки."""
    ensure(session, workspace)
    departments = _departments(session, workspace.id)
    shown = [view for view in all_of(session, workspace.id) if can_see(viewer, view)]
    return {
        "views": [out(view, viewer, departments) for view in shown],
        "templates": [
            {
                "key": item.key,
                "title": item.title,
                "source": item.source,
                "note": item.note,
                "department": item.department,
            }
            for item in templates_of(workspace).values()
            if viewer.sees(SOURCES[item.source].resource)
        ],
        "measures": [{"key": item.key, "title": item.title, "type": item.type, "note": item.note} for item in MEASURES],
        "sources": {
            key: {
                "title": source.title,
                "forms": [{"key": form, "title": FORM_TITLES[form]} for form in source.forms],
                "groups": [{"key": group, "title": GROUP_TITLES[group]} for group in source.groups],
            }
            for key, source in SOURCES.items()
        },
    }


def book_out(view: View, departments: Mapping[uuid.UUID, Department]) -> dict[str, Any]:
    """Вид договоров для схемы реестра - чем книга отличается от реестра."""
    return {
        "key": view.key,
        "title": view.title,
        "owner": owner_out(view, departments),
        "filter": view.filter or {"any": []},
        "measures": list(view.measures or []),
        "groups": list(view.groups or []),
        "forms": list(view.forms or []),
        "share_department": str(dep) if (dep := share_department(view)) else None,
    }


def schema_books(session: Session, workspace: Workspace, user_id: uuid.UUID | None) -> list[dict[str, Any]]:
    """Книги для схемы реестра: виды договоров компании, отделов и свои личные."""
    ensure(session, workspace)
    departments = _departments(session, workspace.id)
    return [
        book_out(view, departments)
        for view in all_of(session, workspace.id)
        if view.source == "contracts" and (view.owner_user_id is None or view.owner_user_id == user_id)
    ]


def find(session: Session, workspace: Workspace, key_or_id: str) -> View:
    ensure(session, workspace)
    query = sa.select(View).where(View.workspace_id == workspace.id, View.archived_at.is_(None))
    try:
        view = session.scalar(query.where(View.id == uuid.UUID(str(key_or_id))))
    except ValueError:
        view = None
    if view is None:
        view = session.scalar(query.where(View.key == ("" if key_or_id in ("", "registry") else key_or_id)))
    if view is None:
        raise FinanceError("Такого вида нет")
    return view


# ── Проверка записи ──────────────────────────────────────────────────────────


def _clean_list(raw: Any, allowed: Iterable[str], what: str) -> list[str]:
    allowed = tuple(allowed)
    if raw in (None, ""):
        return []
    if not isinstance(raw, (list, tuple)):
        raise FinanceError(f"{what}: ждём список")
    out: list[str] = []
    for item in raw:
        key = str(item)
        if key not in allowed:
            raise FinanceError(f"{what}: «{key}» нет")
        if key not in out:
            out.append(key)
    return out


def _clean_params(source: str, raw: Any, departments: Mapping[uuid.UUID, Department]) -> dict[str, Any]:
    params = dict(raw or {}) if isinstance(raw, dict) else {}
    if source == "debts":
        side = params.get("side") or "receivable"
        basis = params.get("basis") or "accruals"
        group = params.get("group") or "client"
        if side not in DEBT_SIDES or basis not in DEBT_BASES or group not in DEBT_GROUPS:
            raise FinanceError("Долги: сторона, основание или группа записаны неверно")
        return {"side": side, "basis": basis, "group": group}
    if source == "unbilled":
        out: dict[str, Any] = {}
        own = str(params.get("own") or "").strip()
        department = str(params.get("department") or "").strip()
        if own:
            out["own"] = own
        if department:
            out["department"] = department
        return out
    return {}


@dataclass
class Draft:
    title: str
    source: str
    owner_department_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None
    filter: dict[str, Any] = field(default_factory=dict)
    measures: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    forms: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    pin: str = "owner"
    template: str = ""
    sheets: list[dict[str, Any]] = field(default_factory=list)


def _uuid_or_none(raw: Any, what: str) -> uuid.UUID | None:
    if raw in (None, ""):
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise FinanceError(f"{what} записан неверно") from exc


def _owner(raw: Any, viewer: Viewer, departments: Mapping[uuid.UUID, Department]) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Владелец из запроса: `company`, `{"department": id}`, `me` - (отдел, учётка)."""
    if raw in (None, "", "company"):
        return None, None
    if raw == "me":
        return None, viewer.user_id
    if isinstance(raw, dict) and raw.get("department"):
        department_id = _uuid_or_none(raw.get("department"), "Отдел")
        if department_id not in departments or departments[department_id].archived_at is not None:
            raise FinanceError("Такого отдела нет")
        return department_id, None
    raise FinanceError("Владелец вида - компания, отдел или вы сами")


# ── Шаблон «Разовые договоры по возрасту долга» ──────────────────────────────


def _values_by_meaning(session: Session, workspace_id: uuid.UUID, field_key: str) -> list[ListValue]:
    return list(
        session.scalars(
            sa.select(ListValue)
            .where(
                ListValue.workspace_id == workspace_id,
                ListValue.field_key == field_key,
                ListValue.archived_at.is_(None),
            )
            .order_by(ListValue.position, ListValue.created_at)
        )
    )


#: Колонки листов шаблона: поля договора подписаны так, как их назвала
#: компания; меры - словами меры.
_AGE_COLUMNS = (
    "row_number", "status", "planned_end_at", "people", "executor", "customer", "number", "signed_at",
    "department", "type", "subject", "amount", "summary_paid", "summary_remaining", "end_date", "note",
)


def _oneoff_age_sheets(session: Session, workspace: Workspace, department_id: uuid.UUID | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Отбор и листы шаблона: весь список, «до 2 мес» … «6+ мес» по сроку, «Остатки».

    Отбор - начисление «вся сумма» со смыслом «выручка», статус «в работе» или
    «исполнен», отдел-владелец. Статусы и вид новой строки - по смыслу, а не
    по написанию: у компании они названы по-своему.
    """
    from app.finance.contracts.fields import FIELD_BY_KEY, fields_of

    titles = {item.key: item.title for item in fields_of(session, workspace.id)}
    statuses = _values_by_meaning(session, workspace.id, "status")
    by_phase: dict[str, str] = {}
    for row in statuses:
        phase = (row.meaning or {}).get("phase")
        if phase in ("in_progress", "fulfilled") and phase not in by_phase:
            by_phase[phase] = str(row.id)
    types = _values_by_meaning(session, workspace.id, "type")
    kind = next(
        (
            row
            for row in types
            if (row.meaning or {}).get("billing") == "total" and (row.meaning or {}).get("economic_role") == "revenue"
        ),
        None,
    )
    base: list[dict[str, Any]] = [
        {"field": "billing", "op": "in", "value": ["total"]},
        {"field": "economic", "op": "in", "value": ["revenue"]},
        {"field": "phase", "op": "in", "value": ["in_progress", "fulfilled"]},
    ]
    if department_id is not None:
        base.append({"field": "department", "op": "in", "value": [str(department_id)]})
    defaults: dict[str, Any] = {"own_side": "executor"}
    if kind is not None:
        defaults["type"] = str(kind.id)
    if "in_progress" in by_phase:
        defaults["status"] = by_phase["in_progress"]
    if department_id is not None:
        defaults["department"] = str(department_id)

    def title_of(key: str) -> str:
        if key == "row_number":
            return "№"
        if key in MEASURE_BY_KEY:
            return MEASURE_BY_KEY[key].title
        return titles.get(key) or (FIELD_BY_KEY[key].title if key in FIELD_BY_KEY else key)

    def block(title: str, *conditions: dict[str, Any], with_age: bool = True) -> dict[str, Any]:
        keys = [key for key in _AGE_COLUMNS if key == "row_number" or key in titles or key in MEASURE_BY_KEY]
        if with_age:
            keys.append("age_months")
        out: dict[str, Any] = {
            "title": title,
            "filter": {"any": [{"all": list(conditions)}]} if conditions else {"any": []},
            "roles": {},
            "columns": [{"key": key, "label": title_of(key), "width": None} for key in keys],
            "defaults": dict(defaults),
        }
        if len(by_phase) == 2:
            out["choices"] = {"status": [by_phase["in_progress"], by_phase["fulfilled"]]}
            out["paint"] = [
                {
                    "filter": {"any": [{"all": [{"field": "status", "op": "in", "value": [by_phase["fulfilled"]]}]}]},
                    "tone": "done",
                }
            ]
        return out

    open_ = {"field": "phase", "op": "in", "value": ["in_progress"]}
    done = {"field": "phase", "op": "in", "value": ["fulfilled"]}
    left = {"field": "summary_remaining", "op": "gt", "value": 0}

    def age(op: str, months: int) -> dict[str, Any]:
        return {"field": "age_months", "op": op, "value": months}

    sheets = [
        {"key": "all", "title": "Все", "blocks": [block("", with_age=False)]},
        {"key": "2m", "title": "до 2 мес", "blocks": [block("", open_, age("lt", 2))]},
        {"key": "3m", "title": "2-3 мес", "blocks": [block("", open_, age("gte", 2), age("lt", 3))]},
        {"key": "6m", "title": "3-6 мес", "blocks": [block("", open_, age("gte", 3), age("lt", 6))]},
        {"key": "6p", "title": "6+ мес", "blocks": [block("", open_, age("gte", 6))]},
        {
            "key": "rest",
            "title": "Остатки",
            "blocks": [block("Работа идёт - есть остаток", open_, left), block("Работа завершена - есть остаток", done, left)],
        },
    ]
    return {"any": [{"all": base}]}, sheets


def _main_sheet_blocks(session: Session, workspace: Workspace) -> list[dict[str, Any]]:
    """Колонки нового вида договоров без шаблона - как у главного листа реестра."""
    main = session.scalar(
        sa.select(EntityView).where(
            EntityView.workspace_id == workspace.id,
            EntityView.main.is_(True),
            EntityView.book == REGISTRY_KEY,
            EntityView.archived_at.is_(None),
        )
    )
    first = (main.blocks or [{}])[0] if main is not None else {}
    return [
        {
            "title": "",
            "filter": {"any": []},
            "roles": dict(first.get("roles") or {}),
            "columns": list(first.get("columns") or []),
            "defaults": {},
        }
    ]


# ── Запись ───────────────────────────────────────────────────────────────────


def create(
    session: Session,
    workspace: Workspace,
    viewer: Viewer,
    actor_email: str,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    """Сохранить вид: из шаблона (`template`) или свой. Листы вида договоров заводятся вместе с ним."""
    ensure(session, workspace)
    departments = _departments(session, workspace.id)
    owner_department, owner_user = _owner(data.get("owner"), viewer, departments)
    _check_owner(viewer, owner_department, owner_user)
    template_key = str(data.get("template") or "")
    if template_key:
        template = templates_of(workspace).get(template_key)
        if template is None:
            raise FinanceError("Такого шаблона нет")
        if template_key == "registry":
            raise FinanceError("Реестр у компании один")
        if template.department and owner_department is None:
            raise FinanceError(f"«{template.title}» - вид отдела: выберите отдел")
        draft = Draft(
            title=template.title,
            source=template.source,
            measures=list(template.measures),
            groups=list(template.groups),
            forms=list(template.forms),
            params=dict(template.params),
            template=template_key,
        )
        if template_key == "oneoff_age":
            code = departments[owner_department].code if owner_department in departments else ""
            draft.title = f"Разовые {code}".strip() if code else "Разовые договоры"
            draft.filter, draft.sheets = _oneoff_age_sheets(session, workspace, owner_department)
        elif owner_department is not None and template.source == "debts":
            code = departments[owner_department].code
            draft.title = f"{template.title} {code}"
            draft.filter = {"any": [{"all": [{"field": "department", "op": "in", "value": [str(owner_department)]}]}]}
        elif owner_department is not None and template.source == "unbilled":
            code = departments[owner_department].code
            draft.title = f"{template.title} {code}"
            draft.params = {"department": code}
    else:
        source = str(data.get("source") or "contracts")
        if source not in VIEW_SOURCES:
            raise FinanceError("Источник вида - договоры, долги или «Не выставлены»")
        draft = Draft(title="", source=source, forms=list(SOURCES[source].forms[:1]))
        if source == "debts":
            draft.params = _clean_params(source, {}, departments)
        if source == "contracts":
            draft.sheets = [{"key": "all", "title": "Все", "blocks": _main_sheet_blocks(session, workspace)}]
    draft.owner_department_id, draft.owner_user_id = owner_department, owner_user
    if data.get("title"):
        draft.title = str(data["title"]).strip()
    if not draft.title:
        raise FinanceError("Назовите вид")
    _apply_fields(draft, data, departments)
    # Вид компании - в колонке у всех; отдела и свой - у владельца.
    draft.pin = str(data["pin"]) if data.get("pin") in VIEW_PINS else ("all" if owner_department is None and owner_user is None else "owner")

    taken = set(session.scalars(sa.select(View.key).where(View.workspace_id == workspace.id)))
    taken |= set(session.scalars(sa.select(EntityView.book).where(EntityView.workspace_id == workspace.id).distinct()))
    position = (session.scalar(sa.select(sa.func.max(View.position)).where(View.workspace_id == workspace.id)) or 0) + POSITION_STEP
    view = View(
        workspace_id=workspace.id,
        key=_free_key(draft.title, taken),
        title=draft.title,
        source=draft.source,
        owner_department_id=draft.owner_department_id,
        owner_user_id=draft.owner_user_id,
        filter=draft.filter,
        measures=draft.measures,
        groups=draft.groups,
        forms=draft.forms,
        params=draft.params,
        pin=draft.pin,
        position=position,
        template=draft.template,
        created_by=viewer.user_id,
    )
    session.add(view)
    session.flush()
    if draft.source == "contracts":
        _add_sheets(session, workspace, view.key, draft.sheets)
    from app.finance.contracts.fields import bump

    bump(session, workspace.id, "schema")
    history.write(
        session,
        workspace,
        kind="core.view",
        entity="view",
        entity_id=view.id,
        title=f"Вид «{view.title}» сохранён" + (f" из шаблона «{templates_of(workspace)[draft.template].title}»" if draft.template else ""),
        after=out(view, None, departments),
        actor=actor_email,
    )
    return out(view, viewer, departments)


def _add_sheets(session: Session, workspace: Workspace, book: str, sheets: list[dict[str, Any]]) -> None:
    from app.finance.contracts.fields import ENTITY

    taken = set(session.scalars(sa.select(EntityView.key).where(EntityView.workspace_id == workspace.id)))
    for index, sheet in enumerate(sheets):
        key = f"{book}_{sheet['key']}"[:60]
        base, number = key, 2
        while key in taken:
            key = f"{base}_{number}"
            number += 1
        taken.add(key)
        session.add(
            EntityView(
                workspace_id=workspace.id,
                entity=ENTITY,
                key=key,
                title=sheet["title"],
                main=False,
                book=book,
                blocks=sheet["blocks"],
                position=(index + 1) * POSITION_STEP,
            )
        )
    session.flush()


def _apply_fields(draft: Any, data: Mapping[str, Any], departments: Mapping[uuid.UUID, Department]) -> None:
    """Отбор, меры, группировки, формы и параметры из запроса - с проверкой по источнику."""
    source = SOURCES[draft.source]
    if "filter" in data:
        if draft.source == "unbilled":
            raise FinanceError("У «Не выставлены» отбор - юрлицо и отдел в параметрах")
        try:
            draft.filter = rules.validate(data.get("filter"))
        except rules.FilterError as exc:
            raise FinanceError(str(exc)) from exc
    if "measures" in data:
        if draft.source != "contracts":
            raise FinanceError("Меры есть только у вида договоров")
        draft.measures = _clean_list(data.get("measures"), MEASURE_BY_KEY, "Меры")
    if "groups" in data:
        draft.groups = _clean_list(data.get("groups"), source.groups, "Группировки")
    if "forms" in data:
        forms = _clean_list(data.get("forms"), source.forms, "Формы")
        if not forms:
            raise FinanceError("У вида должна быть хотя бы одна форма")
        draft.forms = forms
    if "params" in data:
        draft.params = _clean_params(draft.source, data.get("params"), departments)


def update(
    session: Session,
    workspace: Workspace,
    viewer: Viewer,
    actor_email: str,
    view_id: uuid.UUID,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    view = session.get(View, view_id)
    if view is None or view.workspace_id != workspace.id or view.archived_at is not None:
        raise FinanceError("Такого вида нет")
    if not can_edit(viewer, view):
        raise PermissionError("Этот вид правит его владелец или администратор")
    departments = _departments(session, workspace.id)
    before = out(view, None, departments)
    if "title" in data:
        title = str(data.get("title") or "").strip()
        if not title:
            raise FinanceError("Назовите вид")
        view.title = title
    if view.key == REGISTRY_KEY and set(data) - {"title", "pin", "position", "forms"}:
        raise FinanceError("У реестра меняются название, место в колонке и формы")
    if "owner" in data:
        department_id, user_id = _owner(data.get("owner"), viewer, departments)
        _check_owner(viewer, department_id, user_id)
        view.owner_department_id, view.owner_user_id = department_id, user_id
    _apply_fields(view, data, departments)
    if "pin" in data:
        if data.get("pin") not in VIEW_PINS:
            raise FinanceError("Закрепление - всем, владельцу или никому")
        view.pin = str(data["pin"])
    if "position" in data:
        try:
            view.position = int(data["position"])
        except (TypeError, ValueError) as exc:
            raise FinanceError("Место в колонке - число") from exc
    session.flush()
    from app.finance.contracts.fields import bump

    bump(session, workspace.id, "schema")
    after = out(view, None, departments)
    changed = [key for key in after if after[key] != before.get(key)]
    if changed:
        history.write(
            session,
            workspace,
            kind="core.view",
            entity="view",
            entity_id=view.id,
            title=f"Вид «{view.title}»: {', '.join(_WORDS.get(key, key) for key in changed)}",
            before={key: before.get(key) for key in changed},
            after={key: after[key] for key in changed},
            actor=actor_email,
        )
    return out(view, viewer, departments)


_WORDS = {
    "title": "название",
    "owner": "владелец",
    "filter": "отбор",
    "measures": "меры",
    "groups": "группировки",
    "forms": "формы",
    "params": "параметры",
    "pin": "в колонке",
    "position": "место в колонке",
}


def remove(session: Session, workspace: Workspace, viewer: Viewer, actor_email: str, view_id: uuid.UUID) -> None:
    """Убрать вид - и его листы: договоры остаются в реестре, как были."""
    view = session.get(View, view_id)
    if view is None or view.workspace_id != workspace.id or view.archived_at is not None:
        raise FinanceError("Такого вида нет")
    if view.key == REGISTRY_KEY:
        raise FinanceError("Реестр убрать нельзя")
    if not can_edit(viewer, view):
        raise PermissionError("Этот вид убирает его владелец или администратор")
    now = datetime.now(timezone.utc)
    view.archived_at = now
    if view.source == "contracts":
        for sheet in session.scalars(
            sa.select(EntityView).where(
                EntityView.workspace_id == workspace.id, EntityView.book == view.key, EntityView.archived_at.is_(None)
            )
        ):
            sheet.archived_at = now
    session.flush()
    from app.finance.contracts.fields import bump

    bump(session, workspace.id, "schema")
    history.write(
        session,
        workspace,
        kind="core.view",
        entity="view",
        entity_id=view.id,
        title=f"Вид «{view.title}» убран",
        before=out(view, None, _departments(session, workspace.id)),
        actor=actor_email,
    )


# ── Обратные шаги пачки «применить» ──────────────────────────────────────────

#: Что вид хранит и что «вернуть» ставит обратно (`state` / шаг `view_state`).
STATE_FIELDS = (
    "title", "owner_department_id", "owner_user_id", "filter", "measures", "groups", "forms", "params", "pin", "position",
)


def state(view: View) -> dict[str, Any]:
    out_state: dict[str, Any] = {}
    for name in STATE_FIELDS:
        value = getattr(view, name)
        out_state[name] = str(value) if isinstance(value, uuid.UUID) else value
    return out_state


def sheets_of(session: Session, workspace_id: uuid.UUID, view: View) -> list[str]:
    """Действующие листы вида договоров - что уберёт `remove` вместе с видом."""
    if view.source != "contracts":
        return []
    return [
        str(sheet.id)
        for sheet in session.scalars(
            sa.select(EntityView).where(
                EntityView.workspace_id == workspace_id, EntityView.book == view.key, EntityView.archived_at.is_(None)
            )
        )
    ]


def undo(session: Session, workspace: Workspace, actor: Any, kind: str, payload: Mapping[str, Any], done: list[str], skipped: list[str]) -> None:
    """Вернуть сохранение, правку или удаление вида (шаги `view_created`, `view_state`, `view_removed`)."""
    from app.finance.contracts.fields import bump

    view = session.get(View, uuid.UUID(str(payload.get("id"))))
    if view is None or view.workspace_id != workspace.id:
        skipped.append("вида больше нет")
        return
    now = datetime.now(timezone.utc)
    if kind == "view_created":
        if view.archived_at is not None:
            skipped.append(f"вид «{view.title}» уже убран")
            return
        view.archived_at = now
        for sheet_id in sheets_of(session, workspace.id, view):
            sheet = session.get(EntityView, uuid.UUID(sheet_id))
            if sheet is not None:
                sheet.archived_at = now
        done.append(f"вид «{view.title}» убран")
    elif kind == "view_state":
        before = dict(payload.get("before") or {})
        for name in STATE_FIELDS:
            if name not in before:
                continue
            value = before[name]
            if name in ("owner_department_id", "owner_user_id"):
                value = uuid.UUID(value) if value else None
            setattr(view, name, value)
        done.append(f"вид «{view.title}» - как был")
    elif kind == "view_removed":
        if view.archived_at is None:
            skipped.append(f"вид «{view.title}» и так на месте")
            return
        view.archived_at = None
        for sheet_id in payload.get("sheets") or []:
            sheet = session.get(EntityView, uuid.UUID(str(sheet_id)))
            if sheet is not None and sheet.workspace_id == workspace.id:
                sheet.archived_at = None
        done.append(f"вид «{view.title}» вернулся")
    session.flush()
    bump(session, workspace.id, "schema")
    history.write(
        session, workspace, kind="core.view", entity="view", entity_id=view.id,
        title=f"Вид «{view.title}»: {done[-1] if done else 'возвращён'}", after=state(view),
        actor=getattr(actor, "email", "") or "",
    )


# ── Спросить ─────────────────────────────────────────────────────────────────


MONTH_LABELS = ("ЯНВАРЬ", "ФЕВРАЛЬ", "МАРТ", "АПРЕЛЬ", "МАЙ", "ИЮНЬ", "ИЮЛЬ", "АВГУСТ", "СЕНТЯБРЬ", "ОКТЯБРЬ", "НОЯБРЬ", "ДЕКАБРЬ")


def month_label(day: date | None) -> str:
    """Месяц словом, как строка сводной таблицы: «АВГУСТ 2026»."""
    return f"{MONTH_LABELS[day.month - 1]} {day.year}" if day is not None else ""


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(Decimal("0.01")).normalize(), "f")


def _round(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


AGE_BUCKETS = (("до 2 мес", 0, 2), ("2-3 мес", 2, 3), ("3-6 мес", 3, 6), ("6+ мес", 6, None))


def ask(
    session: Session,
    workspace: Workspace,
    access: Any,
    view: View,
    *,
    day: date | None = None,
    members: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Вид договоров на день: договоры по листам и частям, меры, итоги частей, группировки.

    `members` - сюда «По сотрудникам» складывает слагаемые каждой строки
    (договор, его сумма и остаток у этого человека): их раскрывает «объяснить».

    Числа - те же, что на экране вида: части - «договоров: N · остаток» (у
    частей с колонкой «Остаток»), «По сотрудникам» - как `staff.ts`, сумма -
    долей отдела-владельца. Оплачено на другой день - только у оплат KORT:
    сводная таблица знает лишь сегодняшнее, и её число помечено `now_only`.
    """
    from app.finance.contracts import shares as shares_module
    from app.finance.contracts.service import Registry, age_months, facts_of, people_of, today, visible_to
    from app.finance.contracts.models import Contract

    if view.source != "contracts":
        raise FinanceError("Спросить по листам можно вид договоров")
    from app.finance.contracts import summary as summary_module

    on = day or today()
    registry = Registry(session, workspace)
    if on != today():
        # Оплачено KORT - на тот день; сводная таблица знает только сегодняшнее.
        registry.paid_on = on
    # Сводная подключена, но процесс её ещё не читал (сразу после выкладки):
    # прочитать, как «Оплачено» экрана, а не отвечать нулями остатков.
    unread = False
    if registry.summary is None and registry.summary_connected:
        slot = summary_module.get(session, workspace.id)
        if slot is not None and slot.index is not None:
            registry._summary = slot.index
        else:
            unread = True
    contracts = list(
        session.scalars(sa.select(Contract).where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None)))
    )
    people = people_of(session, [item.id for item in contracts])
    registry.parties_for({pid for item in contracts for pid in (item.executor_id, item.customer_id) if pid})
    # Вопрос без сохранения (`model/ask.py`) листов не имеет: договор в нём -
    # если подходит под отбор, одной частью.
    transient = view.id is None
    sheets = [] if transient else [sheet for sheet in registry.views if (sheet.book or "") == view.key]
    base = view.filter or {}
    shown = {
        "client": "customer" not in access.hidden and "executor" not in access.hidden,
        "status": "status" not in access.hidden,
    }
    department = share_department(view)
    money_open = "amount" not in access.hidden
    paid_open = "summary_paid" not in access.hidden and money_open
    people_shares = shares_module.all_visible(session, workspace, access).get("contracts", {}) if "people" in (view.groups or []) else {}

    rows: list[dict[str, Any]] = []
    tallies: dict[tuple[str, int], dict[str, Any]] = {}
    in_view: list[tuple[Contract, dict[str, Any], dict[str, Any]]] = []
    for item in contracts:
        mine = people.get(item.id, [])
        if not visible_to(item, registry, access, mine):
            continue
        facts = facts_of(item, registry, mine)
        facts["age_months"] = age_months(item.signed_at, on)
        if transient:
            # Пустой отбор вопроса - все договоры (у листа пустое правило не отбирает ничего).
            places = [{"view": view.key, "block": 0}] if rules.is_empty(base) or rules.matches(base, facts) else []
        else:
            places = rules.membership(facts, sheets, {view.key: base})
        if not places:
            continue
        share = shares_module.book_share(item, department) if department is not None else None
        amount = item.amount
        if share is not None and share.kind == "share":
            amount = share.amount
        match = registry.summary_of(item)
        found = match is not None and match.state == "found"
        paid = match.paid if found else None
        remaining = (item.amount - match.paid) if found and item.amount is not None else None
        fraction = share.fraction if share is not None and share.kind == "share" else Decimal(1)

        def part(value: Decimal | None) -> Decimal | None:
            if value is None or fraction is None:
                return None
            return _round(value * fraction)

        month = match.months[0] if found and match.months else ""
        measures = {
            "amount": _money(amount) if money_open else None,
            "summary_paid": _money(part(paid)) if paid_open else None,
            "summary_remaining": _money(part(remaining)) if paid_open else None,
            "age_months": facts["age_months"],
            "month": month,
        }
        if match is not None and match.source == "kort":
            measures["paid_source"] = "kort"
        elif found:
            measures["paid_source"] = "table"
            if on != today():
                measures["now_only"] = True
        row = {"id": str(item.id), "number": item.number or "", "places": places, "measures": measures}
        if shown["client"]:
            ours, theirs = item.executor_id, item.customer_id
            if not registry.is_own(ours) and registry.is_own(theirs):
                ours, theirs = theirs, ours
            party = registry.parties_for([theirs]).get(theirs) if theirs else None
            entity = registry.own.get(ours) if ours else None
            row["client"] = party.name if party is not None else ""
            row["entity"] = entity.code if entity is not None else ""
        if shown["status"]:
            row["status"] = str(facts.get("status__text") or "")
            # Фаза статуса - смысл, а не подпись: «По возрасту» считает только договоры в работе.
            row["phase"] = facts.get("phase") or ""
        if fraction is not None and fraction != 1:
            row["fraction"] = format(fraction, "f")
        if share is not None and share.kind != "whole":
            row["share"] = share.kind
        rows.append(row)
        in_view.append(
            (
                item,
                facts,
                {
                    "share": share,
                    "found": found,
                    "remaining": part(remaining),
                    "whole_remaining": remaining,
                    "amount": amount,
                    "month": month,
                },
            )
        )
        for place in places:
            key = (place["view"], place["block"])
            tally = tallies.setdefault(key, {"contracts": 0, "remaining": Decimal("0"), "known": False})
            tally["contracts"] += 1
            if found and part(remaining) is not None and paid_open:
                tally["remaining"] += part(remaining)
                tally["known"] = True

    sheets_out: list[dict[str, Any]] = []
    for sheet in sheets:
        blocks_out = []
        for index, block in enumerate(sheet.blocks or []):
            tally = tallies.get((sheet.key, index), {"contracts": 0, "remaining": Decimal("0"), "known": False})
            with_left = any((column or {}).get("key") == "summary_remaining" for column in (block or {}).get("columns") or [])
            blocks_out.append(
                {
                    "title": (block or {}).get("title") or "",
                    "contracts": tally["contracts"],
                    **({"remaining": _money(_round(tally["remaining"]))} if with_left and tally["known"] else {}),
                }
            )
        sheets_out.append(
            {
                "key": sheet.key,
                "title": sheet.title,
                "contracts": sum(block["contracts"] for block in blocks_out),
                "blocks": blocks_out,
            }
        )
    result: dict[str, Any] = {
        "view": {"key": view.key, "title": view.title},
        "as_of": on.isoformat(),
        "sheets": sheets_out,
        "rows": rows,
        "groups": {},
        # Сводная не прочиталась: «Оплачено» и «Остаток» её юрлиц неизвестны, а не ноль.
        **({"summary": "unread"} if unread else {}),
    }
    if "people" in (view.groups or []) and (sheets or transient):
        # «По сотрудникам» - договоры первого листа, как на экране; у вопроса без сохранения - все его договоры.
        first = in_view if transient else [
            entry for entry in in_view
            if any(place["view"] == sheets[0].key for place in rules.membership(entry[1], [sheets[0]], {view.key: base}))
        ]
        result["groups"]["people"] = _by_people(first, registry, people, people_shares, money_open, paid_open, members=members)
    if "age" in (view.groups or []):
        result["groups"]["age"] = _by_age(in_view, money_open, paid_open)
    return result


def _num(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - не число - ноль, как `numberOf` у клиента
        return Decimal("0")


def _by_people(
    entries: list[tuple[Any, dict[str, Any], dict[str, Any]]],
    registry: Any,
    people: Mapping[uuid.UUID, list[uuid.UUID]],
    people_shares: Mapping[str, Any],
    money_open: bool,
    paid_open: bool,
    *,
    members: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """«По сотрудникам» - та же арифметика, что у `staff.ts`.

    Договор с двумя ответственными - в строке каждого, в итоге один раз;
    своя доля человека - его часть суммы, остаток - той же частью договора.
    Месяц - первый месяц договора в источнике оплат («нет» - пусто).
    """
    names = {
        employee_id: employee.full_name
        for employee_id, employee in registry.employees_for({pid for ids in people.values() for pid in ids}).items()
    }
    rows: dict[str, dict[str, Any]] = {}
    totals = {
        "contracts": 0, "amount": Decimal("0"), "remaining": Decimal("0"), "open_contracts": 0,
        "open_amount": Decimal("0"), "by_month": {}, "shared": 0, "open_shared": 0,
    }
    for contract, facts, measured in entries:
        ids = people.get(contract.id, [])
        persons = [(str(pid), names.get(pid, "-")) for pid in ids] or [("", "Без ответственного")]
        split = (people_shares.get(str(contract.id)) or {}).get("people") or {}

        def share_of(person: str) -> Decimal | None:
            raw = (split.get(person) or {}).get("amount")
            if raw in (None, ""):
                return None
            return _num(raw)

        whole = not (len(ids) > 1 and all(share_of(str(pid)) is not None for pid in ids))
        client = registry.parties_for([contract.customer_id]).get(contract.customer_id) if contract.customer_id else None
        client_name = client.name if client is not None else ""
        total = _num(contract.amount)
        amount = _num(measured["amount"])
        found = measured["found"]
        remaining = measured["remaining"] if found and measured["remaining"] is not None else Decimal("0")
        # Остаток всего договора - для доли человека (`staff.ts`: totalRemaining * part / total).
        total_remaining = measured["whole_remaining"] if found and measured["whole_remaining"] is not None else Decimal("0")
        open_ = facts.get("phase") not in ("fulfilled", "terminated", "failed")
        month = measured["month"]
        totals["contracts"] += 1
        totals["amount"] += amount
        totals["remaining"] += remaining
        if len(persons) > 1 and whole:
            totals["shared"] += 1
        if open_:
            totals["open_contracts"] += 1
            totals["open_amount"] += amount
            totals["by_month"][month] = totals["by_month"].get(month, Decimal("0")) + amount
            if len(persons) > 1 and whole:
                totals["open_shared"] += 1
        for person, name in persons:
            own_part = share_of(person) if person else None
            mine = own_part if own_part is not None else amount
            own_left = remaining if own_part is None else (total_remaining * (own_part / total) if total > 0 else Decimal("0"))
            row = rows.setdefault(
                name,
                {
                    "name": name, "clients": set(), "contracts": 0, "amount": Decimal("0"), "remaining": Decimal("0"),
                    "open_contracts": 0, "open_clients": set(), "open_amount": Decimal("0"), "by_month": {},
                },
            )
            if client_name:
                row["clients"].add(client_name)
            row["contracts"] += 1
            row["amount"] += mine
            row["remaining"] += own_left
            if members is not None:
                members.setdefault(name, []).append(
                    {"id": str(contract.id), "number": contract.number or "", "amount": mine, "remaining": own_left, "open": open_}
                )
            if open_:
                if client_name:
                    row["open_clients"].add(client_name)
                row["open_contracts"] += 1
                row["open_amount"] += mine
                row["by_month"][month] = row["by_month"].get(month, Decimal("0")) + mine

    def money_out(value: Decimal) -> str | None:
        return _money(_round(value)) if money_open else None

    def left_out(value: Decimal) -> str | None:
        return _money(_round(value)) if paid_open else None

    ordered = sorted(rows.values(), key=lambda row: (-row["amount"], row["name"]))
    return {
        "rows": [
            {
                "name": row["name"],
                "clients": len(row["clients"]),
                "contracts": row["contracts"],
                "amount": money_out(row["amount"]),
                "remaining": left_out(row["remaining"]),
                "open_clients": len(row["open_clients"]),
                "open_contracts": row["open_contracts"],
                "open_amount": money_out(row["open_amount"]),
                "by_month": {month: money_out(value) for month, value in row["by_month"].items()},
            }
            for row in ordered
        ],
        "totals": {
            "contracts": totals["contracts"],
            "amount": money_out(totals["amount"]),
            "remaining": left_out(totals["remaining"]),
            "open_contracts": totals["open_contracts"],
            "open_amount": money_out(totals["open_amount"]),
            "by_month": {month: money_out(value) for month, value in totals["by_month"].items()},
            "shared": totals["shared"],
            "open_shared": totals["open_shared"],
        },
    }


def _by_age(entries: list[tuple[Any, dict[str, Any], dict[str, Any]]], money_open: bool, paid_open: bool) -> list[dict[str, Any]]:
    """Договоры в работе по сроку от даты договора: «до 2 мес» … «6+ мес»."""
    out = []
    for title, low, high in AGE_BUCKETS:
        count = 0
        amount = Decimal("0")
        left = Decimal("0")
        for _contract, facts, measured in entries:
            if facts.get("phase") in ("fulfilled", "terminated", "failed"):
                continue
            age = facts.get("age_months")
            if age is None or age < low or (high is not None and age >= high):
                continue
            count += 1
            amount += _num(measured["amount"])
            if measured["found"] and measured["remaining"] is not None:
                left += measured["remaining"]
        out.append(
            {
                "title": title,
                "contracts": count,
                "amount": _money(_round(amount)) if money_open else None,
                "remaining": _money(_round(left)) if paid_open else None,
            }
        )
    return out


__all__ = [
    "COLUMN_MEASURES", "CORE_TEMPLATES", "MEASURES", "MEASURE_BY_KEY", "REGISTRY_KEY", "SOURCES", "TEMPLATES", "templates_of",
    "Viewer", "ask", "book_out", "books", "can_edit", "can_see", "create", "ensure", "find", "listing", "month_label",
    "out", "pinned_for", "remove", "schema_books", "share_department", "update", "viewer_of",
]
