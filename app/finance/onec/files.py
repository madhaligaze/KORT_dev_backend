"""1С без интернета: журналы документов и контрагенты файлами.

У компании, чья 1С стоит на компьютере бухгалтера, REST-сервиса нет: KORT
видит её только отчётами, которые бухгалтер выгрузил в Excel. Выгрузки во
всех компаниях одни и те же:

* журнал «Реализация товаров и услуг» (АВР) и журнал «Счета на оплату» -
  список документов: дата, номер, организация, контрагент, договор, сумма,
  содержание;
* справочник «Контрагенты» - наименование, полное наименование, ИИН/БИН;
* ОСВ по 1210 - сальдо на начало (`onec/osv.py`).

Документы ложатся в те же `OnecDocument`, что и у базы в интернете, - базе на
каждую организацию журнала: одна 1С ведёт несколько ТОО (полигон: «Тумар
Офис» и «Тумар Плюс»), а шаги «из 1С» (`core/onec_start.py`) идут по одному
юрлицу. Дальше база из файлов ничем не отличается: те же участники по БИН,
договоры по актам, сопоставление и факты журнала.

База из файлов - `OnecBase` с адресом `file:<организация>` (`is_files`): по
расписанию она не читается, новые документы приносит следующая выгрузка. Документ, которого в
новой выгрузке того же вида нет, хотя его дата внутри её дат, помечается
`gone` - как у базы в интернете.

Колонки - по названиям (форма «документ» приёмника), вид документа - по
словам названия файла, листа и строк над шапкой. Не узнан вид - журнал не
читается: счёт, принятый за акт, начислил бы месяц, которого не было.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.contracts.fields import party_key
from app.finance import packs
from app.finance.intake import forms, recognize, tables
from app.finance.intake.header import text as cell_text
from app.finance.intake.rows import has_value, is_empty, is_label_total, is_total
from app.finance.intake.values import decide_date_order, parse_date, parse_money
from app.finance.layout import norm
from app.finance.models import Workspace
from app.finance.onec import osv as osv_module
from app.finance.onec.models import OnecBase, OnecDocument
from app.finance.onec.sync import period_in
from app.finance.service import FinanceError

#: Вид журнала по словам его названия: первое совпадение решает.
KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("invoice", ("счета на оплату", "счет на оплату", "счёт на оплату", "счета покупателям")),
    ("sale", ("реализация товаров", "реализации товаров", "реализация услуг", "реализации", "реализация", "акт выполненных", "авр")),
)
_SPACES = re.compile(r"\s+")
#: Начало адреса базы из файлов: `file:<организация>` вместо адреса REST-сервиса.
FILE = "file:"


class FilesError(FinanceError):
    """Файлы 1С не читаются - с текстом для человека."""


@dataclass
class Journal:
    """Журнал документов одного вида из одного листа."""

    file: str
    sheet: str
    kind: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def organizations(self) -> list[str]:
        return list(dict.fromkeys(row["organization"] for row in self.rows if row["organization"]))


@dataclass
class Upload:
    """Что в выгрузке 1С: журналы, контрагенты и ОСВ - до записи."""

    journals: list[Journal] = field(default_factory=list)
    #: Наименование контрагента → {name, full_name, bin, kind}.
    parties: dict[str, dict[str, str]] = field(default_factory=dict)
    osv: osv_module.Osv | None = None
    #: Файлы и листы, которые не журнал, не контрагенты и не ОСВ.
    skipped: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def organizations(self) -> list[str]:
        names: dict[str, None] = {}
        for journal in self.journals:
            for name in journal.organizations:
                names[name] = None
        if not names and self.osv is not None:
            for name in self.osv.organizations:
                names[name] = None
        return list(names)

    def to_dict(self) -> dict[str, Any]:
        return {
            "journals": [
                {"file": item.file, "sheet": item.sheet, "kind": item.kind, "documents": len(item.rows),
                 "organizations": item.organizations, "problems": item.problems}
                for item in self.journals
            ],
            "parties": len(self.parties),
            "with_bin": sum(1 for item in self.parties.values() if item["bin"]),
            "osv": self.osv.to_dict() if self.osv is not None else None,
            "organizations": self.organizations,
            "skipped": self.skipped,
            "problems": self.problems,
        }


def kind_of(*texts: str) -> str | None:
    haystack = " ".join(norm(item) for item in texts if item)
    for kind, words in KINDS:
        if any(norm(word) in haystack for word in words):
            return kind
    return None


def _clean(value: Any) -> str:
    return _SPACES.sub(" ", cell_text(value)).strip()


def _journal(table: tables.Table, reading: recognize.Reading, file_name: str) -> Journal | None:
    above = [cell_text(cell) for row in table.rows[: reading.header_index] for cell in row if has_value(cell)]
    kind = kind_of(file_name, table.name, *above)
    if kind is None:
        return None
    out = Journal(file_name, table.name, kind)
    at = reading.at
    titles = {item.key: item.title for item in forms.DOCUMENT_FIELDS}
    for key in ("date", "number", "counterparty", "amount"):
        if at(key) is None:
            out.problems.append(f"Лист «{table.name}»: нет колонки «{titles[key]}»")
    if out.problems:
        return out
    body = table.rows[reading.body_start:]
    order = decide_date_order(cells[at("date")] for cells in body if at("date") < len(cells))

    def cell(cells: list[Any], key: str) -> Any:
        index = at(key)
        return cells[index] if index is not None and index < len(cells) else None

    for offset, cells in enumerate(body):
        line = reading.body_start + offset
        if is_empty(cells) or is_total(cells, dated=True) or line in table.hidden:
            continue
        day = parse_date(cell(cells, "date"), order)
        money = parse_money(cell(cells, "amount"))
        if day is None or money is None:
            if any(has_value(value) for value in cells):
                out.problems.append(f"Строка {line + 1}: нет даты или суммы - пропущена")
            continue
        currency = _clean(cell(cells, "currency")).upper()
        if currency and currency not in ("KZT", "ТГ", "ТЕНГЕ", "₸"):
            out.problems.append(f"Строка {line + 1}: сумма в {currency} - KORT ведёт тенге, документ пропущен")
            continue
        label = _clean(cell(cells, "contract"))
        number, signed = osv_module.contract_of(label)
        content = _clean(cell(cells, "content"))
        start, end = period_in(content)
        out.rows.append({
            "kind": kind,
            "number": _clean(cell(cells, "number")),
            "doc_date": day,
            "amount": (-money.value if money.negative else money.value).quantize(Decimal("0.01")),
            "organization": _clean(cell(cells, "organization")),
            "party_name": _clean(cell(cells, "counterparty")),
            "contract_label": label,
            "contract_number": number,
            "contract_date": signed,
            "content": content,
            "period_from": start,
            "period_to": end,
            "line": line + 1,
        })
    return out


def _parties(table: tables.Table, reading: recognize.Reading, into: dict[str, dict[str, str]], ids: packs.Ids) -> int:
    at = reading.at
    found = 0
    for cells in table.rows[reading.body_start:]:
        def cell(key: str) -> str:
            index = at(key)
            return _clean(cells[index]) if index is not None and index < len(cells) else ""

        name = cell("name")
        if not name or is_label_total(cells):
            continue
        number = packs.digits(cell("bin"))
        into[name] = {
            "name": name, "full_name": cell("full_name") or name,
            "bin": number if ids.checksum_ok(number) else "", "kind": cell("kind"),
        }
        found += 1
    return found


def read(files: Iterable[tuple[str, bytes]], ids: packs.Ids | None = None) -> Upload:
    """Файлы выгрузки 1С → журналы, контрагенты, ОСВ. Ничего не пишет.

    `ids` - номер участника страны компании; без него - страна по умолчанию.
    """
    ids = ids or packs.country().ids
    out = Upload()
    catalog = forms.forms()
    for file_name, data in files:
        try:
            found = tables.read(data, file_name, detail=True)
        except tables.TableError as exc:
            out.problems.append(f"«{file_name}»: {exc}")
            continue
        for table in found:
            if all(is_empty(row) for row in table.rows):
                continue
            sheet = recognize.recognize(table, file_name, catalog=catalog)
            reading = sheet.reading
            form = reading.form.key if reading is not None else None
            if form == forms.BALANCE:
                try:
                    osv = osv_module.parse(table, file_name)
                except osv_module.OsvError as exc:
                    out.problems.append(f"«{file_name}»: {exc}")
                    continue
                if out.osv is not None:
                    out.problems.append(f"«{file_name}»: вторая ОСВ в выгрузке - взята первая, «{out.osv.file}»")
                    continue
                out.osv = osv
                continue
            if form == forms.PARTICIPANT and reading is not None:
                if _parties(table, reading, out.parties, ids):
                    continue
            if form == forms.DOCUMENT and reading is not None:
                journal = _journal(table, reading, file_name)
                if journal is not None:
                    out.journals.append(journal)
                    continue
                out.problems.append(
                    f"«{file_name}», лист «{table.name}»: журнал документов, но не понять каких - "
                    "в названии нет ни «Реализация», ни «Счета на оплату»"
                )
                continue
            out.skipped.append(f"«{file_name}», лист «{table.name}»")
    return out


# ── Запись: база на организацию ──────────────────────────────────────────────


def _ref(kind: str, row: dict[str, Any]) -> str:
    """Постоянный ключ документа из файла: та же выгрузка - те же ключи."""
    return f"file:{kind}:{row['doc_date']:%Y%m%d}:{row['number']}"


def bases_for(session: Session, workspace: Workspace, upload: Upload, *, actor: str = "") -> dict[str, OnecBase]:
    """Организация выгрузки → база из файлов: найти по названию или завести."""
    existing = {
        party_key(base.organization_name): base
        for base in session.scalars(
            sa.select(OnecBase).where(OnecBase.workspace_id == workspace.id, OnecBase.url.startswith(FILE))
        )
    }
    out: dict[str, OnecBase] = {}
    for name in upload.organizations:
        key = party_key(name)
        base = existing.get(key)
        if base is None:
            days = [row["doc_date"] for journal in upload.journals for row in journal.rows if row["organization"] == name]
            base = OnecBase(
                workspace_id=workspace.id, title=f"1С {name} (файлы)", url=f"{FILE}{key}", login="", secret="",
                organization_ref=f"file:{key}", organization_name=name, organization_bin="",
                sync_from=min(days) if days else date.today(), created_by=actor,
            )
            session.add(base)
            session.flush()
            existing[key] = base
        out[name] = base
    return out


def apply(session: Session, workspace: Workspace, upload: Upload, *, actor: str = "") -> dict[str, Any]:
    """Записать документы выгрузки в базы её организаций. Решения человека не трогаются."""
    if not upload.journals:
        raise FilesError("В файлах нет журнала документов 1С - нужны «Реализация товаров и услуг» и «Счета на оплату»")
    bases = bases_for(session, workspace, upload, actor=actor)
    now = datetime.now(timezone.utc)
    report: dict[str, Any] = {}
    unknown_bin: set[str] = set()
    for name, base in bases.items():
        existing = {
            (item.kind, item.ref): item
            for item in session.scalars(sa.select(OnecDocument).where(OnecDocument.base_id == base.id))
        }
        counts: dict[str, int] = {}
        for journal in upload.journals:
            rows = [row for row in journal.rows if row["organization"] == name]
            if not rows:
                continue
            seen: set[tuple[str, str]] = set()
            for row in rows:
                party = upload.parties.get(row["party_name"]) or {}
                if not party.get("bin"):
                    unknown_bin.add(row["party_name"])
                key = (journal.kind, _ref(journal.kind, row))
                seen.add(key)
                item = existing.get(key)
                if item is None:
                    item = OnecDocument(workspace_id=workspace.id, base_id=base.id, kind=journal.kind, ref=key[1])
                    session.add(item)
                    existing[key] = item
                item.number = row["number"]
                item.doc_date = row["doc_date"]
                item.amount = row["amount"]
                item.posted, item.deleted, item.gone = True, False, False
                item.party_ref = ""
                item.party_name = row["party_name"]
                item.party_full_name = party.get("full_name") or ""
                item.party_bin = party.get("bin") or ""
                # Ключ договора 1С - подпись у клиента: файл не несёт Ref_Key.
                item.contract_ref = f"file:{party_key(row['party_name'])}:{norm(row['contract_label'])}" if row["contract_label"] else ""
                item.contract_number = row["contract_number"]
                item.contract_date = row["contract_date"]
                item.basis_ref, item.status, item.reg_number = "", "", ""
                item.signed_at = None
                item.period_from, item.period_to = row["period_from"], row["period_to"]
                item.content = row["content"]
                item.data_version = ""
                item.synced_at = now
                counts[journal.kind] = counts.get(journal.kind, 0) + 1
            first, last = min(row["doc_date"] for row in rows), max(row["doc_date"] for row in rows)
            for (kind, _ref_key), item in existing.items():
                if kind == journal.kind and (kind, item.ref) not in seen and first <= item.doc_date <= last:
                    item.gone = True
        base.counts = counts
        base.last_sync_at = base.last_ok_at = now
        base.last_error = ""
        base.warnings = [problem for journal in upload.journals for problem in journal.problems][:20]
        report[str(base.id)] = {"organization": name, "title": base.title, "counts": counts}
    session.flush()
    return {"bases": report, "without_bin": sorted(unknown_bin)}


def is_files(base: OnecBase) -> bool:
    """База из файлов: адреса в интернете нет, читается выгрузками."""
    return (base.url or "").startswith(FILE)


__all__ = ["FILE", "FilesError", "Journal", "KINDS", "Upload", "apply", "bases_for", "is_files", "kind_of", "read"]
