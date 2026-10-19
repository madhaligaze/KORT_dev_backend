"""Поля реестра, значения списков со смыслом и засев новой компании.

Засев общий, не под BBC
───────────────────────
Новая компания получает системные поля, статусы с фазами, виды договоров со
способом начисления и подписями сторон, хозяйственные смыслы и главный лист
«Все договоры». Всё, чем компания отличается (колонка «Ссылка на Битрикс 24»,
статус «нужно закрыть по бух», лист «Исполнитель ГК»), приходит из её
собственного Excel при загрузке или заводится в настройке. Под одну компанию
здесь не пишется ни строки.

Засев повторяемый: системное поле, добавленное в следующей версии, появится у
всех компаний при первом обращении к реестру, а не останется только у новых.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.finance.layout import norm, squash
from app.finance.contracts.models import (
    ECONOMIC_ROLES,
    Counter,
    EntityField,
    EntityView,
    ListValue,
)
from app.finance.models import POSITION_STEP, Workspace

ENTITY = "contract"


@dataclass(frozen=True)
class FieldDef:
    """Системное поле: ключ, тип, подпись по умолчанию и написания шапки."""

    key: str
    type: str
    title: str
    names: tuple[str, ...] = ()
    #: Поле только для чтения везде: «как было в файле».
    readonly: bool = False
    #: Спрятано по умолчанию (есть, но в листе и карточке не показывается).
    hidden: bool = False
    #: Способ заполнения по умолчанию (`FILLS`); пусто - у типа его нет.
    fill: str = ""


#: Системные поля в порядке «Сводной». Написания - это то, как колонку
#: подписывают в реестрах; разбор сравнивает по шапке блока, а не по номеру.
#:
#: Способ заполнения по умолчанию - из «Настроек реестра» BBC (26.09.2026):
#: статус, ответственный, исполнитель и отдел - выпадающим списком, вид и
#: предмет - свой текст с подсказкой справочника. Ячейка листа заводила молча
#: значение из любого напечатанного: на проде 26.09 в статусах висело «им», в
#: видах - «Взыскание», оба ни в одном договоре - следы правки ячейки.
SYSTEM_FIELDS: tuple[FieldDef, ...] = (
    FieldDef("status", "list", "Текущее состояние",
             ("текущее состояние", "статус", "состояние", "статус договора"), fill="list"),
    FieldDef("folder_url", "url", "Папка договора",
             ("ссылка на битрикс 24", "ссылка на битрикс", "папка договора", "битрикс", "ссылка")),
    FieldDef("planned_end_at", "date", "Планируемый срок завершения",
             ("планируемый срок завершения", "план дата завершения", "срок завершения")),
    FieldDef("people", "person", "Ответственное лицо",
             ("ответственное лицо (ф.и.о)", "ответственное лицо", "ответственный",
              "наш сотрудник", "ответ лицо"), fill="list"),
    FieldDef("executor", "party", "Исполнитель",
             ("исполнитель bbc", "исполнитель", "продавец", "арендодатель bbc", "арендодатель",
              "арендадатель bbc", "арендадатель", "займодавец", "наша фирма"), fill="own"),
    FieldDef("customer", "party", "Заказчик",
             ("заказчик (клиент)", "заказчик", "покупатель", "арендатор (клиент)", "арендатор",
              "займополучатель", "заказчик (название фирмы)", "клиент"), fill="hint"),
    FieldDef("number", "text", "№ Договора",
             ("№ договора", "номер договора", "договор №")),
    FieldDef("signed_at", "date", "Дата заключения Договора",
             ("дата заключения договора", "дата договора", "дата заключения")),
    FieldDef("department", "department", "Отдел", ("отдел",), fill="list"),
    FieldDef("type", "list", "Вид услуги", ("вид услуги", "вид договора", "вид"), fill="hint"),
    FieldDef("subject", "list", "Предмет Договора",
             ("предмет договора", "предмет", "предмет исполнения"), fill="hint"),
    FieldDef("amount", "money", "Сумма Договора", ("сумма договора", "сумма")),
    FieldDef("paid_snapshot", "money", "Оплачено (в файле)",
             ("оплачено на текущую дату", "оплачено"), readonly=True),
    FieldDef("remaining_snapshot", "money", "Остаток (в файле)",
             ("остаток оплаты", "остаток", "сумма остаток"), readonly=True),
    # Считаются из журнала операций (`payments.py`), в договоре не хранятся.
    # Написаний шапки нет намеренно: колонку файла «Оплачено» разбор кладёт в
    # «как было в файле», а не сюда.
    FieldDef("paid", "money", "Оплачено по выписке", readonly=True),
    FieldDef("remaining", "money", "Остаток по выписке", readonly=True),
    # Из книги-сводки компании (`summary.py`): «Сумма Факт Поступ.» строк
    # договора. В договоре не хранятся, приходят своим запросом. Написаний
    # шапки нет по той же причине, что у «по выписке».
    FieldDef("summary_paid", "money", "Оплачено (сводка)", readonly=True),
    FieldDef("summary_remaining", "money", "Остаток (сводка)", readonly=True),
    # Полных месяцев от даты договора - «Срок, мес» книги «Разовые»
    # (`ROUNDDOWN((TODAY()-дата)/30)`). Считается при чтении.
    FieldDef("age_months", "number", "Срок, мес", readonly=True),
    FieldDef("amendments_text", "text", "№ Доп. соглашения / дата",
             ("№ дополнительное соглашение/дата", "№ дополнительное соглашение",
              "дополнительное соглашение", "доп соглашения", "доп. соглашения")),
    FieldDef("amendments_summary_text", "text", "Предмет доп. соглашения",
             ("предмет доп.соглашения", "предмет доп. соглашения", "предмет доп соглашения")),
    FieldDef("end_date", "date", "Дата расторжения / исполнения",
             ("дата расторжения договора/дата исполения разового договора",
              "дата расторжения договора/дата исполнения разового договора",
              "дата расторжения договора", "дата расторжения", "дата исполнения",
              "факт дата завершения")),
    FieldDef("note", "text", "Примечания", ("*примечания", "примечания", "примечание", "комментарий")),
    # Ниже - поля, которых в файле BBC нет колонкой: они выводятся или
    # задаются в карточке.
    FieldDef("billing", "choice", "Начисление"),
    FieldDef("economic_role", "list", "Хозяйственный смысл", fill="list"),
    FieldDef("amount_terms", "text", "Условие суммы"),
    FieldDef("end_kind", "choice", "Смысл даты окончания"),
    FieldDef("currency", "text", "Валюта", hidden=True),
)
SYSTEM_KEYS = frozenset(item.key for item in SYSTEM_FIELDS)
FIELD_BY_KEY = {item.key: item for item in SYSTEM_FIELDS}
#: Поля-списки с собственными значениями в `list_values`.
SYSTEM_LISTS = ("status", "type", "subject", "economic_role")
#: Поля, правка которых у существующего договора спрашивает «опечатка или с
#: даты». Список отдаёт схема, чтобы клиент не держал свой.
MODE_FIELDS = ("executor", "customer", "amount")
#: Поля «как было в файле» - только чтение.
SNAPSHOT_FIELDS = ("paid_snapshot", "remaining_snapshot")
#: Поля, которые считаются из журнала операций, - только чтение, в договоре не
#: хранятся. Видны тому, кому открыт журнал: сумма оплат - это деньги компании.
LIVE_FIELDS = ("paid", "remaining")
#: Поля из книги-сводки (`summary.py`) - только чтение, в договоре не хранятся.
SUMMARY_FIELDS = ("summary_paid", "summary_remaining")
#: Поля, которые выводятся из самого договора при чтении, - только чтение.
DERIVED_FIELDS = ("age_months",)
#: Всё, что только читается и не хранится: правка, загрузка и значение по
#: умолчанию их не трогают.
COMPUTED_FIELDS = LIVE_FIELDS + SUMMARY_FIELDS + DERIVED_FIELDS
#: Книги листов: `""` - реестр (карточки и «Таблица»), `oneoff` - «Разовые».
BOOKS = ("", "oneoff")
CHOICES = {
    "billing": (("month", "В месяц"), ("total", "Вся сумма"), ("terms", "Условие")),
    "end_kind": (("terminated", "Расторжение"), ("fulfilled", "Исполнение"), ("unknown", "Не ясен")),
}
#: Какие способы заполнения есть у типа поля. Первый - умолчание своего поля:
#: так свои списки вели себя и до настройки (новое значение заводилось само).
FILL_OPTIONS: dict[str, tuple[str, ...]] = {
    "list": ("hint", "list"),
    "multi_list": ("hint", "list"),
    "department": ("hint", "list"),
    "person": ("hint", "list"),
    "party": ("hint", "own"),
}


def fill_of(item: Any) -> str:
    """Способ заполнения поля: записанный, иначе умолчание системного поля или типа.

    Пустое в базе значит «как по умолчанию» - поэтому новой ревизии не нужно
    переписывать поля всех компаний, а смена умолчания в коде доезжает до тех,
    кто его не трогал.
    """
    options = FILL_OPTIONS.get(item.type, ())
    if not options:
        return ""
    stored = getattr(item, "fill", "") or ""
    if stored in options:
        return stored
    default = FIELD_BY_KEY[item.key].fill if getattr(item, "system", False) and item.key in FIELD_BY_KEY else ""
    return default if default in options else options[0]


# ── Засев ────────────────────────────────────────────────────────────────────

#: Статусы. Фаза - системный смысл; подпись компания меняет как хочет.
SEED_STATUSES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("Действующий", {"phase": "active"}),
    ("На исполнении", {"phase": "in_progress"}),
    ("Приостановлен", {"phase": "suspended"}),
    ("Исполнен", {"phase": "fulfilled"}),
    ("Недействующий", {"phase": "terminated"}),
    ("Не состоялся", {"phase": "failed"}),
)
#: Виды договоров: как начислять, какой смысл по умолчанию, как подписаны
#: стороны. «Иное» - вид без начисления, но со смыслом «вид известен».
SEED_TYPES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("Абонентское обслуживание", {"billing": "month", "economic_role": "revenue"}),
    ("Разовая услуга", {"billing": "total", "economic_role": "revenue"}),
    ("Аренда", {"billing": "month", "economic_role": "revenue",
                "roles": {"executor": "Арендодатель", "customer": "Арендатор"}}),
    ("Агентский", {"billing": "terms"}),
    ("Заём", {"billing": "total", "economic_role": "financing",
              "roles": {"executor": "Займодавец", "customer": "Займополучатель"}}),
    ("Купля-продажа", {"billing": "total",
                       "roles": {"executor": "Продавец", "customer": "Покупатель"}}),
    ("Иное", {"kind": "other"}),
)
SEED_ECONOMIC_ROLES: tuple[tuple[str, str], ...] = (
    ("Выручка", "revenue"),
    ("Расход", "expense"),
    ("Финансирование", "financing"),
    ("Внутри группы", "intra_group"),
)

#: Смысл предмета по словам в названии. Это не угадывание, а подстановка с
#: пометкой `{ по предмету }`: её видно в карточке, и человек её правит. Без
#: неё «Финансовая помощь» при виде «Иное» считалась бы выручкой и попала бы в
#: порог НДС.
SUBJECT_HINTS: tuple[tuple[re.Pattern[str], dict[str, Any]], ...] = (
    (re.compile(r"финанс\w*\s+помощ|за[её]м|займ", re.IGNORECASE),
     {"billing": "total", "economic_role": "financing",
      "roles": {"executor": "Займодавец", "customer": "Займополучатель"}}),
    (re.compile(r"агент", re.IGNORECASE), {"billing": "terms"}),
    (re.compile(r"аренд", re.IGNORECASE),
     {"billing": "month", "roles": {"executor": "Арендодатель", "customer": "Арендатор"}}),
    (re.compile(r"купл\w*[-\s]*продаж", re.IGNORECASE),
     {"billing": "total", "roles": {"executor": "Продавец", "customer": "Покупатель"}}),
)


def subject_meaning(value: str) -> dict[str, Any]:
    """Смысл нового предмета по словам в названии; пусто - смысла нет."""
    for pattern, meaning in SUBJECT_HINTS:
        if pattern.search(value or ""):
            return dict(meaning)
    return {}


# ── Похожие значения ─────────────────────────────────────────────────────────
#
# В «списке или своём» (вид, предмет) новое значение заводится из напечатанного
# - так задумано «Настройками реестра»: услуги у BBC появляются постоянно. Цена
# - двойники: «Абонентское обслуживаниее» рядом с «Абонентское обслуживание»
# делят отчёт надвое. Здесь их находят, а сводит человек («не угадывать»).

_SIMILAR_JUNK = re.compile(r"[^0-9a-zа-я]+")


def similar_key(text: Any) -> str:
    """Значение без регистра, пробелов и знаков: «Бух. сопровождение» = «бух сопровождение»."""
    return _SIMILAR_JUNK.sub("", norm(text))


def _distance(a: str, b: str, limit: int) -> int:
    """Опечаток между строками (замена, вставка, пропуск, перестановка соседних);
    больше `limit` - считать дальше незачем."""
    previous2: list[int] = []
    previous = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [0] * len(b)
        best = current[0]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                value = min(value, previous2[j - 2] + 1)
            current[j] = value
            best = min(best, value)
        if best > limit:
            return limit + 1
        previous2, previous = previous, current
    return previous[len(b)]


def is_similar(a: Any, b: Any) -> bool:
    """Двойник: то же без пробелов и знаков или одна-две опечатки.

    Короче пяти букв - только полное совпадение: «НО» и «ЮО» - разные отделы,
    а не опечатка. Длиннее двенадцати - до двух опечаток, короче - одна. Одно
    значение - другое с приставкой спереди («Недействующий») - не двойник.
    """
    left, right = similar_key(a), similar_key(b)
    if not left or not right:
        return False
    if left == right:
        return True
    if left.endswith(right) or right.endswith(left):
        # «Недействующий» - это «Действующий» с «не» спереди: две правки по
        # счёту, но слово противоположное. Приставка - не опечатка.
        return False
    if min(len(left), len(right)) < 5:
        return False
    limit = 1 if max(len(left), len(right)) < 12 else 2
    if abs(len(left) - len(right)) > limit:
        return False
    return _distance(left, right, limit) <= limit


def similar_values(values: Iterable[Any]) -> dict[uuid.UUID, uuid.UUID]:
    """Значение → его более раннее похожее значение того же списка.

    Отмеченные «это разные» (`meaning.distinct`) парой не считаются.
    """
    items = sorted(values, key=lambda item: item.position)
    out: dict[uuid.UUID, uuid.UUID] = {}
    for index, later in enumerate(items):
        apart = set((later.meaning or {}).get("distinct") or [])
        for earlier in items[:index]:
            if str(earlier.id) in apart or str(later.id) in set((earlier.meaning or {}).get("distinct") or []):
                continue
            if is_similar(earlier.value, later.value):
                out[later.id] = earlier.id
                break
    return out


# ── Нормализация ─────────────────────────────────────────────────────────────

_QUOTES = re.compile(r"[«»\"'“”„`]")
_ORG_FORMS = (
    (re.compile(r"товарищество\s+с\s+ограниченной\s+ответственностью", re.IGNORECASE), "тоо"),
    (re.compile(r"индивидуальный\s+предприниматель", re.IGNORECASE), "ип"),
    (re.compile(r"акционерное\s+общество", re.IGNORECASE), "ао"),
)


def party_key(name: Any) -> str:
    """Ключ стороны: без кавычек, регистра, пробелов и знаков.

    «ТОО «Атриум плюс»», 'ТОО "АТРИУМ ПЛЮС"' и «ТОО Атриум  плюс» - один
    ключ. Организационная форма остаётся: «ТОО Альфа» и «ИП Альфа» - разные
    лица, и склеивать их молча нельзя. Похожие, но не равные по ключу имена
    сводит только человек («Это ТОО «Атриум плюс»?»).
    """
    text = _QUOTES.sub(" ", str(name or ""))
    for pattern, short in _ORG_FORMS:
        text = pattern.sub(short, text)
    return squash(text)


_NUMBER_JUNK = re.compile(r"[\s№#]+")


def number_key(number: Any) -> str:
    """Номер договора без пробелов, «№» и регистра - для поиска повторов."""
    return _NUMBER_JUNK.sub("", str(number or "")).lower()


# ── Счётчики ─────────────────────────────────────────────────────────────────


#: Ключ в `session.info`: счётчики, чей сдвиг отложен до конца транзакции.
DEFERRED = "finance.deferred_bumps"


class deferred_bumps:
    """Отложить сдвиг счётчиков до выхода из блока - одним сдвигом в конце.

    Сдвиг держит блокировку строки счётчика до коммита. «Завести» реестра
    заводит юрлица, значения списков, отделы, поля и листы - и каждый из
    них двигал номер схемы в начале длинной транзакции: правка коллеги,
    заводящая новое значение списка, ждала всю загрузку. Внутри блока
    `bump` только помечает счётчик, настоящий сдвиг - один, на выходе.
    """

    def __init__(self, session: Session, workspace_id: uuid.UUID, *names: str):
        self.session, self.workspace_id, self.names = session, workspace_id, set(names)

    def __enter__(self) -> "deferred_bumps":
        self.session.info[DEFERRED] = {"names": self.names, "dirty": set()}
        return self

    def __exit__(self, kind, exc, tb) -> None:
        state = self.session.info.pop(DEFERRED, None)
        if kind is None and state:
            for name in sorted(state["dirty"]):
                bump(self.session, self.workspace_id, name)


def bump(session: Session, workspace_id: uuid.UUID, name: str) -> int:
    """Следующее значение счётчика компании.

    Блокировка строки держится до конца транзакции - номера видны строго по
    порядку (см. `Counter`). Первая запись счётчика заводится во вложенной
    транзакции: два первых запроса одновременно не должны уронить друг друга
    на уникальном ключе. Внутри `deferred_bumps` сдвиг откладывается.
    """
    deferred = session.info.get(DEFERRED)
    if deferred and name in deferred["names"]:
        deferred["dirty"].add(name)
        return current(session, workspace_id, name)
    table = Counter.__table__
    statement = (
        sa.update(table)
        .where(table.c.workspace_id == workspace_id, table.c.name == name)
        .values(value=table.c.value + 1)
        .returning(table.c.value)
    )
    value = session.execute(statement).scalar()
    if value is not None:
        return int(value)
    try:
        with session.begin_nested():
            session.add(Counter(workspace_id=workspace_id, name=name, value=1))
        return 1
    except IntegrityError:
        value = session.execute(statement).scalar()
        return int(value or 1)


def current(session: Session, workspace_id: uuid.UUID, name: str) -> int:
    value = session.scalar(
        sa.select(Counter.value).where(Counter.workspace_id == workspace_id, Counter.name == name)
    )
    return int(value or 0)


# ── Засев реестра ────────────────────────────────────────────────────────────


def _field_rows(session: Session, workspace_id: uuid.UUID) -> dict[str, EntityField]:
    rows = session.scalars(
        sa.select(EntityField).where(
            EntityField.workspace_id == workspace_id, EntityField.entity == ENTITY
        )
    )
    return {row.key: row for row in rows}


def ensure_registry(session: Session, workspace: Workspace) -> None:
    """Досеять реестр компании. Повторный вызов ничего не меняет.

    Всё во вложенной транзакции: реестр открывается несколькими запросами
    сразу (схема, договоры, опрос), и два засева одновременно не должны
    уронить друг друга - второй просто увидит готовое. Урок «Финансов»: раздел
    открывался двумя запросами, оба создавали пространство, второй падал на
    уникальном ключе, и экран оставался пустым.
    """
    existing = _field_rows(session, workspace.id)
    if SYSTEM_KEYS <= set(existing) and existing and _has_main_view(session, workspace.id):
        return
    try:
        with session.begin_nested():
            _seed(session, workspace, existing)
    except IntegrityError:
        # Соседний запрос успел засеять - берём его результат.
        session.expire_all()


def _has_main_view(session: Session, workspace_id: uuid.UUID) -> bool:
    return (
        session.scalar(
            sa.select(sa.func.count())
            .select_from(EntityView)
            .where(EntityView.workspace_id == workspace_id, EntityView.main.is_(True))
        )
        or 0
    ) > 0


def _seed(session: Session, workspace: Workspace, existing: dict[str, EntityField]) -> None:
    first_time = not existing
    previous: EntityField | None = None
    added: list[str] = []
    for index, item in enumerate(SYSTEM_FIELDS):
        if item.key in existing:
            previous = existing[item.key]
            continue
        position = (index + 1) * POSITION_STEP
        if not first_time and previous is not None:
            # Поле новой версии встаёт за своим соседом по порядку, а не на
            # позицию из засева: у компании позиции уже подвинуты руками, и
            # «Оплачено по выписке» оказалось бы среди соглашений.
            position = previous.position + 1
        row = EntityField(
            workspace_id=workspace.id,
            entity=ENTITY,
            key=item.key,
            system=True,
            type=item.type,
            title=item.title,
            names=list(item.names),
            hidden=item.hidden,
            position=position,
        )
        session.add(row)
        previous = row
        added.append(item.key)
    if not first_time and set(LIVE_FIELDS) & set(added):
        _add_live_columns(session, workspace.id)
    if first_time:
        for index, (value, meaning) in enumerate(SEED_STATUSES):
            _seed_value(session, workspace.id, "status", value, meaning, index)
        for index, (value, meaning) in enumerate(SEED_TYPES):
            _seed_value(session, workspace.id, "type", value, meaning, index)
        for index, (value, system) in enumerate(SEED_ECONOMIC_ROLES):
            _seed_value(session, workspace.id, "economic_role", value, {"system": system}, index)
    if not _has_book(session, workspace.id, "oneoff"):
        session.flush()
        _seed_oneoff(session, workspace)
    if not _has_main_view(session, workspace.id):
        session.add(
            EntityView(
                workspace_id=workspace.id,
                entity=ENTITY,
                key="main",
                title="Все договоры",
                main=True,
                blocks=[{"title": "", "filter": {"any": []}, "roles": {}, "columns": [], "defaults": {}}],
                position=0,
            )
        )
    session.flush()
    bump(session, workspace.id, "schema")


def _has_book(session: Session, workspace_id: uuid.UUID, book: str) -> bool:
    """Есть ли в книге хоть один лист - и убранный тоже: удалённое не засевается снова."""
    return (
        session.scalar(
            sa.select(sa.func.count())
            .select_from(EntityView)
            .where(EntityView.workspace_id == workspace_id, EntityView.book == book)
        )
        or 0
    ) > 0


#: Колонки листов «Разовых» - как в книге юротдела BBC «Разовые» (27.09.2026):
#: «Оплачено» - из сводки, «Остаток» - сумма минус оплачено.
_ONEOFF_COLUMNS: tuple[tuple[str, str], ...] = (
    ("row_number", "№"),
    ("status", "Статус"),
    ("planned_end_at", "План дата завершения"),
    ("people", "Ответ. лицо"),
    ("executor", "Исполнитель"),
    ("customer", "Заказчик"),
    ("number", "№ договора"),
    ("signed_at", "Дата договора"),
    ("department", "Отдел"),
    ("type", "Вид услуги"),
    ("subject", "Предмет исполнения"),
    ("amount", "Сумма договора"),
    ("summary_paid", "Оплачено"),
    ("summary_remaining", "Остаток"),
    ("end_date", "Факт дата завершения"),
    ("note", "Примечания"),
)
_CLOSED = ["fulfilled", "terminated", "failed"]


def _oneoff_columns(with_age: bool) -> list[dict[str, Any]]:
    columns = [{"key": key, "label": label, "width": None} for key, label in _ONEOFF_COLUMNS]
    if with_age:
        columns.append({"key": "age_months", "label": "Срок, мес", "width": None})
    return columns


def oneoff_status_extras(session: Session, workspace_id: uuid.UUID) -> dict[str, Any]:
    """Статусы «Разовых» - два, как в книге юротдела: «на исполнении» и «исполнен».

    Значения ищутся по смыслу (фаза), а не по написанию: у компании они могут
    называться по-своему. `choices` - что предлагает выбор, `status` -
    статус новой строки, `done` - статус, при котором строка подсвечивается.
    Нет одного из двух - книга остаётся без ограничения и подсветки.
    """
    rows = session.scalars(
        sa.select(ListValue)
        .where(
            ListValue.workspace_id == workspace_id,
            ListValue.field_key == "status",
            ListValue.archived_at.is_(None),
        )
        .order_by(ListValue.position, ListValue.created_at)
    ).all()
    first = {}
    for row in rows:
        phase = (row.meaning or {}).get("phase")
        if phase in ("in_progress", "fulfilled") and phase not in first:
            first[phase] = str(row.id)
    if len(first) < 2:
        return {}
    return {
        "choices": [first["in_progress"], first["fulfilled"]],
        "status": first["in_progress"],
        "done": first["fulfilled"],
    }


def _seed_oneoff(session: Session, workspace: Workspace) -> None:
    """Книга «Разовые»: те же договоры вида «Разовая услуга», свои листы.

    Повторяет книгу юротдела BBC: весь список, отборы незавершённых по сроку
    от даты договора («до 2 мес» … «6+ мес», как `техн 2` = месяцы по 30
    дней) и «Остатки» - работа идёт / завершена, а оплачено не всё. Засев
    общий: вид «Разовая услуга» засевается каждой компании. Нет его (удалили,
    переименовали) - книга остаётся пустой, листы заводят в настройке.

    Статус - только два книги юротдела. 30.09.2026 в «Разовых ЮО» стояли
    десять «Не состоялся», «Недействующий» и «нужно закрыть по бух» -
    договоры главного реестра, которых в книге юротдела нет. Решение
    владельца: все договоры видны в реестре, а в «Разовых» (листы и
    карточки) - только «На исполнении» и «Исполнен», без статуса тоже нет.
    """
    value = session.scalar(
        sa.select(ListValue).where(
            ListValue.workspace_id == workspace.id,
            ListValue.field_key == "type",
            ListValue.normalized == norm("Разовая услуга"),
            ListValue.archived_at.is_(None),
        )
    )
    if value is None:
        return
    oneoff = {"field": "type", "op": "in", "value": [str(value.id)]}
    open_ = {"field": "phase", "op": "not_in", "value": _CLOSED}
    defaults = {"type": str(value.id), "own_side": "executor"}
    extras = oneoff_status_extras(session, workspace.id)
    if extras.get("status"):
        defaults["status"] = extras["status"]
    two = [{"field": "status", "op": "in", "value": list(extras["choices"])}] if extras.get("choices") else []

    def block(title: str, *conditions: dict[str, Any], with_age: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "title": title,
            "filter": {"any": [{"all": [oneoff, *two, *conditions]}]},
            "roles": {},
            "columns": _oneoff_columns(with_age),
            "defaults": dict(defaults),
        }
        if extras.get("choices"):
            out["choices"] = {"status": list(extras["choices"])}
        if extras.get("done"):
            out["paint"] = [
                {
                    "filter": {"any": [{"all": [{"field": "status", "op": "in", "value": [extras["done"]]}]}]},
                    "tone": "done",
                }
            ]
        return out

    def age(op: str, months: int) -> dict[str, Any]:
        return {"field": "age_months", "op": op, "value": months}

    remaining = {"field": "summary_remaining", "op": "gt", "value": 0}
    sheets: list[tuple[str, str, list[dict[str, Any]]]] = [
        ("oneoff", "Разовые", [block("", with_age=False)]),
        ("oneoff_2m", "до 2 мес", [block("", open_, age("lt", 2))]),
        ("oneoff_3m", "2–3 мес", [block("", open_, age("gte", 2), age("lt", 3))]),
        ("oneoff_6m", "3–6 мес", [block("", open_, age("gte", 3), age("lt", 6))]),
        ("oneoff_6p", "6+ мес", [block("", open_, age("gte", 6))]),
        (
            "oneoff_rest",
            "Остатки",
            [
                block("Работа идёт - есть остаток", open_, remaining),
                block("Работа завершена - есть остаток", {"field": "phase", "op": "in", "value": ["fulfilled"]}, remaining),
            ],
        ),
    ]
    taken = set(
        session.scalars(sa.select(EntityView.key).where(EntityView.workspace_id == workspace.id))
    )
    for index, (key, title, blocks) in enumerate(sheets):
        if key in taken:
            key = slug_for(title, taken)
        taken.add(key)
        session.add(
            EntityView(
                workspace_id=workspace.id,
                entity=ENTITY,
                key=key,
                title=title,
                main=False,
                book="oneoff",
                blocks=blocks,
                position=(index + 1) * POSITION_STEP,
            )
        )


def with_live_columns(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Колонки блока с «Оплачено/Остаток по выписке» сразу за колонками файла.

    Колонки листа из Excel заданы списком из шапки файла, и новое поле само в
    такой лист не попадает. Ставим его туда, где человек смотрел на оплату:
    за «Оплачено/Остаток» из файла. Нет их в блоке - колонки не добавляются.
    """
    keys = [column.get("key") for column in columns]
    anchors = [index for index, key in enumerate(keys) if key in SNAPSHOT_FIELDS]
    missing = [key for key in LIVE_FIELDS if key not in keys]
    if not anchors or not missing:
        return columns
    at = anchors[-1] + 1
    return [*columns[:at], *({"key": key, "label": "", "width": None} for key in missing), *columns[at:]]


def _add_live_columns(session: Session, workspace_id: uuid.UUID) -> None:
    """Листы, загруженные до появления полей из выписок, - `with_live_columns`."""
    views = session.scalars(
        sa.select(EntityView).where(
            EntityView.workspace_id == workspace_id, EntityView.entity == ENTITY, EntityView.archived_at.is_(None)
        )
    )
    for view in views:
        blocks = [
            {**block, "columns": with_live_columns(list((block or {}).get("columns") or []))}
            for block in view.blocks or []
        ]
        if blocks != (view.blocks or []):
            view.blocks = blocks


def _seed_value(
    session: Session, workspace_id: uuid.UUID, field_key: str, value: str, meaning: dict, index: int
) -> None:
    session.add(
        ListValue(
            workspace_id=workspace_id,
            field_key=field_key,
            value=value,
            normalized=norm(value),
            meaning=dict(meaning),
            position=(index + 1) * POSITION_STEP,
        )
    )


def economic_role_values(session: Session, workspace_id: uuid.UUID) -> dict[str, ListValue]:
    """Значения хозяйственного смысла по системному смыслу: `revenue` → «Выручка».

    Если у компании несколько значений с одним системным смыслом («Агентский
    внутри ГК» поверх «Внутри группы»), подстановка берёт первое по порядку -
    своё значение человек выбирает руками.
    """
    rows = session.scalars(
        sa.select(ListValue)
        .where(
            ListValue.workspace_id == workspace_id,
            ListValue.field_key == "economic_role",
            ListValue.archived_at.is_(None),
        )
        .order_by(ListValue.position)
    )
    out: dict[str, ListValue] = {}
    for row in rows:
        system = (row.meaning or {}).get("system")
        if system in ECONOMIC_ROLES and system not in out:
            out[system] = row
    return out


@dataclass
class FieldView:
    """Поле, как его видит конкретный человек: подпись, тип, можно ли править."""

    key: str
    type: str
    title: str
    system: bool
    editable: bool
    required: bool
    hidden: bool
    position: int
    names: list[str] = field(default_factory=list)
    fill: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = {
            "key": self.key,
            "type": self.type,
            "title": self.title,
            "system": self.system,
            "editable": self.editable,
            "required": self.required,
            "hidden": self.hidden,
            "position": self.position,
        }
        if self.key in CHOICES:
            out["choices"] = [{"value": value, "label": label} for value, label in CHOICES[self.key]]
        if self.fill:
            out["fill"] = self.fill
            out["fills"] = list(FILL_OPTIONS.get(self.type, ()))
        return out


def fields_of(session: Session, workspace_id: uuid.UUID) -> list[EntityField]:
    """Живые поля реестра по порядку: системные и свои."""
    return list(
        session.scalars(
            sa.select(EntityField)
            .where(
                EntityField.workspace_id == workspace_id,
                EntityField.entity == ENTITY,
                EntityField.archived_at.is_(None),
            )
            .order_by(EntityField.position, EntityField.created_at)
        )
    )


def slug_for(title: str, taken: set[str]) -> str:
    """Ключ своего поля из подписи: «Источник клиента» → `istochnik_klienta`."""
    base = _translit(title) or "pole"
    base = base[:40].strip("_") or "pole"
    candidate, index = base, 2
    while candidate in taken or candidate in SYSTEM_KEYS:
        candidate = f"{base}_{index}"
        index += 1
    return candidate


_TRANSLIT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
        "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
        "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
        "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
        "я": "ya", "қ": "k", "ғ": "g", "ң": "n", "ү": "u", "ұ": "u", "ө": "o", "һ": "h",
        "ә": "a", "і": "i",
    }
)


def _translit(title: str) -> str:
    text = norm(title).translate(_TRANSLIT)
    return re.sub(r"[^0-9a-z]+", "_", text).strip("_")


__all__ = [
    "CHOICES",
    "ENTITY",
    "FIELD_BY_KEY",
    "FILL_OPTIONS",
    "FieldDef",
    "FieldView",
    "BOOKS",
    "oneoff_status_extras",
    "COMPUTED_FIELDS",
    "DERIVED_FIELDS",
    "LIVE_FIELDS",
    "SUMMARY_FIELDS",
    "MODE_FIELDS",
    "SNAPSHOT_FIELDS",
    "SUBJECT_HINTS",
    "SYSTEM_FIELDS",
    "SYSTEM_KEYS",
    "SYSTEM_LISTS",
    "bump",
    "current",
    "economic_role_values",
    "ensure_registry",
    "fields_of",
    "fill_of",
    "is_similar",
    "number_key",
    "party_key",
    "similar_key",
    "similar_values",
    "slug_for",
    "subject_meaning",
    "with_live_columns",
]
