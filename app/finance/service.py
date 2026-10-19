"""Работа с данными раздела «Финансы»: справочники, операции, импорт.

Здесь нет ни одного расчёта отчётов - они в `reports.py`. Разделение не
формальное: справочники и операции меняют правду, отчёты только читают, и
смешивать эти две ответственности в одном файле означает однажды написать
отчёт, который что-то поправил «по дороге».

Про версии правок
─────────────────
У операции есть `version`. Правка присылает ту версию, которую видела, и если
в базе уже другая - правка отклоняется с 409, а не перетирает чужую работу.
Это не теория: в разделе будут работать несколько финансистов одновременно, а
табличный вид отправляет правку по каждой ячейке, то есть шансов разъехаться
больше, чем у формы.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.finance.layout import norm
from app.finance import banks
from app.finance.config import finance_settings
from app.finance.importing import ParsedRow, Preview
from app.finance.models import (
    POSITION_STEP,
    Account,
    Category,
    Counterparty,
    ImportBatch,
    ImportRow,
    Operation,
    OperationCategory,
    OperationProject,
    OperationTag,
    Plan,
    Project,
    Tag,
    Workspace,
)

log = logging.getLogger(__name__)

DEFAULT_SLUG = "default"


class FinanceError(RuntimeError):
    """Действие невозможно по смыслу: нет счёта, сумма не та, версия устарела."""


class VersionConflict(FinanceError):
    """Операцию уже поправил кто-то другой."""


# ── Пределы значений ─────────────────────────────────────────────────────────
#
# Столбец денег - `numeric(18, 2)`, то есть абсолютная величина меньше 10^16.
# Само по себе это не новость; новость в том, что без проверки перед записью
# Postgres отвечает `NumericValueOutOfRange`, psycopg роняет запрос, и человек
# получает пятисотую вместо объяснения. Проверено вводом сорока девяток в
# ячейку «Сумма»: 500 Internal Server Error и пустой экран.
#
# Потолок взят с запасом на одну цифру вниз: `amount_base` считается как сумма
# × курс, и сумма, ровно влезающая в столбец, после пересчёта в валюту
# компании уже не влезала бы - падение переехало бы на строку ниже и выглядело
# бы совсем необъяснимо.
MONEY_MAX = Decimal("999999999999.99")  # 10^12 − 0.01

#: Разумный век для даты платежа. Не «валидация ради валидации»: год набирают
#: руками и промахиваются мимо клавиши, а операция с датой 2926 года молча
#: становится планом и навсегда оседает в «с учётом ожиданий» и в календаре.
#: Громкий отказ здесь дешевле тихой цифры, которую никто не пойдёт искать.
DATE_MIN = date(2000, 1, 1)
DATE_MAX = date(2100, 12, 31)


def check_money(value: Any, *, field: str = "Сумма") -> Decimal:
    """Сумма к записи: влезает в столбец и уже округлена до копеек.

    Округляем здесь, а не полагаемся на Postgres, потому что округляли в двух
    местах по-разному. Столбец `numeric(18, 2)` округляет `0.005` вверх, до
    копейки, а `amount_base` считался через `quantize` с банковским правилом -
    в ноль. Одна и та же операция весила копейку в сводке и ноль в отчёте,
    и выписка по счёту разошлась со сводкой на 0.01 ₸ ровно по этой причине.
    Расхождение в копейку не «мелочь»: оно означает, что два экрана считают
    по-разному, и в следующий раз разойдутся на большее.

    Ненулевая сумма, которая округляется в ноль, - отказ. Записать её значит
    показать в журнале строку на ноль тенге там, где деньги были.
    """
    try:
        amount = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise FinanceError(f"{field}: это не сумма") from exc
    if not amount.is_finite():
        raise FinanceError(f"{field}: это не сумма")
    if abs(amount) > MONEY_MAX:
        raise FinanceError(
            f"{field} слишком большая: потолок {MONEY_MAX:,.2f}".replace(",", " ")
            + ". Проверьте, не лишние ли это нули"
        )
    rounded = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount and not rounded:
        raise FinanceError(
            f"{field} «{amount}» меньше копейки - в учёте она стала бы нулём"
        )
    return rounded


def check_date(value: date | None, *, field: str) -> date | None:
    """Дата в пределах разумного века - иначе опечатка в годе уедет в отчёт."""
    if value is None:
        return None
    if not DATE_MIN <= value <= DATE_MAX:
        raise FinanceError(
            f"{field} «{value.isoformat()}» вне разумных пределов "
            f"({DATE_MIN.year}–{DATE_MAX.year}). Похоже на опечатку в годе"
        )
    return value


# ── Пространство и первичное наполнение ──────────────────────────────────────


#: Счета, с которых начинают почти все. Заводятся при создании пространства,
#: потому что пустой раздел без счетов не даёт сделать ни одного действия: ни
#: записать операцию, ни загрузить выписку.
SEED_ACCOUNTS = (
    ("Банковский счёт", "bank"),
    ("Касса", "cash"),
)

#: Статьи, которые в управленческом учёте называются одинаково почти везде.
#: Это не «правильный» список, а стартовый: его переименуют под себя.
SEED_CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("income", "Выручка", ""),
    ("income", "Прочий доход", ""),
    ("income", "Получение кредита", "loan_in"),
    ("income", "Возврат займа", "loan_return"),
    ("expense", "Аренда", ""),
    ("expense", "Зарплата", ""),
    ("expense", "Закуп товара", ""),
    ("expense", "Услуги подрядчиков", ""),
    ("expense", "Налоги и сборы", "tax"),
    ("expense", "Погашение кредита", "loan_out"),
    ("expense", "Дивиденды", "dividend"),
)

#: Природа начальных статей. Отдельным словарём, а не четвёртым полем
#: кортежа: так её видно одним взглядом, и ошибка «кредит посчитан выручкой»
#: ловится чтением, а не отчётом.
SEED_NATURES: dict[str, str] = {
    "Выручка": "revenue",
    "Прочий доход": "other",
    "Получение кредита": "capital",
    "Возврат займа": "capital",
    "Закуп товара": "cogs",
    "Налоги и сборы": "tax",
    "Погашение кредита": "capital",
    "Дивиденды": "capital",
}


def create_workspace(session: Session, *, title: str) -> Workspace:
    """Новая компания с начальными справочниками.

    Зовётся из регистрации и из «добавить ещё одну компанию». Пустая компания
    без счетов не даёт сделать ни одного действия - ни записать операцию, ни
    загрузить выписку, - поэтому счета и статьи заводятся сразу.

    `slug` собирается из названия и номера: он нужен только как человекочитаемый
    ключ в адресах и логах, уникальность держится счётчиком, а не транслитом.
    """
    clean = (title or "").strip() or "Компания"
    base = re.sub(r"[^a-z0-9]+", "-", clean.lower()).strip("-") or "company"
    slug = base
    suffix = 2
    while session.scalar(sa.select(Workspace.id).where(Workspace.slug == slug)) is not None:
        slug = f"{base}-{suffix}"
        suffix += 1

    workspace = Workspace(
        slug=slug, title=clean, base_currency=finance_settings.base_currency
    )
    session.add(workspace)
    session.flush()
    _seed(session, workspace)
    log.info("finance: создана компания «%s» (%s)", clean, slug)
    return workspace


def get_workspace(session: Session, workspace_id: uuid.UUID) -> Workspace:
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise FinanceError("Компания не найдена")
    return workspace


def rename_workspace(session: Session, workspace: Workspace, *, title: str) -> Workspace:
    clean = (title or "").strip()
    if len(clean) < 2:
        raise FinanceError("У компании должно быть название")
    workspace.title = clean
    session.flush()
    return workspace


def ensure_workspace(session: Session, *, slug: str = DEFAULT_SLUG) -> Workspace:
    """Компания по умолчанию - для тестов и для данных, заведённых до учёток.

    Осталась намеренно, хотя компаний теперь много. Во-первых, на ней стоят
    полсотни тестов, и переписывать их на регистрацию значило бы проверять
    авторизацию там, где проверяют отчёты. Во-вторых, данные, заведённые до
    появления учёток, лежат именно в компании `default`, и путь к ним должен
    остаться.

    В маршрутах этой функции быть не должно: там компания берётся из сессии.
    Это проверяется тестом `test_finance_routes_take_company_from_session`.

    Про гонку на первом открытии
    ────────────────────────────
    Раздел открывается двумя запросами сразу - сводка и справочники, - и до
    исправления оба видели пустую базу и оба заводили пространство. Второй
    падал на уникальном ключе `slug`, отвечал 500, и экран оставался пустым:
    «Требуется вход» в панели, пустые справочники, ни одной подсказки, что
    делать. Поймано сквозным прогоном 18 сентября 2026; в проде это выглядело
    бы как «раздел не работает у всех, кто зашёл первым».

    Поэтому вставка идёт во вложенной транзакции: проиграли гонку - откатываем
    только её и перечитываем то, что создал сосед. К этому моменту его
    транзакция уже завершена (на конфликте уникального ключа Postgres держит
    нас на блокировке до его фиксации), поэтому перечитывание видит и
    пространство, и его начальные справочники.
    """
    workspace = session.scalar(sa.select(Workspace).where(Workspace.slug == slug))
    if workspace is not None:
        return workspace

    workspace = Workspace(
        slug=slug, title="Финансы компании", base_currency=finance_settings.base_currency
    )
    try:
        with session.begin_nested():
            session.add(workspace)
            session.flush()
    except IntegrityError:
        existing = session.scalar(sa.select(Workspace).where(Workspace.slug == slug))
        if existing is None:
            # Конфликт был, а пространства нет - это уже не гонка, а что-то
            # другое, и молчать об этом нельзя.
            raise
        log.info("finance: пространство «%s» создал параллельный запрос", slug)
        return existing

    _seed(session, workspace)
    log.info("finance: создано пространство «%s» с начальными справочниками", slug)
    return workspace


def _seed(session: Session, workspace: Workspace) -> None:
    """Начальные счета и статьи новой компании."""
    for position, (name, kind) in enumerate(SEED_ACCOUNTS):
        session.add(
            Account(
                workspace_id=workspace.id,
                name=name,
                normalized_name=norm(name),
                kind=kind,
                currency=workspace.base_currency,
                starting_balance=Decimal("0"),
                position=position * POSITION_STEP,
            )
        )
    for position, (side, name, system_key) in enumerate(SEED_CATEGORIES):
        session.add(
            Category(
                workspace_id=workspace.id,
                side=side,
                name=name,
                normalized_name=norm(name),
                system_key=system_key,
                nature=SEED_NATURES.get(name, "operating"),
                position=position * POSITION_STEP,
            )
        )
    session.flush()


# ── Справочники ──────────────────────────────────────────────────────────────


def _next_position(session: Session, model, workspace_id: uuid.UUID) -> int:
    top = session.scalar(
        sa.select(sa.func.max(model.position)).where(model.workspace_id == workspace_id)
    )
    return int(top or 0) + POSITION_STEP


def list_accounts(session: Session, workspace_id: uuid.UUID, *, with_archived: bool = False) -> list[Account]:
    query = sa.select(Account).where(Account.workspace_id == workspace_id)
    if not with_archived:
        query = query.where(Account.archived_at.is_(None))
    return list(session.scalars(query.order_by(Account.position, Account.name)))


def create_account(
    session: Session,
    workspace: Workspace,
    *,
    name: str,
    kind: str = "bank",
    currency: str | None = None,
    starting_balance: Decimal | float | str = 0,
    excluded_from_reports: bool = False,
    number: str = "",
) -> Account:
    clean = (name or "").strip()
    if not clean:
        raise FinanceError("У счёта должно быть название")
    exists = session.scalar(
        sa.select(Account).where(
            Account.workspace_id == workspace.id, Account.normalized_name == norm(clean)
        )
    )
    if exists is not None:
        raise FinanceError(f"Счёт «{clean}» уже есть")
    account = Account(
        workspace_id=workspace.id,
        name=clean,
        normalized_name=norm(clean),
        kind=kind,
        currency=(currency or workspace.base_currency).upper(),
        starting_balance=check_money(starting_balance or 0, field="Начальный остаток"),
        excluded_from_reports=excluded_from_reports,
        number=_check_number(session, workspace.id, number),
        position=_next_position(session, Account, workspace.id),
    )
    session.add(account)
    session.flush()
    return account


def _check_number(
    session: Session, workspace_id: uuid.UUID, number: str | None, *, own_id: uuid.UUID | None = None
) -> str:
    """Номер счёта без оформления - и свободен ли он в этой компании.

    Один номер у двух счетов сделал бы выбор счёта по выписке угадыванием:
    выписка легла бы на тот, что нашёлся первым.
    """
    raw = (number or "").strip()
    if not raw:
        return ""
    key = banks.account_key(raw)
    if not key:
        raise FinanceError(f"«{raw}» не похоже на номер счёта: нужны буквы и цифры, от восьми знаков")
    taken = session.scalar(
        sa.select(Account).where(
            Account.workspace_id == workspace_id,
            Account.number == key,
            Account.archived_at.is_(None),
        )
    )
    if taken is not None and taken.id != own_id:
        raise FinanceError(f"Номер {key} уже записан у счёта «{taken.name}»")
    return key


def set_account_number(
    session: Session, workspace: Workspace, account_id: uuid.UUID, number: str | None
) -> tuple[Account, str]:
    """Записать счёту номер в банке. Возвращает счёт и прежний номер."""
    account = session.get(Account, account_id)
    if account is None or account.workspace_id != workspace.id:
        raise FinanceError("Счёт не найден")
    before = account.number or ""
    account.number = _check_number(session, workspace.id, number, own_id=account.id)
    session.flush()
    return account, before


def account_numbers(session: Session, workspace_id: uuid.UUID) -> dict[str, str]:
    """Номер счёта → название. Этим импорт узнаёт счета по выписке."""
    return {item.number: item.name for item in list_accounts(session, workspace_id) if item.number}


def set_starting_balance(
    session: Session, workspace: Workspace, account_id: uuid.UUID, value: Any
) -> tuple[Account, Decimal]:
    """Поменять начальный остаток счёта. Возвращает счёт и прежний остаток.

    Раньше остаток задавался только при создании счёта. А «Банковский счёт» и
    «Касса» заводятся при регистрации сами, с нулём, - и человек, загрузивший
    в них выписку, навсегда оставался с отрицательным остатком: у выписки Kaspi
    за год «Всего на счетах» показывало −6 523,59 при 21 439,09 на карте.
    """
    account = session.get(Account, account_id)
    if account is None or account.workspace_id != workspace.id:
        raise FinanceError("Счёт не найден")
    before = Decimal(str(account.starting_balance))
    account.starting_balance = check_money(value, field="Начальный остаток")
    session.flush()
    return account, before


def account_net_before(
    session: Session, workspace_id: uuid.UUID, account_id: uuid.UUID, when: date
) -> tuple[Decimal, int]:
    """Движение по счёту до даты (факт): сумма и сколько операций.

    Нужно сверке выписки: остаток счёта на начало периода выписки - это
    начальный остаток плюс всё, что было раньше. Если раньше не было ничего,
    остаток на начало и есть начальный остаток, и его можно взять из выписки.
    """
    signed = sa.case(
        (Operation.account_to_id == account_id, Operation.amount),
        else_=-Operation.amount,
    )
    total, count = session.execute(
        sa.select(sa.func.coalesce(sa.func.sum(signed), 0), sa.func.count(Operation.id)).where(
            Operation.workspace_id == workspace_id,
            Operation.deleted_at.is_(None),
            Operation.status == "fact",
            Operation.paid_at < when,
            sa.or_(Operation.account_to_id == account_id, Operation.account_from_id == account_id),
        )
    ).one()
    return Decimal(str(total)), int(count or 0)


def reconcile_statement(
    session: Session, workspace: Workspace, preview: Preview
) -> dict[str, Any] | None:
    """Сверка выписки с банком - то, что банк напечатал, против того, что разобрано.

    Три числа из самой выписки (остаток на начало, на конец и движение по
    разобранным строкам) и одно из учёта (остаток счёта на начало периода).
    Сходится - значит, при разборе не потеряно ни одной операции, и после
    загрузки остаток счёта совпадёт с банком. Не сходится - разница видна до
    того, как операции заведены.

    Соседи по рынку этого не делают: выписка Kaspi, загруженная в Finmap,
    дала остаток −13 047,18 при 21 439,09 на карте, и ни одного слова о том,
    что остатки не сошлись.
    """
    bank = preview.bank
    if not bank:
        return None
    net = Decimal("0")
    here = bank.get("account")
    for row in preview.rows:
        amount = row.values.get("amount")
        if not amount:
            continue
        value = Decimal(str(amount))
        if row.values.get("kind") == "transfer":
            # Перевод между своими счетами: знак - с точки зрения счёта
            # выписки. Раньше любой перевод считался списанием, и перевод С
            # депозита НА счёт выписки уводил сверку в минус на двойную сумму.
            incoming = row.values.get("own_transfer") == "in" or (
                bool(here) and row.values.get("account_to") == here
            )
            net += value if incoming else -value
            continue
        net += value if row.values.get("kind") == "income" else -value
    opening = Decimal(bank["opening_balance"]) if bank.get("opening_balance") else None
    closing = Decimal(bank["closing_balance"]) if bank.get("closing_balance") else None
    out: dict[str, Any] = {
        **bank,
        "file_net": str(net),
        "expected_closing": str(opening + net) if opening is not None else None,
        "gap": str(closing - (opening + net)) if opening is not None and closing is not None else None,
        "account_id": None,
        "starting_balance": None,
        "ledger_opening": None,
        "earlier_operations": 0,
        "can_set_start": False,
    }
    name = bank.get("account")
    start = bank.get("period_start")
    if not name or not start:
        return out
    account = next(
        (item for item in list_accounts(session, workspace.id) if item.normalized_name == norm(name)),
        None,
    )
    if account is None:
        return out
    earlier, count = account_net_before(session, workspace.id, account.id, date.fromisoformat(start))
    ledger_opening = Decimal(str(account.starting_balance)) + earlier
    out.update(
        account_id=str(account.id),
        starting_balance=str(account.starting_balance),
        ledger_opening=str(ledger_opening),
        earlier_operations=count,
        # Начальный остаток из выписки предлагаем, только если раньше периода
        # по счёту ничего не было: иначе остаток на начало задают прежние
        # операции, и менять начальный остаток значило бы подгонять ответ.
        can_set_start=opening is not None and count == 0 and ledger_opening != opening,
    )
    return out


def _find_or_create(
    session: Session,
    model,
    workspace_id: uuid.UUID,
    name: str,
    *,
    extra: dict[str, Any] | None = None,
    create: bool = True,
):
    """Найти запись справочника по названию или создать её.

    Сравнение по `normalized_name`: «PW Клиент», «pw клиент» и «PW  Клиент» -
    одна и та же запись. Иначе справочник за месяц зарастает двойниками, и в
    отчёте один контрагент оказывается тремя.
    """
    clean = (name or "").strip()
    if not clean:
        return None
    conditions = [model.workspace_id == workspace_id, model.normalized_name == norm(clean)]
    for key, value in (extra or {}).items():
        conditions.append(getattr(model, key) == value)
    found = session.scalar(sa.select(model).where(*conditions))
    if found is not None or not create:
        return found
    created = model(
        workspace_id=workspace_id,
        name=clean,
        normalized_name=norm(clean),
        **(extra or {}),
    )
    if hasattr(created, "position"):
        created.position = _next_position(session, model, workspace_id)
    session.add(created)
    session.flush()
    return created


def find_entry(session: Session, model, workspace_id: uuid.UUID, name: str, **extra):
    """Найти запись справочника по названию, ничего не создавая.

    Отдельным именем, а не `create=False` у `_find_or_create`: табличный вид
    зовёт это на каждую правку ячейки, и вызов обязан читаться как «найди», а
    не как «создай, но не сейчас».
    """
    return _find_or_create(session, model, workspace_id, name, extra=extra or None, create=False)


def ensure_category(session: Session, workspace_id: uuid.UUID, side: str, name: str, *, create: bool = True):
    return _find_or_create(session, Category, workspace_id, name, extra={"side": side}, create=create)


def ensure_counterparty(
    session: Session, workspace_id: uuid.UUID, name: str, *, role: str = "client", create: bool = True
):
    return _find_or_create(session, Counterparty, workspace_id, name, extra={"role": role}, create=create)


def ensure_project(session: Session, workspace_id: uuid.UUID, name: str, *, create: bool = True):
    return _find_or_create(session, Project, workspace_id, name, create=create)


def ensure_tag(session: Session, workspace_id: uuid.UUID, name: str, *, create: bool = True):
    return _find_or_create(session, Tag, workspace_id, name, create=create)


def list_categories(session: Session, workspace_id: uuid.UUID, side: str | None = None) -> list[Category]:
    query = sa.select(Category).where(
        Category.workspace_id == workspace_id, Category.archived_at.is_(None)
    )
    if side:
        query = query.where(Category.side == side)
    return list(session.scalars(query.order_by(Category.side, Category.position, Category.name)))


def list_counterparties(session: Session, workspace_id: uuid.UUID, role: str | None = None) -> list[Counterparty]:
    query = sa.select(Counterparty).where(
        Counterparty.workspace_id == workspace_id, Counterparty.archived_at.is_(None)
    )
    if role:
        query = query.where(Counterparty.role == role)
    return list(session.scalars(query.order_by(Counterparty.position, Counterparty.name)))


def list_projects(session: Session, workspace_id: uuid.UUID) -> list[Project]:
    return list(
        session.scalars(
            sa.select(Project)
            .where(Project.workspace_id == workspace_id, Project.archived_at.is_(None))
            .order_by(Project.position, Project.name)
        )
    )


def list_tags(session: Session, workspace_id: uuid.UUID) -> list[Tag]:
    return list(
        session.scalars(
            sa.select(Tag)
            .where(Tag.workspace_id == workspace_id, Tag.archived_at.is_(None))
            .order_by(Tag.name)
        )
    )


def archive(session: Session, model, workspace_id: uuid.UUID, item_id: uuid.UUID) -> None:
    """Убрать запись справочника из списков, не удаляя.

    Удалять нельзя: на записи ссылаются операции, и удаление либо оборвёт
    ссылку, либо потребует переписать историю. «Архивная» запись исчезает из
    выпадающих списков, но прошлые отчёты остаются целыми.
    """
    item = session.get(model, item_id)
    if item is None or item.workspace_id != workspace_id:
        raise FinanceError("Запись не найдена")
    item.archived_at = datetime.now(timezone.utc)
    session.flush()


# ── Операции ─────────────────────────────────────────────────────────────────


@dataclass
class OperationInput:
    """Поля операции, приходящие из формы или из таблицы."""

    kind: str
    paid_at: date
    amount: Decimal
    status: str = "fact"
    accrued_at: date | None = None
    period_start: date | None = None
    period_end: date | None = None
    currency: str | None = None
    rate: Decimal | None = None
    account_from_id: uuid.UUID | None = None
    account_to_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    comment: str = ""
    projects: Sequence[tuple[uuid.UUID, Decimal]] = ()
    #: Дробление по статьям: один платёж на несколько статей.
    categories: Sequence[tuple[uuid.UUID, Decimal]] = ()
    tags: Sequence[uuid.UUID] = ()
    #: Откуда операция родилась, если не руками.
    recurrence_id: uuid.UUID | None = None
    integration_id: uuid.UUID | None = None
    source: str = "app"
    external_key: str | None = None
    raw: dict[str, Any] | None = None
    import_batch_id: uuid.UUID | None = None


def _validate(session: Session, workspace: Workspace, data: OperationInput) -> None:
    if data.amount is None or Decimal(data.amount) < 0:
        raise FinanceError("Сумма не может быть отрицательной: направление задаёт вид операции")
    check_money(data.amount)
    # Курс проверяем вместе с суммой: в базе лежит и то и другое, а `amount_base`
    # считается их произведением - переполнится он, а не сумма.
    if data.rate is not None:
        rate = Decimal(str(data.rate))
        if rate <= 0:
            raise FinanceError("Курс должен быть больше нуля")
        check_money(Decimal(str(data.amount)) * rate, field="Сумма в валюте компании")
    check_date(data.paid_at, field="Дата платежа")
    check_date(data.accrued_at, field="Дата начисления")
    if data.kind == "transfer":
        if not (data.account_from_id and data.account_to_id):
            raise FinanceError("Для перевода нужны оба счёта")
        if data.account_from_id == data.account_to_id:
            raise FinanceError("Перевод на тот же счёт ничего не меняет")
    # У ожидания счёта может не быть, и это не пробел в данных.
    #
    # Счёт клиенту выставляют, не зная, на какой из своих счетов придут деньги:
    # это выяснится в момент оплаты. Требовать счёт заранее значит заставить
    # выбрать наугад - а потом сверять выписку с угаданным. Для факта счёт
    # обязателен по-прежнему: деньги всегда откуда-то и куда-то двигаются.
    if data.status == "plan":
        return
    if data.kind == "income" and not data.account_to_id:
        raise FinanceError("Не указано, на какой счёт пришли деньги")
    if data.kind == "expense" and not data.account_from_id:
        raise FinanceError("Не указано, с какого счёта ушли деньги")
    for account_id in (data.account_from_id, data.account_to_id):
        if account_id is None:
            continue
        account = session.get(Account, account_id)
        if account is None or account.workspace_id != workspace.id:
            raise FinanceError("Счёт не найден")
    if data.category_id is not None:
        category = session.get(Category, data.category_id)
        if category is None or category.workspace_id != workspace.id:
            raise FinanceError("Категория не найдена")
        if data.kind != "transfer":
            expected = "income" if data.kind == "income" else "expense"
            if category.side != expected:
                raise FinanceError(
                    f"Категория «{category.name}» относится к другой стороне учёта: "
                    f"это {'доход' if category.side == 'income' else 'расход'}"
                )


def _split_state(amount: Decimal, splits: Sequence[tuple[uuid.UUID, Decimal]]) -> str:
    if not splits:
        return "none"
    total = sum((Decimal(str(value)) for _pid, value in splits), Decimal("0"))
    if total == amount:
        return "exact"
    if total > amount:
        return "mismatch"
    return "partial"


def _write_category_splits(
    session: Session, operation: Operation, splits: Sequence[tuple[uuid.UUID, Decimal]]
) -> None:
    """Переписать дробление операции по статьям.

    Части всегда положительные - знак задаёт вид операции. Сумма частей больше
    суммы операции запрещена: это не «частичное разнесение», а ошибка, и
    показать её надо в момент ввода, а не в отчёте через месяц. Меньше -
    можно: остаток остаётся на статье самой операции.

    Когда части заданы, `category_id` операции не отменяется: на нём остаётся
    неразнесённый остаток, и отчёт складывает и то и то.
    """
    session.execute(
        sa.delete(OperationCategory).where(OperationCategory.operation_id == operation.id)
    )
    if not splits:
        return
    total = Decimal("0")
    for category_id, value in splits:
        amount = Decimal(str(value))
        if amount <= 0:
            raise FinanceError("Часть платежа не может быть нулевой или отрицательной")
        total += amount
        session.add(
            OperationCategory(
                operation_id=operation.id, category_id=category_id, amount=amount
            )
        )
    if total > Decimal(str(operation.amount)):
        raise FinanceError(
            f"Части по статьям ({total}) больше суммы операции ({operation.amount})"
        )
    session.flush()


def create_operation(
    session: Session, workspace: Workspace, data: OperationInput, *, actor: str = ""
) -> Operation:
    _validate(session, workspace, data)
    # Округляет `check_money`, и только он: сумма, которую увидит база, и
    # сумма, от которой считается `amount_base`, обязаны быть одним числом.
    amount = check_money(data.amount)
    rate = Decimal(str(data.rate)) if data.rate is not None else Decimal("1")
    currency = (data.currency or workspace.base_currency).upper()
    operation = Operation(
        workspace_id=workspace.id,
        kind=data.kind,
        status=data.status,
        paid_at=data.paid_at,
        accrued_at=data.accrued_at,
        period_start=data.period_start,
        period_end=data.period_end,
        amount=amount,
        currency=currency,
        # Сумма в валюте компании считается сейчас и хранится: отчёт за июнь,
        # открытый в сентябре, обязан показывать те же цифры.
        amount_base=check_money(amount * rate, field="Сумма в валюте компании"),
        rate=rate,
        account_from_id=data.account_from_id,
        account_to_id=data.account_to_id,
        category_id=data.category_id if data.kind != "transfer" else None,
        counterparty_id=data.counterparty_id,
        comment=data.comment or "",
        split_state=_split_state(amount, data.projects),
        source=data.source,
        recurrence_id=data.recurrence_id,
        integration_id=data.integration_id,
        external_key=data.external_key,
        import_batch_id=data.import_batch_id,
        raw=data.raw or {},
        created_by=actor,
    )
    session.add(operation)
    session.flush()
    for project_id, value in data.projects:
        session.add(
            OperationProject(operation_id=operation.id, project_id=project_id, amount=Decimal(str(value)))
        )
    _write_category_splits(session, operation, data.categories)
    for tag_id in data.tags:
        session.add(OperationTag(operation_id=operation.id, tag_id=tag_id))
    session.flush()
    return operation


def update_operation(
    session: Session,
    workspace: Workspace,
    operation_id: uuid.UUID,
    changes: dict[str, Any],
    *,
    version: int | None = None,
    actor: str = "",
) -> Operation:
    """Правка полей операции. `version` - та, что видел правящий."""
    operation = session.get(Operation, operation_id)
    if operation is None or operation.workspace_id != workspace.id or operation.deleted_at is not None:
        raise FinanceError("Операция не найдена")
    if version is not None and operation.version != version:
        raise VersionConflict(
            f"Операцию уже изменили: у вас версия {version}, в базе {operation.version}. "
            "Обновите страницу, чтобы не затереть чужую правку."
        )

    projects = changes.pop("projects", None)
    categories = changes.pop("categories", None)
    tags = changes.pop("tags", None)
    for key, value in changes.items():
        if not hasattr(operation, key):
            raise FinanceError(f"Неизвестное поле «{key}»")
        setattr(operation, key, value)

    if projects is not None:
        session.execute(
            sa.delete(OperationProject).where(OperationProject.operation_id == operation.id)
        )
        for project_id, value in projects:
            session.add(
                OperationProject(
                    operation_id=operation.id, project_id=project_id, amount=Decimal(str(value))
                )
            )
        operation.split_state = _split_state(Decimal(str(operation.amount)), projects)
    if categories is not None:
        _write_category_splits(session, operation, categories)
    if tags is not None:
        session.execute(sa.delete(OperationTag).where(OperationTag.operation_id == operation.id))
        for tag_id in tags:
            session.add(OperationTag(operation_id=operation.id, tag_id=tag_id))

    # Проверяем операцию целиком, а не те поля, что вспомнились.
    #
    # `status` здесь был пропущен, и умолчание `OperationInput` подставляло
    # «факт». Значит, любое ожидание при правке проверялось как факт и
    # отклонялось с «Не указано, на какой счёт пришли деньги» - при том что
    # отсутствие счёта у ожидания разрешено намеренно (см. `_validate`). Счёт
    # клиенту выставляют, не зная, куда придут деньги; поправить у такого
    # ожидания хотя бы комментарий было нельзя.
    data = OperationInput(
        kind=operation.kind,
        status=operation.status,
        paid_at=operation.paid_at,
        accrued_at=operation.accrued_at,
        amount=Decimal(str(operation.amount)),
        rate=Decimal(str(operation.rate)) if operation.rate is not None else None,
        account_from_id=operation.account_from_id,
        account_to_id=operation.account_to_id,
        category_id=operation.category_id,
    )
    _validate(session, workspace, data)
    operation.amount = check_money(operation.amount)
    operation.amount_base = check_money(
        operation.amount * Decimal(str(operation.rate)), field="Сумма в валюте компании"
    )
    operation.version += 1
    session.flush()
    return operation


def delete_operation(
    session: Session, workspace: Workspace, operation_id: uuid.UUID, *, actor: str = ""
) -> None:
    """Пометить операцию удалённой.

    Мягко, а не `DELETE`: удалённая операция обязана остаться видимой в истории
    действий, иначе исчезновение денег из отчёта нельзя объяснить.
    """
    operation = session.get(Operation, operation_id)
    if operation is None or operation.workspace_id != workspace.id:
        raise FinanceError("Операция не найдена")
    operation.deleted_at = datetime.now(timezone.utc)
    operation.version += 1
    session.flush()


@dataclass
class OperationFilter:
    """Фильтр журнала. Пустые поля ничего не сужают."""

    date_from: date | None = None
    date_to: date | None = None
    kinds: Sequence[str] = ()
    statuses: Sequence[str] = ()
    account_ids: Sequence[uuid.UUID] = ()
    category_ids: Sequence[uuid.UUID] = ()
    counterparty_ids: Sequence[uuid.UUID] = ()
    project_ids: Sequence[uuid.UUID] = ()
    search: str = ""
    amount_from: Decimal | None = None
    amount_to: Decimal | None = None
    #: По какой дате фильтровать: платежа или сделки.
    by: str = "paid"


def _apply_filter(query, workspace_id: uuid.UUID, flt: OperationFilter):
    column = Operation.paid_at if flt.by == "paid" else sa.func.coalesce(
        Operation.accrued_at, Operation.paid_at
    )
    query = query.where(Operation.workspace_id == workspace_id, Operation.deleted_at.is_(None))
    if flt.date_from:
        query = query.where(column >= flt.date_from)
    if flt.date_to:
        query = query.where(column <= flt.date_to)
    if flt.kinds:
        query = query.where(Operation.kind.in_(list(flt.kinds)))
    if flt.statuses:
        query = query.where(Operation.status.in_(list(flt.statuses)))
    if flt.account_ids:
        ids = list(flt.account_ids)
        query = query.where(
            sa.or_(Operation.account_from_id.in_(ids), Operation.account_to_id.in_(ids))
        )
    if flt.category_ids:
        query = query.where(Operation.category_id.in_(list(flt.category_ids)))
    if flt.counterparty_ids:
        query = query.where(Operation.counterparty_id.in_(list(flt.counterparty_ids)))
    if flt.project_ids:
        query = query.where(
            Operation.id.in_(
                sa.select(OperationProject.operation_id).where(
                    OperationProject.project_id.in_(list(flt.project_ids))
                )
            )
        )
    if flt.amount_from is not None:
        query = query.where(Operation.amount >= flt.amount_from)
    if flt.amount_to is not None:
        query = query.where(Operation.amount <= flt.amount_to)
    if flt.search:
        like = f"%{flt.search.strip()}%"
        query = query.where(Operation.comment.ilike(like))
    return query


def list_operations(
    session: Session,
    workspace_id: uuid.UUID,
    flt: OperationFilter | None = None,
    *,
    limit: int = 250,
    offset: int = 0,
) -> tuple[list[Operation], int]:
    flt = flt or OperationFilter()
    base = _apply_filter(sa.select(Operation), workspace_id, flt)
    total = session.scalar(
        _apply_filter(sa.select(sa.func.count(Operation.id)), workspace_id, flt)
    )
    rows = list(
        session.scalars(
            # `id` последним ключом: операции одной выписки заводятся одной
            # транзакцией, и `created_at` у них одинаковый. Без третьего ключа
            # порядок одинаковых дат держится на плане запроса Postgres, то есть
            # на удаче - а на нём стоят страницы журнала и строки листа.
            base.order_by(Operation.paid_at.desc(), Operation.created_at.desc(), Operation.id)
            .limit(limit)
            .offset(offset)
        )
    )
    return rows, int(total or 0)


def operation_sums(
    session: Session, workspace_id: uuid.UUID, flt: OperationFilter | None = None
) -> dict[str, Decimal]:
    """Итоги журнала по всему фильтру - в базе, а не по странице.

    Раньше карточки «Поступило / Списано» над журналом складывали 250 строк
    текущей страницы. Рядом стояло «Операций: 2050» - итог по фильтру, и
    суммы читались как итог того же фильтра. На выписке Kaspi за год журнал
    показывал «Поступило 568 310,56» при настоящих 11 029 038,88: цифры
    выглядели как цифры, а врали в двадцать раз.

    Считается только факт: ожидание ещё не поступило и не списано. Так итог
    журнала совпадает с отчётом «Деньги» за тот же период.
    """
    flt = flt or OperationFilter()
    query = _apply_filter(
        sa.select(Operation.kind, sa.func.coalesce(sa.func.sum(Operation.amount_base), 0)),
        workspace_id,
        flt,
    ).where(Operation.status == "fact", Operation.kind.in_(("income", "expense")))
    sums = {"income": Decimal("0"), "expense": Decimal("0")}
    for kind, value in session.execute(query.group_by(Operation.kind)):
        sums[kind] = Decimal(str(value))
    return sums


def operation_projects(session: Session, operation_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[OperationProject]]:
    if not operation_ids:
        return {}
    rows = session.scalars(
        sa.select(OperationProject).where(OperationProject.operation_id.in_(list(operation_ids)))
    )
    grouped: dict[uuid.UUID, list[OperationProject]] = {}
    for row in rows:
        grouped.setdefault(row.operation_id, []).append(row)
    return grouped


def operation_tags(session: Session, operation_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    if not operation_ids:
        return {}
    rows = session.execute(
        sa.select(OperationTag.operation_id, OperationTag.tag_id).where(
            OperationTag.operation_id.in_(list(operation_ids))
        )
    )
    grouped: dict[uuid.UUID, list[uuid.UUID]] = {}
    for operation_id, tag_id in rows:
        grouped.setdefault(operation_id, []).append(tag_id)
    return grouped


# ── Импорт ───────────────────────────────────────────────────────────────────


def save_preview(
    session: Session, workspace: Workspace, preview: Preview, *, actor: str = ""
) -> ImportBatch:
    """Сохранить разбор файла как партию импорта, ничего ещё не заводя.

    Партия сохраняется до применения намеренно: человек уходит разбираться с
    десятью строками, возвращается через час и не должен загружать файл заново.
    """
    # Прошлый незавершённый разбор ТОГО ЖЕ источника снимается.
    #
    # Ответ на вопрос раздела («на какой счёт?», «как записаны даты?») - это
    # повторный разбор, то есть новая партия. Без уборки в истории оставались
    # две-три записи «разобрано, не заведено» на один файл, и человек не знал,
    # какую из них продолжать. Найдено живым прогоном 18 сентября.
    stale = session.scalars(
        sa.select(ImportBatch).where(
            ImportBatch.workspace_id == workspace.id,
            ImportBatch.file_name == preview.file_name,
            ImportBatch.status == "preview",
        )
    ).all()
    for old_batch in stale:
        session.execute(sa.delete(ImportRow).where(ImportRow.batch_id == old_batch.id))
        session.delete(old_batch)
    if stale:
        session.flush()

    counts = preview.counts
    batch = ImportBatch(
        workspace_id=workspace.id,
        file_name=preview.file_name,
        status="preview",
        mapping=preview.mapping,
        decisions={
            "date_order": preview.date_reading.order,
            "date_evidence": preview.date_reading.evidence,
            "header_line": preview.header_line,
            "unused_columns": preview.unused_columns,
            "accounts_missing": preview.accounts_missing,
            # Номер счёта из выписки и счёт, на который она легла: при заводке
            # номер записывается счёту, и следующая выписка найдёт его сама.
            "statement_account": {
                "number": (preview.bank or {}).get("account_number") or "",
                "account": (preview.bank or {}).get("account") or "",
            },
        },
        rows_total=counts["total"],
        created_by=actor,
    )
    session.add(batch)
    session.flush()

    known = _known_keys(session, workspace.id)
    duplicates = 0
    for row in preview.rows:
        if row.state == "imported" and row.values.get("external_key") in known:
            # Состояние меняем у самой строки, а не в местной переменной:
            # `preview.counts` считается по `row.state`, и от местной
            # переменной ответ предпросмотра ничего не узнавал - обещал
            # «готово 2050» там, где заведётся ноль.
            row.state = "duplicate"
            duplicates += 1
            row.problems.append(
                {"field": "", "text": "такая операция уже есть - повторная загрузка того же файла"}
            )
        session.add(
            ImportRow(
                batch_id=batch.id,
                line=row.line,
                state=row.state,
                raw=row.raw,
                parsed=row.values,
                problems=row.problems,
            )
        )
    batch.rows_duplicate = duplicates
    batch.rows_failed = counts["failed"]
    batch.rows_skipped = counts["skipped"]
    session.flush()
    return batch


def _known_keys(session: Session, workspace_id: uuid.UUID) -> set[str]:
    rows = session.scalars(
        sa.select(Operation.external_key).where(
            Operation.workspace_id == workspace_id,
            Operation.external_key.is_not(None),
            Operation.deleted_at.is_(None),
        )
    )
    return {value for value in rows if value}


def apply_batch(
    session: Session,
    workspace: Workspace,
    batch_id: uuid.UUID,
    *,
    actor: str = "",
    only_lines: Sequence[int] | None = None,
    create_dictionaries: bool = True,
) -> dict[str, Any]:
    """Завести операции по готовым строкам партии.

    Главное отличие от импортёров, которые мы разбирали: **строки с
    замечаниями не мешают остальным.** Плохая строка остаётся в партии со своим
    объяснением, хорошие становятся операциями. Файл из двухсот строк с одной
    испорченной заводит сто девяносто девять, а не ноль.
    """
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.workspace_id != workspace.id:
        raise FinanceError("Партия импорта не найдена")
    # Повторный вызов - законная работа, а не ошибка: человек поправил
    # отложенную строку и заводит только её. Уже заведённые строки пропускаются
    # по `operation_id`, поэтому дублей это не создаёт.

    accounts = {account.normalized_name: account for account in list_accounts(session, workspace.id)}
    rows = list(
        session.scalars(
            sa.select(ImportRow).where(ImportRow.batch_id == batch.id).order_by(ImportRow.line)
        )
    )
    known = _known_keys(session, workspace.id)
    remembered = _remember_statement_number(session, workspace, batch, accounts)
    by_bin = {
        str((item.details or {}).get("bin")): item
        for item in list_counterparties(session, workspace.id)
        if (item.details or {}).get("bin")
    }

    imported = 0
    for row in rows:
        if only_lines is not None and row.line not in set(only_lines):
            continue
        if row.state != "imported" or row.operation_id is not None:
            continue
        values = dict(row.parsed or {})
        key = values.get("external_key")
        if key and key in known:
            row.state = "duplicate"
            continue

        kind = values.get("kind")
        try:
            paid_at = date.fromisoformat(values["paid_at"])
            amount = Decimal(str(values["amount"]))
        except (KeyError, TypeError, ValueError):
            row.state = "failed"
            row.problems = list(row.problems or []) + [
                {"field": "", "text": "строка не разобрана до конца - нет даты или суммы"}
            ]
            continue

        account_from = accounts.get(norm(values.get("account_from") or "")) if values.get("account_from") else None
        account_to = accounts.get(norm(values.get("account_to") or "")) if values.get("account_to") else None

        side = "income" if kind == "income" else "expense"
        category = None
        if values.get("category") and kind != "transfer":
            category = ensure_category(
                session, workspace.id, side, values["category"], create=create_dictionaries
            )
        counterparty = None
        party_bin = str(values.get("counterparty_bin") or "")
        if party_bin and party_bin in by_bin:
            # БИН - тот же контрагент, как бы банк ни напечатал имя: «ТОО
            # "Альфа"» в одной выписке и «Альфа ТОО» в другой.
            counterparty = by_bin[party_bin]
        elif values.get("counterparty"):
            role = "client" if kind == "income" else "supplier"
            counterparty = ensure_counterparty(
                session, workspace.id, values["counterparty"], role=role, create=create_dictionaries
            )
            if counterparty is not None and party_bin and not (counterparty.details or {}).get("bin"):
                counterparty.details = {**(counterparty.details or {}), "bin": party_bin}
                by_bin[party_bin] = counterparty
        projects: list[tuple[uuid.UUID, Decimal]] = []
        if values.get("project"):
            project = ensure_project(session, workspace.id, values["project"], create=create_dictionaries)
            if project is not None:
                projects.append((project.id, amount))
        tags: list[uuid.UUID] = []
        for tag_name in values.get("tags") or []:
            tag = ensure_tag(session, workspace.id, tag_name, create=create_dictionaries)
            if tag is not None:
                tags.append(tag.id)

        accrued = values.get("accrued_at")
        data = OperationInput(
            kind=kind,
            status="fact",
            paid_at=paid_at,
            accrued_at=date.fromisoformat(accrued) if accrued else None,
            period_start=date.fromisoformat(values["period_start"]) if values.get("period_start") else None,
            period_end=date.fromisoformat(values["period_end"]) if values.get("period_end") else None,
            amount=amount,
            currency=values.get("currency") or workspace.base_currency,
            account_from_id=account_from.id if account_from else None,
            account_to_id=account_to.id if account_to else None,
            category_id=category.id if category else None,
            counterparty_id=counterparty.id if counterparty else None,
            comment=values.get("comment") or "",
            projects=projects,
            tags=tags,
            source="import",
            external_key=key,
            raw=row.raw,
            import_batch_id=batch.id,
        )
        try:
            operation = create_operation(session, workspace, data, actor=actor)
        except FinanceError as exc:
            row.state = "failed"
            row.problems = list(row.problems or []) + [{"field": "", "text": str(exc)}]
            continue
        row.operation_id = operation.id
        imported += 1
        if key:
            known.add(key)

    batch.rows_imported = int(
        session.scalar(
            sa.select(sa.func.count(ImportRow.id)).where(
                ImportRow.batch_id == batch.id, ImportRow.operation_id.is_not(None)
            )
        )
        or 0
    )
    batch.rows_failed = int(
        session.scalar(
            sa.select(sa.func.count(ImportRow.id)).where(
                ImportRow.batch_id == batch.id, ImportRow.state == "failed"
            )
        )
        or 0
    )
    batch.status = "applied"
    batch.applied_at = datetime.now(timezone.utc)
    session.flush()
    return {
        "imported": imported,
        "failed": batch.rows_failed,
        "skipped": batch.rows_skipped,
        "duplicate": batch.rows_duplicate,
        "total": batch.rows_total,
        "remembered": remembered,
    }


def _remember_statement_number(
    session: Session, workspace: Workspace, batch: ImportBatch, accounts: dict[str, Account]
) -> dict[str, str] | None:
    """Записать счёту номер из выписки, которую на него заводят.

    Только если у счёта номера ещё нет и этот номер не записан у другого счёта:
    чужой номер молча не перетирается - человек мог выбрать счёт по ошибке, и
    тогда следующая выписка тихо легла бы не туда. Возвращает, что записано.
    """
    statement = (batch.decisions or {}).get("statement_account") or {}
    key = banks.account_key(statement.get("number"))
    account = accounts.get(norm(statement.get("account") or ""))
    if not key or account is None or account.number:
        return None
    if any(other.number == key for other in accounts.values()):
        return None
    account.number = key
    session.flush()
    return {"account": account.name, "number": key, "account_id": str(account.id)}


def fix_import_row(
    session: Session,
    workspace: Workspace,
    batch_id: uuid.UUID,
    line: int,
    patch: dict[str, Any],
) -> ImportRow:
    """Поправить отложенную строку партии, не перезагружая файл.

    Разобранные значения строки правятся точечно; после правки строка
    пересчитывается на «чего не хватает» и, если хватает всего, помечается
    готовой к заводке. Это и есть ответ на «файл отвергнут целиком»: работа
    идёт со строкой, а не с файлом.
    """
    row = session.scalar(
        sa.select(ImportRow).where(ImportRow.batch_id == batch_id, ImportRow.line == line)
    )
    if row is None:
        raise FinanceError("Строка не найдена")
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.workspace_id != workspace.id:
        raise FinanceError("Партия импорта не найдена")

    values = dict(row.parsed or {})
    values.update({key: value for key, value in patch.items() if key in _FIXABLE})
    problems: list[dict[str, str]] = []

    if not values.get("paid_at"):
        problems.append({"field": "paid_at", "text": "нет даты платежа"})
    if not values.get("amount"):
        problems.append({"field": "amount", "text": "нет суммы"})
    kind = values.get("kind")
    if kind not in ("income", "expense", "transfer"):
        problems.append({"field": "kind", "text": "не указан вид операции"})
    accounts = {account.normalized_name for account in list_accounts(session, workspace.id)}
    for key, needed_for in (("account_from", ("expense", "transfer")), ("account_to", ("income", "transfer"))):
        name = values.get(key)
        if name and norm(name) not in accounts:
            problems.append({"field": key, "text": f"счёт «{name}» не найден"})
        elif not name and kind in needed_for:
            problems.append({"field": key, "text": "счёт не указан"})

    row.parsed = values
    row.problems = problems
    row.state = "failed" if problems else "imported"
    session.flush()
    return row


#: Поля строки импорта, которые можно поправить руками. Список закрытый: через
#: правку строки нельзя подменить отпечаток и завести дубль в обход проверки.
_FIXABLE = frozenset(
    {
        "paid_at", "accrued_at", "period_start", "period_end", "amount", "kind", "currency",
        "account_from", "account_to", "category", "subcategory", "counterparty", "project",
        "subproject", "comment", "tags",
    }
)


__all__ = [
    "DEFAULT_SLUG",
    "create_workspace",
    "get_workspace",
    "rename_workspace",
    "FinanceError",
    "OperationFilter",
    "OperationInput",
    "SEED_ACCOUNTS",
    "SEED_CATEGORIES",
    "VersionConflict",
    "account_net_before",
    "apply_batch",
    "archive",
    "check_date",
    "check_money",
    "create_account",
    "create_operation",
    "delete_operation",
    "ensure_category",
    "ensure_counterparty",
    "ensure_project",
    "ensure_tag",
    "ensure_workspace",
    "fix_import_row",
    "list_accounts",
    "list_categories",
    "list_counterparties",
    "list_operations",
    "list_projects",
    "list_tags",
    "operation_projects",
    "operation_sums",
    "reconcile_statement",
    "set_starting_balance",
    "operation_tags",
    "save_preview",
    "update_operation",
]
