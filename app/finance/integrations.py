"""Подключения банков: выписка, книга Google, приём операций по адресу.

Почему тут нет «подключим Kaspi по API»
───────────────────────────────────────
Публичного API для малого бизнеса у банков Казахстана нет. Кнопка «подключить
Kaspi», которая на самом деле ничего не подключает, — это худшее, что можно
сделать с разделом учёта: человек считает, что операции приходят сами, и не
сверяет выписку. Поэтому у каждого банка честно написано, чем он подключается:

* `statement` — выписка файлом. Работает у всех банков без исключения, потому
  что выписку выдаёт каждый; у Kaspi и Halyk разбор свой, у остальных —
  обобщённый табличный и OCR для сканов.
* `sheets` — книга Google, которую ведут руками: читается по расписанию тем же
  разбором, что и файл.
* `api` — наш адрес приёма. Сторону банка закрывает то, что клиент может
  прислать сам: скрипт, интеграционная шина, выгрузка из 1С. Токен выдаётся на
  подключение, операции приходят пачкой и попадают в тот же разбор.

Токен хранится **хешем**. Показывается он один раз, при создании: база учёта не
должна содержать ключи, которыми в неё же можно писать.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.models import Integration, Workspace
from app.finance.service import FinanceError

#: Что умеет каждый банк. Логотипы лежат в `public/finance/banks/`.
@dataclass(frozen=True)
class Bank:
    slug: str
    title: str
    logo: str
    #: Виды подключения по убыванию надёжности.
    ways: tuple[str, ...]
    #: Чем читается выписка этого банка.
    parser: str


BANKS: tuple[Bank, ...] = (
    Bank("kaspi", "Kaspi Bank", "kaspiBank.png", ("statement", "api"), "свой разбор Kaspi Gold и Business"),
    Bank("halyk", "Halyk Bank", "halyk-bank.svg", ("statement", "api"), "свой разбор выписки Halyk"),
    Bank("jusan", "Jusan Bank", "jusanBank.svg", ("statement", "api"), "обобщённый табличный разбор"),
    Bank("forte", "ForteBank", "fortebank.png", ("statement", "api"), "обобщённый табличный разбор"),
    Bank("bcc", "Банк ЦентрКредит", "centerCredit_logo.png", ("statement", "api"), "обобщённый табличный разбор"),
    Bank("freedom", "Freedom Bank", "Freedom.png", ("statement", "api"), "обобщённый табличный разбор"),
    Bank("rbk", "Bank RBK", "rbk-kz-bank.png", ("statement", "api"), "обобщённый табличный разбор"),
    Bank("eco", "EcoCenter Bank", "ecoCenterBank.jpeg", ("statement", "api"), "обобщённый табличный разбор"),
    Bank("home", "Home Credit Bank", "HomeCreditBank.png", ("statement", "api"), "обобщённый табличный разбор"),
    Bank("sheets", "Книга Google Sheets", "", ("sheets",), "тот же разбор, что и файл"),
    Bank("other", "Другой банк или система", "", ("statement", "api"), "обобщённый табличный разбор и OCR"),
)

BY_SLUG = {bank.slug: bank for bank in BANKS}


def catalog() -> list[dict[str, Any]]:
    """Справочник подключений для экрана."""
    return [
        {
            "slug": bank.slug,
            "title": bank.title,
            "logo": f"/finance/banks/{bank.logo}" if bank.logo else "",
            "ways": list(bank.ways),
            "parser": bank.parser,
        }
        for bank in BANKS
    ]


# ── Токен ────────────────────────────────────────────────────────────────────


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    """Токен приёма: 32 байта случайности, человеку показывается один раз."""
    return secrets.token_urlsafe(32)


# ── Подключения ─────────────────────────────────────────────────────────────


def list_integrations(session: Session, workspace_id: uuid.UUID) -> list[Integration]:
    return list(
        session.scalars(
            sa.select(Integration)
            .where(Integration.workspace_id == workspace_id)
            .order_by(Integration.created_at)
        )
    )


def create(
    session: Session,
    workspace: Workspace,
    *,
    slug: str,
    kind: str,
    title: str = "",
    account_id: uuid.UUID | None = None,
    settings: dict[str, Any] | None = None,
    actor: str = "",
) -> tuple[Integration, str]:
    """Создать подключение. Для приёма по адресу возвращается токен — один раз."""
    bank = BY_SLUG.get(slug)
    if bank is None:
        raise FinanceError(f"Неизвестное подключение «{slug}»")
    if kind not in bank.ways:
        raise FinanceError(f"{bank.title} так не подключается: {', '.join(bank.ways)}")

    token = new_token() if kind == "api" else ""
    integration = Integration(
        workspace_id=workspace.id,
        slug=slug,
        title=title.strip() or bank.title,
        kind=kind,
        state="active" if kind != "api" else "active",
        account_id=account_id,
        token_hash=_hash(token) if token else None,
        settings=settings or {},
        created_by=actor,
    )
    session.add(integration)
    session.flush()
    return integration, token


def rotate_token(session: Session, workspace_id: uuid.UUID, integration_id: uuid.UUID) -> str:
    """Сменить токен: старый перестаёт работать в тот же момент."""
    integration = session.get(Integration, integration_id)
    if integration is None or integration.workspace_id != workspace_id:
        raise FinanceError("Подключение не найдено")
    if integration.kind != "api":
        raise FinanceError("Токен есть только у приёма по адресу")
    token = new_token()
    integration.token_hash = _hash(token)
    session.flush()
    return token


def set_state(
    session: Session, workspace_id: uuid.UUID, integration_id: uuid.UUID, state: str
) -> Integration:
    integration = session.get(Integration, integration_id)
    if integration is None or integration.workspace_id != workspace_id:
        raise FinanceError("Подключение не найдено")
    if state not in ("active", "off"):
        raise FinanceError("Состояние бывает «active» или «off»")
    integration.state = state
    session.flush()
    return integration


def remove(session: Session, workspace_id: uuid.UUID, integration_id: uuid.UUID) -> None:
    integration = session.get(Integration, integration_id)
    if integration is None or integration.workspace_id != workspace_id:
        raise FinanceError("Подключение не найдено")
    session.delete(integration)
    session.flush()


def resolve_token(session: Session, token: str) -> Integration:
    """Найти подключение по токену.

    Ищем по хешу, а не перебором с расшифровкой: расшифровать нечего, и это
    правильно. Выключенное подключение отвечает отказом, а не тишиной — иначе
    отправляющая сторона считала бы, что операции доходят.
    """
    if not token or len(token) < 20:
        raise FinanceError("Токен не похож на токен")
    integration = session.scalar(
        sa.select(Integration).where(Integration.token_hash == _hash(token))
    )
    if integration is None:
        raise FinanceError("Подключение по этому токену не найдено")
    if integration.state != "active":
        raise FinanceError("Подключение выключено")
    return integration


# ── Приём операций ──────────────────────────────────────────────────────────


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, AttributeError, TypeError) as exc:
        raise FinanceError(f"Сумма «{value}» не похожа на число") from exc


def _day(value: Any) -> date:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise FinanceError(f"Дату «{value}» прочитать нельзя: нужен вид ГГГГ-ММ-ДД")


def rows_of(payload: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Привести присланные операции к строкам разбора.

    Форма нарочно простая: дата, сумма, комментарий, необязательные счёт,
    статья, контрагент и свой ключ. Требовать от чужого скрипта нашу модель
    целиком значило бы, что интеграцию никто не напишет.

    Знак суммы задаёт вид: минус — расход. Это то же правило, что в выписке, и
    другого быть не должно — иначе один и тот же файл, присланный по адресу и
    загруженный руками, дал бы разные цифры.
    """
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise FinanceError(f"Операция {index}: ожидался объект")
        amount = _money(item.get("amount"))
        if amount == 0:
            raise FinanceError(f"Операция {index}: нулевая сумма")
        kind = str(item.get("kind") or ("expense" if amount < 0 else "income"))
        if kind not in ("income", "expense", "transfer"):
            raise FinanceError(f"Операция {index}: вид «{kind}» неизвестен")
        rows.append(
            {
                "paid_at": _day(item.get("paid_at") or item.get("date")),
                "amount": abs(amount),
                "kind": kind,
                "comment": str(item.get("comment") or "")[:2000],
                "account": (item.get("account") or "").strip() or None,
                "category": (item.get("category") or "").strip() or None,
                "counterparty": (item.get("counterparty") or "").strip() or None,
                "external_key": (item.get("id") or item.get("external_key") or "").strip() or None,
            }
        )
    return rows


def mark_received(session: Session, integration: Integration, count: int) -> None:
    integration.received = int(integration.received or 0) + count
    integration.last_seen_at = datetime.now(timezone.utc)
    session.flush()


def to_dict(integration: Integration, *, account_name: str = "") -> dict[str, Any]:
    bank = BY_SLUG.get(integration.slug)
    return {
        "id": str(integration.id),
        "slug": integration.slug,
        "title": integration.title,
        "kind": integration.kind,
        "state": integration.state,
        "logo": f"/finance/banks/{bank.logo}" if bank and bank.logo else "",
        "account": account_name,
        "account_id": str(integration.account_id) if integration.account_id else None,
        "settings": integration.settings or {},
        "received": integration.received,
        "last_seen_at": integration.last_seen_at.isoformat() if integration.last_seen_at else None,
        "has_token": bool(integration.token_hash),
    }


__all__ = [
    "BANKS",
    "BY_SLUG",
    "Bank",
    "catalog",
    "create",
    "list_integrations",
    "mark_received",
    "new_token",
    "remove",
    "resolve_token",
    "rotate_token",
    "rows_of",
    "set_state",
    "to_dict",
]
