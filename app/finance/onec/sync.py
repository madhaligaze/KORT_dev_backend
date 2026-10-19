"""Чтение базы 1С в снимок документов (`OnecDocument`).

Что читается и откуда
─────────────────────
* счёт на оплату - `Document_СчетНаОплатуПокупателю`;
* АВР - `Document_РеализацияТоваровУслуг` с отчётным периодом; дата
  подписания акта покупателем - регистр «Сведения по актам выполненных
  работ» (1С заводит запись при проведении с датой акта, бухгалтер ставит
  настоящую; пустая дата - акт не подписан);
* счёт-фактура - `Document_СчетФактураВыданный`, основание - реализация;
* ЭСФ - `Document_ЭСФ` (исходящие): состояние в ИС ЭСФ и регистрационный
  номер, основание - счёт-фактура;
* поступление - `Document_ПлатежноеПоручениеВходящее` с видом «оплата
  покупателя»: по строке расшифровки платежа, у каждой свой договор и счёт.

Чтение - за окно с `sync_from` и только организации базы. Документ из окна,
которого в 1С больше нет, помечается `gone`, а не стирается: в KORT на нём
может стоять решение человека.

1С не трогается ни записью, ни проведением: только GET.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import secretbox
from app.finance.onec.client import Client, Connection, OnecError, OnecMissing
from app.finance.onec.models import OnecBase, OnecDocument

EMPTY_REF = "00000000-0000-0000-0000-000000000000"
SALE_TYPE = "StandardODATA.Document_РеализацияТоваровУслуг"
SF_TYPE = "StandardODATA.Document_СчетФактураВыданный"

#: «за период с 01.10.2026 по 31.10.2026», «с 02.10.2026 года по 01.11.2026 года».
_PERIOD = re.compile(
    r"с\s+(\d{1,2})\.(\d{1,2})\.(\d{4})\s*(?:г\.|года|г)?\s+по\s+(\d{1,2})\.(\d{1,2})\.(\d{4})",
    re.IGNORECASE,
)
#: «Договор № ОБО/146 от 25.06.2026» - номер, когда реквизит «Номер договора» пуст.
_NUMBER_IN_TITLE = re.compile(r"№\s*(.+?)\s+от\s+\d", re.IGNORECASE)


@dataclass
class Snapshot:
    """Прочитанное из базы - до записи в KORT."""

    organizations: list[dict[str, str]]
    organization_ref: str = ""
    documents: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        """Сколько документов каждого вида - без помеченных на удаление: их в учёте нет."""
        out: dict[str, int] = {}
        for item in self.documents:
            if item.get("deleted"):
                continue
            out[item["kind"]] = out.get(item["kind"], 0) + 1
        return out


def connection_of(base: OnecBase) -> Connection:
    return Connection(url=base.url, login=base.login, password=secretbox.unseal(base.secret))


def _date(value: Any) -> date | None:
    text = str(value or "")
    if not text or text.startswith("0001-01-01"):
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _money(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _ref(value: Any) -> str:
    text = str(value or "")
    return "" if not text or text == EMPTY_REF else text


def period_in(text: str) -> tuple[date | None, date | None]:
    """Период услуги из содержания строки счёта: «за период с … по …»."""
    m = _PERIOD.search(text or "")
    if not m:
        return None, None
    try:
        start = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        end = date(int(m.group(6)), int(m.group(5)), int(m.group(4)))
    except ValueError:
        return None, None
    return (start, end) if start <= end else (None, None)


def organizations(client: Client) -> list[dict[str, str]]:
    out = []
    for row in client.rows("Catalog_Организации", select="Ref_Key,Description,ИдентификационныйНомер,DeletionMark"):
        if row.get("DeletionMark"):
            continue
        out.append(
            {"ref": row["Ref_Key"], "name": row.get("Description") or "", "bin": (row.get("ИдентификационныйНомер") or "").strip()}
        )
    return out


def _first_content(lines: Iterable[dict[str, Any]]) -> str:
    for line in lines or []:
        text = str(line.get("Содержание") or "").strip()
        if text:
            return text
    return ""


def read(client: Client, *, organization_ref: str, since: date) -> Snapshot:
    """Прочитать документы организации базы с даты `since` (без записи в KORT)."""
    snapshot = Snapshot(organizations=organizations(client), organization_ref=organization_ref)
    parties = {
        row["Ref_Key"]: row
        for row in client.rows(
            "Catalog_Контрагенты",
            select="Ref_Key,Description,НаименованиеПолное,ИдентификационныйКодЛичности,IsFolder",
            where="IsFolder eq false",
        )
    }
    contracts = {
        row["Ref_Key"]: row
        for row in client.rows(
            "Catalog_ДоговорыКонтрагентов",
            select="Ref_Key,Owner_Key,НомерДоговора,ДатаДоговора,Description,IsFolder",
            where="IsFolder eq false",
        )
    }
    window = f"Организация_Key eq guid'{organization_ref}' and Date ge datetime'{since:%Y-%m-%d}T00:00:00'"

    def base_row(kind: str, row: dict[str, Any], *, ref: str | None = None) -> dict[str, Any]:
        party = parties.get(_ref(row.get("Контрагент_Key"))) or {}
        return {
            "kind": kind,
            "ref": ref or row["Ref_Key"],
            "number": str(row.get("Number") or "").strip(),
            "doc_date": _date(row.get("Date")),
            "amount": _money(row.get("СуммаДокумента")),
            "posted": bool(row.get("Posted")),
            "deleted": bool(row.get("DeletionMark")),
            "party_ref": _ref(row.get("Контрагент_Key")),
            "party_name": str(party.get("Description") or "").strip(),
            "party_full_name": str(party.get("НаименованиеПолное") or "").strip(),
            "party_bin": str(party.get("ИдентификационныйКодЛичности") or "").strip(),
            "data_version": str(row.get("DataVersion") or ""),
            **contract_of(_ref(row.get("ДоговорКонтрагента_Key"))),
        }

    def contract_of(ref: str) -> dict[str, Any]:
        item = contracts.get(ref) if ref else None
        if not item:
            return {"contract_ref": ref, "contract_number": "", "contract_date": None}
        number = str(item.get("НомерДоговора") or "").strip()
        if not number:
            m = _NUMBER_IN_TITLE.search(str(item.get("Description") or ""))
            number = m.group(1).strip() if m else ""
        return {"contract_ref": ref, "contract_number": number, "contract_date": _date(item.get("ДатаДоговора"))}

    def optional(what: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except OnecMissing as exc:
            snapshot.warnings.append(f"{what}: {exc}")

    # Счета на оплату.
    for row in client.rows(
        "Document_СчетНаОплатуПокупателю",
        select="Ref_Key,DataVersion,Number,Date,Posted,DeletionMark,Контрагент_Key,ДоговорКонтрагента_Key,СуммаДокумента,Услуги",
        where=window,
        order="Date,Ref_Key",
    ):
        item = base_row("invoice", row)
        item["content"] = _first_content(row.get("Услуги"))
        item["period_from"], item["period_to"] = period_in(item["content"])
        snapshot.documents.append(item)

    # Реализации (АВР) и дата подписания акта.
    signed: dict[str, date | None] = {}

    def read_signed() -> None:
        for row in client.rows(
            "InformationRegister_СведенияПоАктамВыполненныхРабот",
            select="Организация_Key,ДокументРеализацииУслуг,ДокументРеализацииУслуг_Type,ДатаПодписания",
            where=f"Организация_Key eq guid'{organization_ref}'",
            order="",
            page=5000,
        ):
            if row.get("ДокументРеализацииУслуг_Type") == SALE_TYPE:
                signed[str(row.get("ДокументРеализацииУслуг"))] = _date(row.get("ДатаПодписания"))

    optional("Сведения по актам выполненных работ", read_signed)
    for row in client.rows(
        "Document_РеализацияТоваровУслуг",
        select=(
            "Ref_Key,DataVersion,Number,Date,Posted,DeletionMark,ВидОперации,Контрагент_Key,ДоговорКонтрагента_Key,"
            "СуммаДокумента,ДатаНачалаОтчетногоПериода,ДатаОкончанияОтчетногоПериода,Услуги"
        ),
        where=window,
        order="Date,Ref_Key",
    ):
        item = base_row("sale", row)
        item["content"] = _first_content(row.get("Услуги"))
        start, end = _date(row.get("ДатаНачалаОтчетногоПериода")), _date(row.get("ДатаОкончанияОтчетногоПериода"))
        if not start:
            start, end = period_in(item["content"])
        item["period_from"], item["period_to"] = start, end
        item["signed_at"] = signed.get(row["Ref_Key"])
        snapshot.documents.append(item)

    # Счета-фактуры: основание - реализация.
    for row in client.rows(
        "Document_СчетФактураВыданный",
        select=(
            "Ref_Key,DataVersion,Number,Date,Posted,DeletionMark,Контрагент_Key,ДоговорКонтрагента_Key,СуммаДокумента,"
            "ДокументОснование,ДокументОснование_Type,СпособВыставления,ДокументыОснования"
        ),
        where=window,
        order="Date,Ref_Key",
    ):
        item = base_row("sf", row)
        basis = str(row.get("ДокументОснование") or "") if row.get("ДокументОснование_Type") == SALE_TYPE else ""
        if not _ref(basis):
            basis = next(
                (
                    str(line.get("ДокументОснование") or "")
                    for line in row.get("ДокументыОснования") or []
                    if line.get("ДокументОснование_Type") == SALE_TYPE
                ),
                "",
            )
        item["basis_ref"] = _ref(basis)
        item["status"] = str(row.get("СпособВыставления") or "")
        snapshot.documents.append(item)

    # ЭСФ: исходящие, основание - счёт-фактура.
    def read_esf() -> None:
        for row in client.rows(
            "Document_ЭСФ",
            select=(
                "Ref_Key,DataVersion,Number,Date,Posted,DeletionMark,Направление,Статус,Состояние,РегистрационныйНомер,"
                "Контрагент_Key,СуммаДокумента,СчетФактура,СчетФактура_Type,ДатаОборота"
            ),
            where=window + " and Направление eq 'Исходящий'",
            order="Date,Ref_Key",
        ):
            item = base_row("esf", row)
            item["basis_ref"] = _ref(row.get("СчетФактура")) if row.get("СчетФактура_Type") == SF_TYPE else ""
            item["status"] = str(row.get("Состояние") or row.get("Статус") or "")
            item["reg_number"] = str(row.get("РегистрационныйНомер") or "")
            snapshot.documents.append(item)

    optional("ЭСФ", read_esf)

    # Поступления от покупателей: по строке расшифровки.
    for row in client.rows(
        "Document_ПлатежноеПоручениеВходящее",
        select=(
            "Ref_Key,DataVersion,Number,Date,Posted,DeletionMark,ВидОперации,Контрагент_Key,СуммаДокумента,"
            "НазначениеПлатежа,РасшифровкаПлатежа"
        ),
        where=window + " and ВидОперации eq 'ОплатаПокупателя'",
        order="Date,Ref_Key",
    ):
        lines = row.get("РасшифровкаПлатежа") or [{}]
        for line in lines:
            ref = row["Ref_Key"] if len(lines) == 1 else f"{row['Ref_Key']}:{line.get('LineNumber')}"
            item = base_row("payment", {**row, "ДоговорКонтрагента_Key": line.get("ДоговорКонтрагента_Key")}, ref=ref)
            if len(lines) > 1:
                item["amount"] = _money(line.get("СуммаПлатежа"))
            item["basis_ref"] = _ref(line.get("СчетНаОплату_Key"))
            item["content"] = str(row.get("НазначениеПлатежа") or "").strip()
            snapshot.documents.append(item)

    return snapshot


FIELDS = (
    "number", "doc_date", "amount", "posted", "deleted", "party_ref", "party_name", "party_full_name", "party_bin",
    "contract_ref", "contract_number", "contract_date", "basis_ref", "signed_at", "status", "reg_number",
    "period_from", "period_to", "content", "data_version",
)


def apply(session: Session, base: OnecBase, snapshot: Snapshot, *, since: date) -> dict[str, int]:
    """Записать прочитанное в снимок базы. Решения человека не трогаются."""
    existing = {
        (item.kind, item.ref): item
        for item in session.scalars(sa.select(OnecDocument).where(OnecDocument.base_id == base.id))
    }
    now = datetime.now(timezone.utc)
    seen: set[tuple[str, str]] = set()
    for row in snapshot.documents:
        if row["doc_date"] is None:
            continue
        key = (row["kind"], row["ref"])
        seen.add(key)
        item = existing.get(key)
        if item is None:
            item = OnecDocument(workspace_id=base.workspace_id, base_id=base.id, kind=row["kind"], ref=row["ref"])
            session.add(item)
        for name in FIELDS:
            value = row.get(name)
            if value is None and name in ("number", "party_ref", "party_name", "party_full_name", "party_bin",
                                          "contract_ref", "contract_number", "basis_ref", "status", "reg_number",
                                          "content", "data_version"):
                value = ""
            setattr(item, name, value)
        item.gone = False
        item.synced_at = now
    for key, item in existing.items():
        if key not in seen and item.doc_date >= since:
            item.gone = True
    session.flush()
    return snapshot.counts()


def choose_organization(base: OnecBase, found: list[dict[str, str]]) -> str:
    """Организация, чьи документы читать: выбранная или единственная в базе."""
    refs = {item["ref"] for item in found}
    if base.organization_ref and base.organization_ref in refs:
        return base.organization_ref
    if len(found) == 1:
        return found[0]["ref"]
    if not found:
        raise OnecError("В базе 1С нет ни одной организации")
    raise OnecError("В базе несколько организаций - выберите, чьи документы читать")


def default_since(today: date | None = None) -> date:
    """С какой даты читать по умолчанию: с начала прошлого года."""
    today = today or date.today()
    return date(today.year - 1, 1, 1)


__all__ = [
    "FIELDS",
    "Snapshot",
    "apply",
    "choose_organization",
    "connection_of",
    "default_since",
    "organizations",
    "period_in",
    "read",
]
