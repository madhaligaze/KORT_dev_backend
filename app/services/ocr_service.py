from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import fitz
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential

from app.core.config import settings
from app.schemas.statement import (
    OCRReviewField,
    OCRReviewPayload,
    OCRReviewTable,
    ParsedStatement,
    StatementMetadata,
    StatementTotals,
    StatementTransaction,
)

DATE_PATTERN = re.compile(r"^\d{2}\.\d{2}\.\d{2}$")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
GENERIC_HEADER_TOKENS = {
    "date": {"date", "дата"},
    "income": {"income", "credit", "inflow", "приход", "поступление", "зачисление"},
    "expense": {"expense", "debit", "outflow", "расход", "списание"},
    "amount": {"amount", "сумма"},
    "detail": {"description", "detail", "details", "описание", "детали", "назначение"},
    "operation": {"operation", "type", "операция", "тип"},
}
KNOWN_OPERATIONS = ("Пополнение", "Перевод", "Покупка", "Снятие", "Разное")


class OCRProcessingError(ValueError):
    pass


def parse_ocr_statement(filename: str, content: bytes) -> ParsedStatement:
    analyze_result = analyze_layout_document(filename, content)
    return build_statement_from_layout(filename, analyze_result)


def extract_ocr_review(filename: str, content: bytes) -> OCRReviewPayload:
    analyze_result = analyze_layout_document(filename, content)
    lines = _extract_lines(analyze_result)
    tables = _table_matrices(analyze_result)
    suggested_table_index = 0 if tables else None
    suggested_header_row_index = (
        _find_header_row(tables[suggested_table_index])
        if suggested_table_index is not None
        else None
    )
    return OCRReviewPayload(
        review_id="",
        source_filename=filename,
        lines=lines[:80],
        tables=[
            OCRReviewTable(
                table_index=index,
                rows=table,
                suggested_header_row_index=_find_header_row(table),
                cell_confidence=[
                    [0.72 if cell else None for cell in row]
                    for row in table
                ],
            )
            for index, table in enumerate(tables[:4])
        ],
        suggested_table_index=suggested_table_index,
        suggested_header_row_index=suggested_header_row_index,
        available_fields=[
            OCRReviewField(key="date", label="Date", required=True),
            OCRReviewField(key="detail", label="Detail / Counterparty", required=True),
            OCRReviewField(key="operation", label="Operation"),
            OCRReviewField(key="amount", label="Net Amount"),
            OCRReviewField(key="income", label="Income"),
            OCRReviewField(key="expense", label="Expense"),
        ],
    )


def build_statement_from_review_mapping(
    filename: str,
    tables: list[list[list[str]]],
    *,
    table_index: int,
    header_row_index: int,
    column_mapping: dict[str, int | None],
    title: str | None = None,
) -> ParsedStatement:
    if table_index < 0 or table_index >= len(tables):
        raise OCRProcessingError("Selected OCR table was not found.")

    table = tables[table_index]
    if header_row_index < 0 or header_row_index >= len(table):
        raise OCRProcessingError("Selected header row is outside the OCR table range.")

    normalized_mapping = {
        key: value
        for key, value in column_mapping.items()
        if value is not None and value >= 0
    }
    if "date" not in normalized_mapping:
        raise OCRProcessingError("Date column is required for OCR review materialization.")
    if "amount" not in normalized_mapping and not (
        "income" in normalized_mapping and "expense" in normalized_mapping
    ):
        raise OCRProcessingError("Map either Net Amount or both Income and Expense columns.")
    if "detail" not in normalized_mapping:
        raise OCRProcessingError("Detail / Counterparty column is required.")

    transactions = _extract_generic_transactions(table[header_row_index + 1 :], normalized_mapping)
    if not transactions:
        raise OCRProcessingError("No transactions could be extracted from the selected OCR table.")

    document_title = title or next((row[0] for row in table if row and row[0]), filename)
    return ParsedStatement(
        metadata=StatementMetadata(
            source_filename=filename,
            title=document_title,
            parser_key="ocr_scanned_statement",
            transaction_count=len(transactions),
            totals=_calculate_totals(transactions),
        ),
        transactions=transactions,
    )


def analyze_layout_document(filename: str, content: bytes):
    if settings.azure_document_intelligence_endpoint and settings.azure_document_intelligence_key:
        client = DocumentIntelligenceClient(
            endpoint=settings.azure_document_intelligence_endpoint,
            credential=AzureKeyCredential(settings.azure_document_intelligence_key),
        )
        extension = Path(filename).suffix.lower()
        content_type = (
            "image/png"
            if extension == ".png"
            else "image/jpeg"
            if extension in {".jpg", ".jpeg"}
            else "application/pdf"
        )
        poller = client.begin_analyze_document(
            "prebuilt-layout",
            body=BytesIO(content),
            content_type=content_type,
        )
        return poller.result()

    return _analyze_with_rapidocr(filename, content)


def build_statement_from_layout(filename: str, analyze_result: Any) -> ParsedStatement:
    for table in _table_matrices(analyze_result):
        statement = _parse_table_statement(filename, table)
        if statement is not None:
            return statement

    lines = _extract_lines(analyze_result)
    statement = _parse_kaspi_like_lines(filename, lines)
    if statement is not None:
        return statement

    raise OCRProcessingError(
        "OCR extracted the document, but no supported statement structure was recognized."
    )


def _table_matrices(analyze_result: Any) -> list[list[list[str]]]:
    matrices: list[list[list[str]]] = []
    for table in getattr(analyze_result, "tables", []) or []:
        row_count = getattr(table, "row_count", 0) or 0
        column_count = getattr(table, "column_count", 0) or 0
        if row_count <= 0 or column_count <= 0:
            continue
        matrix = [["" for _ in range(column_count)] for _ in range(row_count)]
        for cell in getattr(table, "cells", []) or []:
            row_index = getattr(cell, "row_index", None)
            column_index = getattr(cell, "column_index", None)
            if row_index is None or column_index is None:
                continue
            if row_index >= row_count or column_index >= column_count:
                continue
            matrix[row_index][column_index] = _normalize(getattr(cell, "content", "") or "")
        matrices.append(matrix)
    return matrices


def _analyze_with_rapidocr(filename: str, content: bytes):
    try:
        from rapidocr import RapidOCR
    except ImportError as exc:
        raise OCRProcessingError(
            "OCR parser requires Azure Document Intelligence or local RapidOCR runtime."
        ) from exc

    extension = Path(filename).suffix.lower()
    ocr = RapidOCR()
    page_layouts = []

    if extension == ".pdf":
        document = fitz.open(stream=content, filetype="pdf")
        for page in document:
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            page_layouts.append(_rapidocr_output_to_layout(ocr(pixmap.tobytes("png"))))
    else:
        page_layouts.append(_rapidocr_output_to_layout(ocr(content)))

    tables = [table for layout in page_layouts for table in layout["tables"]]
    pages = [
        SimpleNamespace(lines=[SimpleNamespace(content=line) for line in layout["lines"]])
        for layout in page_layouts
    ]
    return SimpleNamespace(tables=tables, pages=pages)


def _rapidocr_output_to_layout(output: Any) -> dict[str, list[Any]]:
    raw_boxes = getattr(output, "boxes", None)
    raw_texts = getattr(output, "txts", None)
    boxes = list(raw_boxes) if raw_boxes is not None else []
    texts = list(raw_texts) if raw_texts is not None else []
    entries = []
    for box, text in zip(boxes, texts):
        normalized_text = _normalize(text or "")
        if not normalized_text:
            continue
        x_values = [point[0] for point in box]
        y_values = [point[1] for point in box]
        entries.append(
            {
                "text": normalized_text,
                "x": min(x_values),
                "y": min(y_values),
                "height": max(y_values) - min(y_values),
            }
        )

    entries.sort(key=lambda item: (item["y"], item["x"]))
    line_groups: list[list[dict[str, float | str]]] = []
    for entry in entries:
        if not line_groups:
            line_groups.append([entry])
            continue
        current_group = line_groups[-1]
        group_y = sum(item["y"] for item in current_group) / len(current_group)
        tolerance = max(
            max(float(item["height"]) for item in current_group) * 0.7,
            float(entry["height"]) * 0.7,
            8.0,
        )
        if abs(float(entry["y"]) - group_y) <= tolerance:
            current_group.append(entry)
        else:
            line_groups.append([entry])

    normalized_groups = [sorted(group, key=lambda item: item["x"]) for group in line_groups]
    lines = [" ".join(str(item["text"]) for item in group) for group in normalized_groups]
    table = _line_groups_to_table(normalized_groups)
    tables = [_table_to_namespace(table)] if table else []
    return {"lines": lines, "tables": tables}


def _line_groups_to_table(line_groups: list[list[dict[str, float | str]]]) -> list[list[str]]:
    if not line_groups:
        return []
    anchor_positions: list[float] = []
    rows: list[list[str]] = []
    for group in line_groups:
        row: list[str] = []
        for item in group:
            x = float(item["x"])
            text = str(item["text"])
            matched_index = next(
                (index for index, anchor in enumerate(anchor_positions) if abs(anchor - x) <= 28),
                None,
            )
            if matched_index is None:
                anchor_positions.append(x)
                combined = sorted(zip(anchor_positions, range(len(anchor_positions))))
                order = [index for _, index in combined]
                anchor_positions[:] = [anchor_positions[index] for index in order]
                matched_index = anchor_positions.index(x)
                for existing in rows:
                    while len(existing) < len(anchor_positions):
                        existing.append("")
            while len(row) < len(anchor_positions):
                row.append("")
            if row[matched_index]:
                row[matched_index] = f"{row[matched_index]} {text}".strip()
            else:
                row[matched_index] = text
        if any(cell for cell in row):
            rows.append(row)
    return rows


def _table_to_namespace(table: list[list[str]]):
    row_count = len(table)
    column_count = max((len(row) for row in table), default=0)
    cells = []
    for row_index, row in enumerate(table):
        for column_index, content in enumerate(row):
            if content:
                cells.append(
                    SimpleNamespace(
                        row_index=row_index,
                        column_index=column_index,
                        content=content,
                    )
                )
    return SimpleNamespace(row_count=row_count, column_count=column_count, cells=cells)


def _parse_table_statement(filename: str, table: list[list[str]]) -> ParsedStatement | None:
    header_index = _find_header_row(table)
    if header_index is None:
        return None

    column_map = _build_column_map(table[header_index])
    transactions = _extract_generic_transactions(table[header_index + 1 :], column_map)
    if not transactions:
        return None

    title = next((row[0] for row in table if row and row[0]), filename)
    return ParsedStatement(
        metadata=StatementMetadata(
            source_filename=filename,
            title=title,
            parser_key="ocr_scanned_statement",
            transaction_count=len(transactions),
            totals=_calculate_totals(transactions),
        ),
        transactions=transactions,
    )


def _find_header_row(rows: list[list[str]]) -> int | None:
    for index, row in enumerate(rows):
        normalized = [_normalize(cell).lower() for cell in row]
        if "date" in normalized or "дата" in normalized:
            if any(token in normalized for token in GENERIC_HEADER_TOKENS["detail"] | GENERIC_HEADER_TOKENS["amount"] | GENERIC_HEADER_TOKENS["income"] | GENERIC_HEADER_TOKENS["expense"]):
                return index
    return None


def _build_column_map(row: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(row):
        value = _normalize(cell).lower()
        for key, tokens in GENERIC_HEADER_TOKENS.items():
            if value in tokens:
                mapping[key] = index
                break
    return mapping


def _extract_generic_transactions(
    rows: list[list[str]],
    column_map: dict[str, int],
) -> list[StatementTransaction]:
    if "date" not in column_map:
        return []

    transactions: list[StatementTransaction] = []
    for row in rows:
        date_value = _cell_value(row, column_map.get("date"))
        if not date_value:
            continue
        date = _coerce_date(date_value)
        if date is None:
            continue

        income = _coerce_numeric(_cell_value(row, column_map.get("income")))
        expense = _coerce_numeric(_cell_value(row, column_map.get("expense")))
        amount = _coerce_numeric(_cell_value(row, column_map.get("amount")))
        if amount is not None and income is None and expense is None:
            if amount >= 0:
                income = amount
            else:
                expense = abs(amount)

        if income is None and expense is None:
            continue

        operation = _normalize(_cell_value(row, column_map.get("operation")) or "Operation")
        detail = _normalize(_cell_value(row, column_map.get("detail")) or operation)
        signed_amount = income if income is not None else -(expense or 0.0)

        transactions.append(
            StatementTransaction(
                date=date,
                amount=round(signed_amount, 2),
                income=round(income, 2) if income is not None else None,
                expense=round(expense, 2) if expense is not None else None,
                operation=operation,
                detail=detail,
                details_operation=f"{detail} {operation}".strip(),
                direction="inflow" if income is not None else "outflow",
                source="ocr",
                source_confidence=0.72,
            )
        )

    return transactions


def _extract_lines(analyze_result: Any) -> list[str]:
    lines: list[str] = []
    for page in getattr(analyze_result, "pages", []) or []:
        for line in getattr(page, "lines", []) or []:
            content = _normalize(getattr(line, "content", "") or "")
            if content:
                lines.append(content)
    return lines


def _parse_kaspi_like_lines(filename: str, lines: list[str]) -> ParsedStatement | None:
    full_text = "\n".join(lines)
    if "Kaspi Gold" not in full_text or "ВЫПИСКА" not in full_text:
        return None

    transactions = _extract_kaspi_transactions(lines)
    if not transactions:
        return None

    title = next((line for line in lines if "ВЫПИСКА" in line), filename)
    period_match = re.search(r"с\s+(\d{2}\.\d{2}\.\d{2})\s+по\s+(\d{2}\.\d{2}\.\d{2})", title)
    currency = _label_value(lines, "Валюта счета:")
    opening_balance = _parse_amount_safe(_value_after_balance_label(lines, first=True))
    closing_balance = _parse_amount_safe(_value_after_balance_label(lines, first=False))

    return ParsedStatement(
        metadata=StatementMetadata(
            source_filename=filename,
            title=title,
            parser_key="ocr_scanned_statement",
            account_holder=_label_value(lines, "Клиент:"),
            card_number=_label_value(lines, "Номер карты:"),
            account_number=_label_value(lines, "Номер счета:"),
            currency=currency,
            period_start=period_match.group(1) if period_match else None,
            period_end=period_match.group(2) if period_match else None,
            opening_balance=opening_balance,
            closing_balance=closing_balance,
            transaction_count=len(transactions),
            totals=_calculate_totals(transactions),
        ),
        transactions=transactions,
    )


def _extract_kaspi_transactions(lines: list[str]) -> list[StatementTransaction]:
    try:
        header_index = lines.index("Дата")
    except ValueError:
        return []

    body = lines[header_index + 1 :]
    transactions: list[StatementTransaction] = []
    index = 0
    while index < len(body):
        if not DATE_PATTERN.match(body[index]):
            index += 1
            continue

        if index + 2 >= len(body):
            break

        date = body[index]
        amount_line = body[index + 1]
        operation_line = body[index + 2]
        amount = _parse_amount_safe(amount_line)
        if amount is None:
            index += 1
            continue

        operation, detail = _split_operation_detail(operation_line)
        note = None
        flags: list[str] = []
        next_index = index + 3
        while next_index < len(body) and not DATE_PATTERN.match(body[next_index]):
            candidate = body[next_index]
            if candidate.startswith("-") or candidate.startswith("•") or candidate.startswith("—"):
                note = candidate
                flags.append("requires_attention")
            next_index += 1

        transactions.append(
            StatementTransaction(
                date=date,
                amount=round(amount, 2),
                income=round(amount, 2) if amount >= 0 else None,
                expense=round(abs(amount), 2) if amount < 0 else None,
                operation=operation,
                detail=detail,
                details_operation=f"{detail}{operation}",
                direction="inflow" if amount >= 0 else "outflow",
                note=note,
                flags=flags,
                source="ocr",
                source_confidence=0.68,
            )
        )
        index = next_index

    return transactions


def _label_value(lines: list[str], label: str) -> str | None:
    for index, line in enumerate(lines):
        if line == label and index + 1 < len(lines):
            return lines[index + 1]
    return None


def _value_after_balance_label(lines: list[str], *, first: bool) -> str | None:
    matches = [
        index
        for index, line in enumerate(lines)
        if line.startswith("Доступно на ") and line.endswith(":") and index + 1 < len(lines)
    ]
    if not matches:
        return None
    selected = matches[0] if first else matches[-1]
    return lines[selected + 1]


def _split_operation_detail(value: str) -> tuple[str, str]:
    for operation in KNOWN_OPERATIONS:
        if value.startswith(operation):
            detail = _normalize(value[len(operation) :]) or operation
            return operation, detail
    return "Операция", _normalize(value)


def _cell_value(row: list[str], index: int | None) -> str | None:
    if index is None or index >= len(row):
        return None
    value = _normalize(row[index])
    return value or None


def _coerce_numeric(value: str | None) -> float | None:
    if not value:
        return None
    return _parse_amount_safe(value)


def _parse_amount_safe(value: str | None) -> float | None:
    if not value:
        return None
    normalized = _normalize(value).replace("₸", "").replace(" ", "")
    normalized = normalized.replace("−", "-")
    sign = -1 if normalized.startswith("-") else 1
    normalized = normalized.lstrip("+-").replace(",", ".")
    try:
        return round(float(Decimal(normalized)) * sign, 2)
    except InvalidOperation:
        return None


def _coerce_date(value: str) -> str | None:
    text = _normalize(value)
    if DATE_PATTERN.match(text):
        return text
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            parsed = datetime.strptime(text, pattern)
            return parsed.strftime("%d.%m.%y")
        except ValueError:
            continue
    return None


def _calculate_totals(transactions: list[StatementTransaction]) -> StatementTotals:
    totals = defaultdict(float)
    for transaction in transactions:
        if transaction.income is not None:
            totals["income_total"] += transaction.income
        if transaction.expense is not None:
            totals["expense_total"] += transaction.expense
        if transaction.operation == "Покупка" and transaction.expense is not None:
            totals["purchase_total"] += transaction.expense
        if transaction.operation == "Перевод" and transaction.expense is not None:
            totals["transfer_total"] += transaction.expense
        if transaction.operation == "Пополнение" and transaction.income is not None:
            totals["topup_total"] += transaction.income
        if transaction.operation == "Снятие" and transaction.expense is not None:
            totals["cash_withdrawal_total"] += transaction.expense
    return StatementTotals(**{key: round(value, 2) for key, value in totals.items()})


def _normalize(value: str) -> str:
    collapsed = value.replace("\xa0", " ").replace("−", "-")
    collapsed = unicodedata.normalize("NFC", collapsed)
    return re.sub(r"[ \t]+", " ", collapsed).strip()
