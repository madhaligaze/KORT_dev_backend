from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

import fitz

from app.schemas.statement import (
    ParsedStatement,
    StatementMetadata,
    StatementTotals,
    StatementTransaction,
)

_DATE_FULL_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_PERIOD_RE = re.compile(r"с\s+(\d{2}\.\d{2}\.\d{4})\s+по\s+(\d{2}\.\d{2}\.\d{4})")
_BALANCE_AMOUNT_RE = re.compile(r"([\d\s\xa0]+,\d{2})\s*KZT")

_AUTOCONV_MARKER = "Автоконвертация"

# Column indices inside the Halyk transaction table
_COL_DATE_OP = 0
_COL_DATE_PROC = 1
_COL_DESCRIPTION = 2
_COL_AMOUNT_OP = 3   # Сумма операции в валюте операции (e.g. -11.01 USD)
_COL_CURRENCY = 4
_COL_INCOME = 5
_COL_EXPENSE = 6


def looks_like_halyk_personal(text: str) -> bool:
    """Выписка физлица Halyk: в шапке «ФИО:» и «ИИН:».

    У выписки ТОО тот же банк, но в шапке «Клиент» и «ИИН/БИН», а таблица
    другая — дебет, кредит, контрагент, детали платежа. Её читает общий
    разбор и показывает видом для юрлица.
    """
    return "Народный Банк Казахстана" in text and "Выписка по счету" in text and "ФИО" in text


def detect_halyk_fiz_statement(filename: str, content: bytes) -> float:
    if Path(filename).suffix.lower() != ".pdf":
        return 0.0
    try:
        doc = fitz.open(stream=content, filetype="pdf")
        sample = doc[0].get_text("text")
        doc.close()
    except Exception:
        return 0.0
    return 1.0 if looks_like_halyk_personal(sample) else 0.0


def parse_halyk_fiz_statement(filename: str, content: bytes) -> ParsedStatement:
    doc = fitz.open(stream=content, filetype="pdf")
    page0_text = doc[0].get_text("text")
    transactions = _extract_transactions(doc)
    doc.close()

    totals = _calculate_totals(transactions)
    metadata = _extract_metadata(filename, page0_text, transactions, totals)
    return ParsedStatement(metadata=metadata, transactions=transactions)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _extract_metadata(
    filename: str,
    page0_text: str,
    transactions: list[StatementTransaction],
    totals: StatementTotals,
) -> StatementMetadata:
    lines = [line.strip() for line in page0_text.splitlines() if line.strip()]

    account_holder = _find_after_label(lines, "ФИО:")
    account_number = _find_after_label(lines, "Номер счета KZT:")
    period_start = period_end = None
    period_match = _PERIOD_RE.search(page0_text)
    if period_match:
        period_start = _normalize_date(period_match.group(1))
        period_end = _normalize_date(period_match.group(2))

    opening_balance = _extract_kzt_balance(page0_text, "Входящий остаток:")
    closing_balance = _extract_kzt_balance(page0_text, "Исходящий остаток:")

    period_label = ""
    if period_start and period_end:
        period_label = f" за период с {period_start} по {period_end}"

    return StatementMetadata(
        source_filename=filename,
        title=f"ВЫПИСКА по счету Халык Банк{period_label}",
        parser_key="halyk_fiz_statement",
        account_holder=account_holder,
        account_number=account_number,
        currency="KZT",
        period_start=period_start,
        period_end=period_end,
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        transaction_count=len(transactions),
        totals=totals,
        holder_kind="personal",
    )


def _find_after_label(lines: list[str], label: str) -> str | None:
    for i, line in enumerate(lines):
        if label in line:
            # Value may be on same line after the label, or on next line
            after = line.split(label, 1)[1].strip()
            if after:
                return after
            if i + 1 < len(lines):
                return lines[i + 1]
    return None


def _extract_kzt_balance(text: str, label: str) -> float | None:
    idx = text.find(label)
    if idx == -1:
        return None
    snippet = text[idx:idx + 80]
    m = _BALANCE_AMOUNT_RE.search(snippet)
    if not m:
        return None
    return _parse_amount(m.group(1))


# ---------------------------------------------------------------------------
# Transaction extraction
# ---------------------------------------------------------------------------

def _extract_transactions(doc: fitz.Document) -> list[StatementTransaction]:
    transactions: list[StatementTransaction] = []
    for page in doc:
        transactions.extend(_extract_page_transactions(page))
    return transactions


def _extract_page_transactions(page: fitz.Page) -> list[StatementTransaction]:
    try:
        tables = page.find_tables()
        if tables.tables:
            return _parse_table_rows(tables)
    except Exception:
        pass
    return _parse_text_fallback(page)


def _parse_table_rows(tables) -> list[StatementTransaction]:
    transactions: list[StatementTransaction] = []
    for table in tables.tables:
        rows = table.extract()
        for row in rows:
            tx = _row_to_transaction(row)
            if tx is not None:
                transactions.append(tx)
    return transactions


def _row_to_transaction(row: list) -> StatementTransaction | None:
    if not row or len(row) < 7:
        return None
    date_str = _cell(row, _COL_DATE_OP)
    if not _DATE_FULL_RE.match(date_str):
        return None

    description = _cell(row, _COL_DESCRIPTION).replace("\n", " ").strip()

    income_raw = _parse_amount(_cell(row, _COL_INCOME))
    expense_raw = _parse_amount(_cell(row, _COL_EXPENSE))
    income_val = income_raw if income_raw > 0 else None
    expense_val = abs(expense_raw) if expense_raw < 0 else None
    amount = (income_val or 0.0) - (expense_val or 0.0)
    currency_op = _cell(row, _COL_CURRENCY) or None
    date_short = _normalize_date(date_str)
    processing_date = _normalize_date(_cell(row, _COL_DATE_PROC))

    if _AUTOCONV_MARKER in description:
        # Store the operation-currency amount in note for later KZT merging
        op_amount = _parse_amount(_cell(row, _COL_AMOUNT_OP))
        return StatementTransaction(
            date=date_short,
            amount=amount,
            income=income_val,
            expense=expense_val,
            operation="autoconv",
            detail=description,
            details_operation=description,
            direction="outflow" if expense_val else "inflow",
            currency_op=currency_op,
            processing_date=processing_date,
            note=f"fx:{op_amount}",
        )

    direction = "inflow" if income_val else "outflow"
    operation = _derive_operation(description)

    return StatementTransaction(
        date=date_short,
        amount=amount,
        income=income_val,
        expense=expense_val,
        operation=operation,
        detail=description,
        details_operation=f"{description} / {operation}" if operation else description,
        direction=direction,
        currency_op=currency_op,
        processing_date=processing_date,
        comment=f"Дата тр: {date_short}",
    )


# ---------------------------------------------------------------------------
# Text-based fallback parser
# ---------------------------------------------------------------------------

def _parse_text_fallback(page: fitz.Page) -> list[StatementTransaction]:
    """Word-position-based reconstruction for pages where find_tables() fails."""
    words = page.get_text("words")
    if not words:
        return []

    # Group words into approximate rows by y0 coordinate (tolerance ±4pt)
    row_map: dict[int, list[tuple[float, str]]] = {}
    for x0, y0, _x1, _y1, word, *_ in words:
        bucket = round(y0 / 4) * 4
        row_map.setdefault(bucket, []).append((x0, word))

    # Rebuild each row as ordered words
    text_rows = []
    for _y in sorted(row_map):
        ordered = sorted(row_map[_y], key=lambda t: t[0])
        text_rows.append(" ".join(w for _, w in ordered))

    transactions: list[StatementTransaction] = []
    current: list[str] = []

    for line in text_rows:
        first_token = line.split()[0] if line.split() else ""
        if _DATE_FULL_RE.match(first_token):
            if current:
                tx = _parse_text_row(current)
                if tx:
                    transactions.append(tx)
            current = [line]
        elif current:
            current.append(line)

    if current:
        tx = _parse_text_row(current)
        if tx:
            transactions.append(tx)

    return transactions


def _parse_text_row(chunks: list[str]) -> StatementTransaction | None:
    combined = " ".join(chunks)
    tokens = combined.split()
    if len(tokens) < 9:
        return None

    date_str = tokens[0]
    if not _DATE_FULL_RE.match(date_str):
        return None

    # Find currency token (KZT/USD/EUR) to locate amount boundaries
    currency_idx = next(
        (i for i, t in enumerate(tokens) if t in ("KZT", "USD", "EUR")),
        None,
    )
    if currency_idx is None:
        return None

    # Tokens after currency: income, expense, commission, card
    tail = tokens[currency_idx + 1:]
    if len(tail) < 2:
        return None

    income_raw = _parse_amount(tail[0])
    expense_raw = _parse_amount(tail[1])
    income_val = income_raw if income_raw > 0 else None
    expense_val = abs(expense_raw) if expense_raw < 0 else None

    # Description is between date+processing_date (tokens[1]) and the amount token
    amount_token = tokens[currency_idx - 1]
    amount_start = 2  # skip date_op and date_proc
    amount_end = tokens.index(amount_token) if amount_token in tokens else currency_idx - 1
    description = " ".join(tokens[amount_start:amount_end])

    amount = (income_val or 0.0) - (expense_val or 0.0)
    date_short = _normalize_date(date_str)
    processing_date = _normalize_date(tokens[1]) if len(tokens) > 1 and _DATE_FULL_RE.match(tokens[1]) else None
    currency_op = tokens[currency_idx]

    if _AUTOCONV_MARKER in description:
        op_amount = _parse_amount(amount_token)  # tokens[currency_idx - 1] is the op amount
        return StatementTransaction(
            date=date_short,
            amount=amount,
            income=income_val,
            expense=expense_val,
            operation="autoconv",
            detail=description,
            details_operation=description,
            direction="outflow" if expense_val else "inflow",
            currency_op=currency_op,
            processing_date=processing_date,
            note=f"fx:{op_amount}",
        )

    direction = "inflow" if income_val else "outflow"
    operation = _derive_operation(description)

    return StatementTransaction(
        date=date_short,
        amount=amount,
        income=income_val,
        expense=expense_val,
        operation=operation,
        detail=description,
        details_operation=f"{description} / {operation}" if operation else description,
        direction=direction,
        currency_op=currency_op,
        processing_date=processing_date,
        comment=f"Дата тр: {date_short}",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cell(row: list, idx: int) -> str:
    if idx >= len(row):
        return ""
    val = row[idx]
    return (val or "").strip()


def _parse_amount(s: str) -> float:
    if not s:
        return 0.0
    cleaned = re.sub(r"[\s\xa0\u202f]", "", s).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _normalize_date(date_str: str) -> str:
    """Convert DD.MM.YYYY to DD.MM.YY for consistency with other parsers."""
    parts = date_str.split(".")
    if len(parts) == 3 and len(parts[2]) == 4:
        return f"{parts[0]}.{parts[1]}.{parts[2][2:]}"
    return date_str


def _derive_operation(description: str) -> str:
    d = description.lower()
    if "поступление" in d:
        return "Поступление"
    if "оплаты у коммерсанта" in d or "оплата" in d:
        return "Покупка"
    if "перевод" in d:
        return "Перевод"
    if "автоконвертация" in d:
        return "Конвертация"
    return "Разное"


def _calculate_totals(transactions: list[StatementTransaction]) -> StatementTotals:
    income_total = sum(t.income or 0.0 for t in transactions)
    expense_total = sum(t.expense or 0.0 for t in transactions)
    transfer_total = sum(
        (t.expense or 0.0) for t in transactions if t.operation == "Перевод"
    )
    topup_total = sum(
        (t.income or 0.0) for t in transactions if t.operation == "Поступление"
    )
    purchase_total = sum(
        (t.expense or 0.0) for t in transactions if t.operation == "Покупка"
    )
    return StatementTotals(
        income_total=round(income_total, 2),
        expense_total=round(expense_total, 2),
        purchase_total=round(purchase_total, 2),
        transfer_total=round(transfer_total, 2),
        topup_total=round(topup_total, 2),
    )
