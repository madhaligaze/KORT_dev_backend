"""Банковская выписка PDF → строки импорта «Финансов».

Почему берём готовый разбор, а не пишем свой
────────────────────────────────────────────
Разбор выписок в этом продукте уже есть: раздел «Анализ выписок» читает Kaspi
Gold, Kaspi Business, Halyk, сканы через OCR и обобщённый табличный формат
(`app.services.document_service`). Проверено на живом файле 18 сентября 2026:
выписка Kaspi Gold на 47 страниц разобралась в 2050 операций с датами, суммами
и деталями.

Вторая копия этого разбора разъехалась бы с первой на первой же правке — как
уже случилось бы с привязкой колонок, если бы её не вынесли в общий модуль.
Поэтому здесь только перевод: операции разбора → строки предпросмотра импорта.

Что этот перевод добавляет от себя
──────────────────────────────────
**Счёт.** В выписке его нет как названия: файл сам по себе и есть счёт.
Поэтому счёт выбирает человек при загрузке, и он же попадает во все строки.
Без этого каждая строка легла бы с замечанием «не указано, куда пришли деньги»,
то есть файл на две тысячи строк отказался бы целиком — ровно то, за что мы
критикуем соседей по рынку.

**Направление.** У разбора есть `direction` (`inflow`/`outflow`) и знак суммы.
Берём направление, а сумму приводим к модулю: знак и вид операции
одновременно — два источника правды об одном.

**Комментарий.** Kaspi даёт пару «операция» + «детали»: «Покупка» и
«Magnum Cash&Carry». В комментарий идут обе части, потому что по ним потом
работают правила категоризации, а «Покупка» без магазина не говорит ничего.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)

# Какие файлы сюда уходят, решает `importing.analyze` по содержимому
# (`formats.sniff`): PDF — всегда, Excel — только если в нём нет таблицы с
# шапкой и файл узнаёт один из шаблонов выписок.

_SPACES = re.compile(r"[\s ]+")


class StatementError(RuntimeError):
    """Файл не разобрался ни одним из известных шаблонов выписок."""


def _parse_date(value: str) -> date | None:
    """Дата из выписки. Форматы разные у каждого банка, год бывает двузначным."""
    text = (value or "").strip()
    if not text:
        return None
    for pattern in ("%d.%m.%y", "%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    # «17.09.26 12:34» и прочие хвосты времени.
    head = text.split(" ")[0]
    if head != text:
        return _parse_date(head)
    return None


def _iso(value: str | None) -> str | None:
    parsed = _parse_date(value or "")
    return parsed.isoformat() if parsed else None


def _money_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


def read_statement(data: bytes, file_name: str, *, account: str | None) -> dict[str, Any]:
    """Разобрать выписку и вернуть строки в том же виде, что табличный импорт.

    Возвращает словарь с ключами `rows` (значения будущих операций),
    `parser_key` (каким шаблоном прочитали) и `account` (какой счёт проставлен).
    """
    from app.services.document_service import (  # локальный импорт: тяжёлый модуль
        DocumentParseError,
        parse_statement_with_diagnostics,
    )

    try:
        statement, matches = parse_statement_with_diagnostics(file_name, data)
    except DocumentParseError as exc:
        raise StatementError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — чужой разбор, причина бывает любой
        log.warning("finance: выписка «%s» не разобралась: %s", file_name, exc)
        raise StatementError(
            f"Не удалось прочитать выписку: {exc}. "
            "Если файл — скан, попробуйте выгрузить из банка PDF с текстом."
        ) from exc

    parser_key = getattr(statement.metadata, "parser_key", "") if statement.metadata else ""
    transactions = list(statement.transactions or [])
    if not transactions:
        raise StatementError(
            "Выписка прочиталась, но операций в ней не нашлось. "
            "Проверьте, что это выписка за период, а не справка об остатке."
        )

    rows: list[dict[str, Any]] = []
    for index, tx in enumerate(transactions):
        paid_at = _parse_date(tx.date)
        amount = Decimal(str(abs(tx.amount or 0)))
        kind = "income" if (tx.direction or "").lower() == "inflow" else "expense"
        # Часть выписок не заполняет direction — тогда решает знак суммы.
        if not tx.direction:
            kind = "expense" if (tx.amount or 0) < 0 else "income"

        detail = _SPACES.sub(" ", (tx.detail or "").strip())
        operation = _SPACES.sub(" ", (tx.operation or "").strip())
        comment = " · ".join(part for part in (operation, detail) if part)

        values: dict[str, Any] = {
            "paid_at": paid_at.isoformat() if paid_at else None,
            "accrued_at": None,
            "period_start": None,
            "period_end": None,
            "amount": str(amount) if amount else None,
            "kind": kind,
            "currency": (tx.currency_op or None),
            "account_from": account if kind == "expense" else None,
            "account_to": account if kind == "income" else None,
            "category": tx.category or None,
            "subcategory": None,
            # Контрагент из выписки берём осторожно: у Kaspi Gold в «деталях»
            # лежит и магазин, и человек, и назначение — это комментарий, а не
            # справочник. Тот, кто действительно назван контрагентом банком,
            # приходит в `raw_counterparty`.
            "counterparty": (tx.raw_counterparty or None),
            "project": None,
            "subproject": None,
            "comment": comment,
            "tags": [],
        }
        raw = {
            "Дата": tx.date,
            "Сумма": tx.amount,
            "Операция": operation,
            "Детали": detail,
        }
        if tx.document_number:
            raw["Документ"] = tx.document_number
        rows.append({"line": index + 2, "values": values, "raw": raw})

    meta = statement.metadata
    # Что напечатал сам банк: остатки на начало и конец периода. По ним
    # предпросмотр сверяет разобранные операции с банком, а начальный остаток
    # счёта ставится из выписки, а не набирается руками.
    bank = {
        "period_start": _iso(getattr(meta, "period_start", None)),
        "period_end": _iso(getattr(meta, "period_end", None)),
        "opening_balance": _money_text(getattr(meta, "opening_balance", None)),
        "closing_balance": _money_text(getattr(meta, "closing_balance", None)),
        "account_number": getattr(meta, "account_number", None) or "",
        "card_number": getattr(meta, "card_number", None) or "",
    }
    return {
        "bank": bank,
        "rows": rows,
        "parser_key": parser_key,
        "parsers_tried": [
            {"key": match.key, "label": match.label, "score": match.score} for match in matches
        ],
        "account": account,
        "count": len(rows),
    }


__all__ = ["StatementError", "read_statement"]
