"""Excel любой формы: лист таблицы отдела → модель KORT (этап 2, шаг «Excel любой формы»).

Компания приходит с таблицами, которые отделы ведут как привыкли: слушатели
учебного центра с оплатами в строке, помесячные начисления аренды, «долги на
01.06». Приёмник узнаёт смысл строки (`intake/recognize.py`); то, что он не
узнал, человек размечает один раз (`intake/layout.py`). Дальше один путь для
любой формы - тот же, что у 1С: предложение с числами до записи, вопросы
только там, где источник не отвечает сам, пачка - одна точка восстановления.

Формы, которые кладутся в модель отсюда:

* **договор** - строка = договор: участник (по БИН, иначе по написанию),
  договор (по номеру у юрлица, без номера - единственный договор участника у
  этого юрлица), условия - сумма, как начислять, начало, отдел. Найденный
  договор получает только пустое: решение человека и прежний источник файл
  не перебивает, расхождение - строкой сверки. Оплаты в строке («Оплата 1 |
  Дата 1») и «Остаток» - для сверки: оплаты KORT берёт из банка, один
  источник оплат на юрлицо (полигон, О12);
* **остаток на дату** - входящее сальдо договоров на начало учёта; сальдо
  другого источника (ОСВ 1С), которое файл называет иначе, - вопрос (С1), а
  не перезапись;
* **договор за месяц** - строки по месяцам листа сверяются с начислениями
  KORT: начисляет KORT из условий, файл - довод. Месяца нет в файле (Ф7) -
  строка «нет в файле», не вопрос. «Долг на 01.06» в листе - сальдо (как
  выше), «съехал 30.06» в примечании - дата окончания, если KORT её не знает
  (Ф8). Новое написание арендатора у найденного договора - написание
  участника (У6).

Сверка («сравнить») - тот же путь без записи: `plan` ничего не пишет и
отдаёт и то, что ляжет, и то, в чём файл расходится с KORT.

Не угадывает: два участника на одно написание, два договора участника без
номера, сумма «1,5 млн» (Ф2) - вопрос. Ответы - `answers["questions"]` по
ключу вопроса; ответ на разметку листа - `answers["sheets"][лист]`.
"""
from __future__ import annotations

import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history
from app.finance.contracts import restore
from app.finance.contracts.fields import number_key, party_key
from app.finance.contracts.models import Contract
from app.finance.contracts.service import Access, Actor, Registry, read_money
from app.finance.core import bin as bins
from app.finance.core import opening as opening_module
from app.finance.core import settings as core_settings
from app.finance.core.accruals import Ledger, month_key, month_start
from app.finance.intake import forms as forms_module
from app.finance.intake import layout as layout_module
from app.finance.intake import recognize
from app.finance.intake import tables as tables_module
from app.finance.intake.forms import BALANCE, CONTRACT, CONTRACT_MONTH
from app.finance.intake.layout import Layout, Read
from app.finance.intake.values import parse_date
from app.finance.layout import norm
from app.finance.models import Counterparty, Workspace
from app.finance.service import FinanceError

_ZERO = Decimal("0")
#: Формы, которые этот путь кладёт в модель; реестр, выписки и 1С - своими разборами.
FORMS = (CONTRACT, BALANCE, CONTRACT_MONTH)
MONTHS = ("январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр")
_MONTH_WORD = re.compile(r"(?<![а-яё])(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)[а-яё]*")
_YEAR = re.compile(r"(?<!\d)(20\d\d)(?!\d)")
_ON_DAY = re.compile(r"на\s+(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?")
#: «съехал 30.06», «выехал 30.06.2026», «расторгнут с 31.05»: конец договора словами примечания.
_ENDED = re.compile(r"(?<![а-яё])(съехал[а-яё]*|выехал[а-яё]*|расторгнут[а-яё]*|закончил[а-яё]*)\s+(?:с\s+)?(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?")


class ExcelError(FinanceError):
    """Лист нельзя положить в модель - словами для человека."""


def _require(access: Access) -> None:
    if not access.edit:
        raise PermissionError("Заводить из таблиц можно с правом правки реестра")


def _money(value: Decimal | None) -> str | None:
    return format(value.quantize(Decimal("0.01")), "f") if value is not None else None


def _month_of(text: str, today: date) -> date | None:
    """Месяц словами («Июль», «Начисления аренды за июль 2026»): первое число."""
    found = _MONTH_WORD.search(norm(text))
    if found is None:
        return None
    month = next(index for index, stem in enumerate(MONTHS, start=1) if found.group(1).startswith(stem))
    year_found = _YEAR.search(text)
    year = int(year_found.group(1)) if year_found else (today.year - 1 if month > today.month else today.year)
    return date(year, month, 1)


def _day_of(text: str, today: date, year_hint: int | None = None) -> date | None:
    """«на 01.06», «на 01.06.2026»: день из шапки или названия; год - из файла или ближайший прошедший."""
    found = _ON_DAY.search(norm(text))
    if found is None:
        return None
    day, month = int(found.group(1)), int(found.group(2))
    year = int(found.group(3)) if found.group(3) else (year_hint or today.year)
    if year < 100:
        year += 2000
    try:
        value = date(year, month, day)
    except ValueError:
        return None
    if not found.group(3) and not year_hint and value > today:
        value = value.replace(year=value.year - 1)
    return value


# ── Чтение файла ─────────────────────────────────────────────────────────────


@dataclass
class SheetIn:
    """Лист файла: таблица, что узнал приёмник и разметка (ответ человека или запомненная)."""

    table: tables_module.Table
    sheet: recognize.Sheet
    signature: str
    layout: Layout | None
    form: str | None
    read: Read | None = None


def _catalog(session: Session, workspace: Workspace) -> tuple[forms_module.Form, ...]:
    from app.finance.contracts.fields import fields_of

    return forms_module.forms(fields_of(session, workspace.id), core_settings.intake_words(session, workspace.id))


def read_file(session: Session, workspace: Workspace, data: bytes, file_name: str, answers: dict[str, Any] | None = None) -> list[SheetIn]:
    """Листы файла с разметкой: ответ из `answers`, иначе запомненный компанией, иначе - что узнал приёмник."""
    try:
        found = tables_module.read(data, file_name, detail=True)
    except tables_module.TableError as exc:
        raise ExcelError(str(exc)) from exc
    catalog = _catalog(session, workspace)
    stored = core_settings.intake_layouts(session, workspace.id)
    given = (answers or {}).get("sheets") or {}
    out: list[SheetIn] = []
    for sheet in recognize.recognize_file(found, file_name, catalog=catalog):
        table = next(item for item in found if item.name == sheet.name)
        header = sheet.reading.header if sheet.reading else _first_header(table)
        signature = layout_module.signature(header)
        layout = Layout.of(given.get(sheet.name)) or Layout.of(stored.get(signature))
        if layout is not None and not layout.sheet:
            layout.sheet = sheet.name
        form = (layout.form or None) if layout is not None else sheet.form
        item = SheetIn(table, sheet, signature, layout, form)
        if form in FORMS and not (layout and layout.skip):
            item.read = layout_module.read(table, forms_module.by_key(form, catalog), layout)
            if item.read.reading is not None:
                item.signature = layout_module.signature(item.read.reading.header)
        out.append(item)
    return out


def _first_header(table: tables_module.Table) -> list[str]:
    """Шапка листа, который ни одна форма не узнала: первая строка, где больше двух текстовых ячеек."""
    for row in table.rows[:40]:
        cells = [str(cell).strip() for cell in row if isinstance(cell, str) and str(cell).strip()]
        if len(cells) >= 2:
            return [str(cell or "") for cell in row]
    return []


def describe(session: Session, workspace: Workspace, data: bytes, file_name: str) -> dict[str, Any]:
    """«Предложить» для файла - как `recognize.describe`, плюс словарь компании и запомненная разметка.

    Лист, размеченный раньше, называет свою форму сам (`layout`); лист формы,
    которую кладёт этот путь, помечен `excel`: экран предлагает «Показать,
    что заведём».
    """
    from app.finance import formats

    if formats.sniff(data, file_name) == "pdf":
        from app.finance.contracts.fields import fields_of

        return recognize.describe(data, file_name, contract_fields=fields_of(session, workspace.id))
    try:
        sheets = read_file(session, workspace, data, file_name)
    except ExcelError as exc:
        return {"file": file_name, "error": str(exc), "sheets": []}
    out = []
    for item in sheets:
        described = item.sheet.to_dict()
        described["signature"] = item.signature
        described["header"] = [str(cell).strip() for cell in (item.read.reading.header if item.read and item.read.reading else
                                                                item.sheet.reading.header if item.sheet.reading else _first_header(item.table))
                               if str(cell or "").strip()]
        if item.layout is not None and item.layout.skip:
            described["layout"] = item.layout.to_dict()
        elif item.layout is not None:
            described["layout"] = item.layout.to_dict()
            described["form"] = item.layout.form
            form = forms_module.by_key(item.layout.form)
            described["form_title"] = form.title
            if item.read is not None:
                described["columns"] = [
                    {"index": column.index, "header": column.header, "key": column.key, "how": column.how,
                     "title": (form.field(column.key).title if column.key and form.field(column.key) else None), "candidates": []}
                    for column in item.read.columns
                ]
                described["rows"] = len(item.read.rows)
                # Вопрос «что значит строка» снят ответом человека.
                described["questions"] = [q for q in described["questions"] if q.get("kind") != "form"]
        described["excel"] = (described.get("form") in FORMS)
        out.append(described)
    catalog = _catalog(session, workspace)
    forms = [
        {"key": form.key, "title": form.title, "fields": [{"key": item.key, "title": item.title} for item in form.fields]}
        for form in catalog if form.key in FORMS
    ]
    return {"file": file_name, "sheets": out, "forms": forms}


# ── Участники и договоры ─────────────────────────────────────────────────────


@dataclass
class Proposal:
    """Что лист положит в модель - без записи."""

    sheet: str
    form: str
    setup: list[dict[str, Any]] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)
    refusals: list[dict[str, Any]] = field(default_factory=list)
    contracts: list[dict[str, Any]] = field(default_factory=list)
    parties: Counter = field(default_factory=Counter)
    openings: list[dict[str, Any]] = field(default_factory=list)
    ends: list[dict[str, Any]] = field(default_factory=list)
    compare: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    as_of: date | None = None
    rows: int = 0

    def blocking(self) -> list[dict[str, Any]]:
        """Отказы, которые держат лист; БИН с ошибкой у строки - замечание: строка заводится без него (У4)."""
        return [item for item in self.refusals if item.get("kind") != "bin"]

    def to_dict(self) -> dict[str, Any]:
        """Для экрана и API: строка листа (`row`) остаётся здесь, наружу - только поля."""
        counts = Counter(item["action"] for item in self.contracts)
        return {
            "sheet": self.sheet,
            "form": self.form,
            "form_title": forms_module.by_key(self.form).title if self.form else None,
            "rows": self.rows,
            "setup": self.setup,
            "questions": self.questions,
            "refusals": self.refusals,
            "ready": not self.setup and not self.blocking(),
            "contracts": {
                "new": counts["new"], "same": counts["same"], "fill": counts["fill"], "waiting": counts["waiting"],
                "items": [{key: value for key, value in item.items() if key != "row"} for item in self.contracts],
            },
            "parties": dict(self.parties),
            "openings": {
                "as_of": self.as_of.isoformat() if self.as_of else None,
                "count": sum(1 for item in self.openings if item.get("take")),
                "debt": _money(sum((Decimal(item["amount"]) for item in self.openings if item.get("take")), _ZERO)),
                "items": self.openings,
            },
            "ends": self.ends,
            "compare": self.compare,
            "missing": self.missing,
        }


class Context:
    """Что KORT уже знает: участники, наши юрлица, договоры по юрлицам - на один план."""

    def __init__(self, session: Session, workspace: Workspace, *, today: date, access: Access | None = None):
        from app.finance.contracts.payments import former_executors

        self.session, self.workspace, self.today = session, workspace, today
        #: Оплаты по договорам открыты (`contracts/payments._check_money`): иначе сверка без денег KORT.
        self.money_open = access is None or (access.view and "paid" not in access.hidden)
        self.registry = Registry(session, workspace)
        self.contracts = [
            item for item in session.scalars(
                sa.select(Contract).where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None))
            )
        ]
        self.former = former_executors(session, workspace.id)
        self._money = None
        self._ledger: Ledger | None = None

    def own(self, raw: Any) -> uuid.UUID | None:
        """Наше юрлицо по написанию («Тумар Офис», «ИП Сейткали») или id."""
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            value = uuid.UUID(text)
            return value if self.registry.is_own(value) else None
        except ValueError:
            pass
        resolved = self.registry.resolve_party(text, slot="executor", create=False)
        if resolved.party is not None and self.registry.is_own(resolved.party.id):
            return resolved.party.id
        key = party_key(text)
        parties = self.registry.parties_for(list(self.registry.own))
        hits = {
            party_id for party_id, entity in self.registry.own.items()
            if key and key in {party_key(entity.code or ""), party_key(entity.full_name or ""),
                               party_key(parties[party_id].name if party_id in parties else "")}
        }
        return next(iter(hits)) if len(hits) == 1 else None

    def party(self, name: str, number: str) -> tuple[str, Counterparty | None, list[Counterparty]]:
        """Участник строки: (действие, участник, спорные). Действие - same, bin, create, dispute."""
        if number:
            owner = self.session.scalar(
                sa.select(Counterparty).where(
                    Counterparty.workspace_id == self.workspace.id, Counterparty.bin == number, Counterparty.archived_at.is_(None)
                )
            )
            if owner is not None:
                return "same", owner, []
        resolved = self.registry.resolve_party(name, slot="customer", create=False)
        if resolved.ambiguous:
            return "dispute", None, list(resolved.ambiguous)
        if resolved.party is not None:
            found = resolved.party
            if number and found.bin and found.bin != number:
                return "dispute", None, [found]
            return ("bin" if number and not found.bin else "same"), found, []
        return "create", None, []

    def contracts_of(self, party_id: uuid.UUID, entity_id: uuid.UUID | None) -> list[Contract]:
        out = []
        for contract in self.contracts:
            if contract.customer_id != party_id and contract.executor_id != party_id:
                continue
            ours = {contract.executor_id, contract.customer_id} | self.former.get(contract.id, set())
            if entity_id is None or entity_id in ours:
                out.append(contract)
        return out

    def by_number(self, number: str, entity_id: uuid.UUID | None) -> list[Contract]:
        key = number_key(number)
        if not key:
            return []
        out = []
        for contract in self.contracts:
            if contract.number_key != key and number_key(contract.number) != key:
                continue
            ours = {contract.executor_id, contract.customer_id} | self.former.get(contract.id, set())
            if entity_id is None or entity_id in ours:
                out.append(contract)
        return out

    @property
    def money(self):
        if self._money is None:
            from app.finance.core.payments import Money

            self._money = Money(self.session, self.workspace, today=self.today)
        return self._money

    @property
    def ledger(self) -> Ledger:
        if self._ledger is None:
            self._ledger = Ledger(self.session, self.workspace.id, self.contracts)
        return self._ledger


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return " ".join(str(value).split())


def _amount(raw: Any) -> tuple[Decimal | None, str]:
    """Сумма ячейки: число или текст, который числом не стал («1,5 млн» - Ф2)."""
    try:
        return read_money(raw, field="Сумма")
    except FinanceError:
        return None, _text(raw)


def _setup_contract(read: Read, layout: Layout | None, ctx: Context, proposal: Proposal) -> uuid.UUID | None:
    """Чего не хватает листу договоров, чтобы его прочитать: колонка стороны, наше юрлицо, начисление."""
    if not read.has("customer"):
        proposal.setup.append({"kind": "column", "key": "customer", "text": "Какая колонка - вторая сторона договора (клиент)?"})
    executor = ctx.own(layout.executor) if layout and layout.executor else None
    if not read.has("executor") and executor is None:
        names = ctx.registry.parties_for(list(ctx.registry.own))
        own = [{"value": str(party_id), "label": names[party_id].name if party_id in names else entity.code}
               for party_id, entity in ctx.registry.own.items()]
        if len(own) == 1:
            executor = uuid.UUID(own[0]["value"])
        else:
            proposal.setup.append({"kind": "executor", "text": "Чьи это договоры - какое наше юрлицо исполнитель?", "options": own})
    if not read.has("billing") and not read.has("type") and not (layout and layout.billing in ("total", "month")):
        proposal.setup.append({
            "kind": "billing", "text": "Как начисляются договоры листа?",
            "options": [{"value": "total", "label": "вся сумма в месяце начала"}, {"value": "month", "label": "сумма каждый месяц"}],
        })
    return executor


def _plan_contracts(read: Read, layout: Layout | None, ctx: Context, answers: dict[str, Any], proposal: Proposal) -> None:
    executor_default = _setup_contract(read, layout, ctx, proposal)
    rows_of_party: Counter = Counter()
    resolved_rows = []
    for row in read.rows:
        name = _text(row.values.get("customer"))
        if not name:
            continue
        number_raw = bins.digits(row.values.get("party_bin"))
        number = number_raw if number_raw and bins.checksum_ok(number_raw) else ""
        if number_raw and not number:
            proposal.refusals.append({
                "kind": "bin", "line": row.line,
                "text": f"Строка {row.line}: БИН/ИИН «{_text(row.values.get('party_bin'))}» у «{name}» с ошибкой в контрольной цифре - не принят",
            })
        action, party, disputed = ctx.party(name, number)
        executor = ctx.own(row.values.get("executor")) if row.values.get("executor") else executor_default
        key = party.id if party is not None else ("new", party_key(name), number)
        rows_of_party[(key, executor)] += 1
        resolved_rows.append((row, name, number, action, party, disputed, executor, key))
    proposal.rows = len(resolved_rows)
    seen_parties: dict[Any, str] = {}
    for row, name, number, action, party, disputed, executor, key in resolved_rows:
        line_key = f"{read.sheet}:{row.line}"
        if key not in seen_parties:
            seen_parties[key] = action
            proposal.parties[action] += 1
        item: dict[str, Any] = {"line": row.line, "name": name, "bin": number or None, "party": action}
        if action == "dispute":
            chosen = (answers.get(f"party:{line_key}") or "")
            match = next((candidate for candidate in disputed if str(candidate.id) == chosen), None)
            if match is None:
                proposal.questions.append({
                    "kind": "party", "key": f"party:{line_key}", "line": row.line,
                    "text": f"Строка {row.line}: «{name}» - это кто?",
                    "options": [{"value": str(candidate.id), "label": candidate.name} for candidate in disputed],
                })
                proposal.contracts.append({**item, "action": "waiting", "why": "участник спорный"})
                continue
            party, action = match, "same"
        if party is not None:
            item["party_id"] = str(party.id)
        if executor is None:
            proposal.contracts.append({**item, "action": "waiting", "why": "не ясно наше юрлицо"})
            continue
        number_text = _text(row.values.get("number"))
        amount, terms = _amount(row.values.get("amount"))
        start = parse_date(row.values.get("billing_from") or row.values.get("signed_at"), read.dates)
        contract = None
        if party is not None:
            if number_text:
                found = [c for c in ctx.by_number(number_text, executor) if party.id in (c.customer_id, c.executor_id)]
                contract = found[0] if len(found) == 1 else None
            else:
                mine = ctx.contracts_of(party.id, executor)
                if len(mine) == 1 and rows_of_party[(key, executor)] == 1:
                    contract = mine[0]
                elif mine:
                    by_start = [c for c in mine if start and (c.billing_from or c.signed_at) and
                                month_key(c.billing_from or c.signed_at) == month_key(start)]
                    if len(by_start) == 1:
                        contract = by_start[0]
                    else:
                        chosen = answers.get(f"contract:{line_key}")
                        if chosen == "new":
                            contract = None
                        elif chosen and any(str(c.id) == chosen for c in mine):
                            contract = next(c for c in mine if str(c.id) == chosen)
                        else:
                            proposal.questions.append({
                                "kind": "contract", "key": f"contract:{line_key}", "line": row.line,
                                "text": f"Строка {row.line}: у «{party.name}» уже есть договоры - этот из них или новый?",
                                "options": [{"value": str(c.id), "label": c.number or "без номера"} for c in mine]
                                + [{"value": "new", "label": "новый договор"}],
                            })
                            proposal.contracts.append({**item, "action": "waiting", "why": "договор не ясен"})
                            continue
        answered = answers.get(f"amount:{line_key}")
        if amount is None and terms and answered not in (None, ""):
            amount, _rest = _amount(answered)
        if contract is not None:
            item.update({"contract_id": str(contract.id), "number": contract.number or None})
            fill: dict[str, Any] = {}
            if contract.amount is None and amount is not None:
                fill["amount"] = format(amount, "f")
            elif contract.amount is None and terms:
                proposal.questions.append(_amount_question(read.sheet, row.line, name, terms))
            if contract.amount is not None and amount is not None and contract.amount != amount:
                proposal.compare.append({"kind": "amount", "line": row.line, "contract_id": str(contract.id), "name": name,
                                         "file": _money(amount), "kort": _money(contract.amount)})
            if not (contract.billing_from or contract.signed_at) and start:
                fill["billing_from"] = start.isoformat()
            if not contract.billing and layout is not None and layout.billing in ("total", "month"):
                # Реестр не сказал, как начислять («Стоимость» без вида), - лист говорит.
                fill["billing"] = layout.billing
            item.update({"action": "fill" if fill else "same", "fill": fill})
            _compare_paid(row, read, contract, ctx, proposal, name)
        else:
            if amount is None and terms:
                proposal.questions.append(_amount_question(read.sheet, row.line, name, terms))
            item.update({
                "action": "new", "number": number_text or None, "amount": _money(amount), "terms": terms or None,
                "start": start.isoformat() if start else None, "executor": str(executor),
            })
        item["row"] = row
        proposal.contracts.append(item)


def _amount_question(sheet: str, line: int, name: str, terms: str) -> dict[str, Any]:
    return {
        "kind": "amount", "key": f"amount:{sheet}:{line}", "line": line,
        "text": f"Строка {line}: сумма «{terms}» у «{name}» - не число. Сколько это в тенге?",
    }


def _compare_paid(row: layout_module.Row, read: Read, contract: Contract, ctx: Context, proposal: Proposal, name: str) -> None:
    """Оплаты в строке и «Остаток» против KORT: строка сверки, не вопрос. Оплаты KORT - только тому, кому они открыты."""
    if not ctx.money_open:
        return
    pays = [_amount(value)[0] for value in (row.repeats.get("pay_sum") or {}).values()]
    paid_file = sum((value for value in pays if value is not None), _ZERO) if pays else None
    if paid_file is None and row.values.get("paid_snapshot") is not None:
        paid_file = _amount(row.values.get("paid_snapshot"))[0]
    if paid_file is None:
        return
    money = ctx.money.contract(contract)
    if money.paid != paid_file:
        proposal.compare.append({"kind": "paid", "line": row.line, "contract_id": str(contract.id), "name": name,
                                 "file": _money(paid_file), "kort": _money(money.paid)})


# ── Остатки на дату ──────────────────────────────────────────────────────────


def _year_hint(read: Read, file_name: str) -> int | None:
    for text in (read.sheet, file_name):
        found = _YEAR.search(text or "")
        if found:
            return int(found.group(1))
    return None


def _as_of(read: Read, layout: Layout | None, file_name: str, ctx: Context) -> date | None:
    if layout and layout.as_of:
        try:
            return date.fromisoformat(layout.as_of)
        except ValueError:
            pass
    hint = _year_hint(read, file_name)
    for column in read.columns:
        if column.key == "opening":
            day = _day_of(column.header, ctx.today, hint)
            if day:
                return day
    return _day_of(read.sheet, ctx.today, hint)


def _opening_wanted(
    contract: Contract, amount: Decimal, line: int, name: str, as_of: date, ctx: Context, answers: dict[str, Any], proposal: Proposal
) -> None:
    """Сальдо договора из файла: то же, что у KORT, - ничего; другое из другого источника - вопрос (С1)."""
    existing = opening_module.current(ctx.session, ctx.workspace, as_of).get(contract.id)
    have = existing.amount if existing is not None else None
    item = {"line": line, "contract_id": str(contract.id), "number": contract.number or None, "name": name,
            "amount": _money(amount), "kort": _money(have), "take": False}
    if have is not None and have == amount:
        item["state"] = "same"
    elif existing is not None and existing.source != "file":
        key = f"opening:{contract.id}"
        choice = answers.get(key)
        source = {"1c": "1С", "book": "сводной таблицы"}.get(existing.source, existing.source)
        if choice == "file":
            item.update({"state": "changed", "take": True})
        elif choice == "keep":
            item["state"] = "kept"
        else:
            item["state"] = "dispute"
            proposal.questions.append({
                "kind": "opening", "key": key, "line": line,
                "text": (f"«{name}» {contract.number or ''}: долг на {as_of:%d.%m.%Y} по {source} - {_money(have)}, "
                         f"в таблице - {_money(amount)}. Какой верен?"),
                "options": [{"value": "keep", "label": f"по {source}"}, {"value": "file", "label": "по таблице"}],
            })
    elif amount or existing is not None:
        item.update({"state": "new" if existing is None else "changed", "take": True})
    else:
        item["state"] = "same"
    proposal.openings.append(item)


def _plan_balance(read: Read, layout: Layout | None, file_name: str, ctx: Context, answers: dict[str, Any], proposal: Proposal) -> None:
    proposal.as_of = _as_of(read, layout, file_name, ctx)
    if proposal.as_of is None:
        proposal.setup.append({"kind": "as_of", "text": f"Лист «{read.sheet}»: на какой день эти долги?"})
        return
    if not read.has("account") and not read.has("contract"):
        proposal.setup.append({"kind": "column", "key": "account", "text": "Какая колонка - чей долг (клиент)?"})
        return
    entity = ctx.own(layout.executor) if layout and layout.executor else None
    proposal.rows = len(read.rows)
    for row in read.rows:
        name = _text(row.values.get("account"))
        amount, _terms = _amount(row.values.get("opening"))
        if amount is None:
            continue
        hint = _text(row.values.get("contract"))
        action, party, _disputed = ctx.party(name, "") if name else ("dispute", None, [])
        found: list[Contract] = []
        if hint:
            found = ctx.by_number(hint, entity)
            if party is not None:
                found = [c for c in found if party.id in (c.customer_id, c.executor_id)]
        if not found and party is not None:
            mine = ctx.contracts_of(party.id, entity)
            if hint:
                # «Курс» в таблице - предмет договора: тот, чей предмет назван так же.
                subjects = {c.id: norm(ctx.registry.values[c.subject_id].value) for c in mine if c.subject_id in ctx.registry.values}
                found = [c for c in mine if subjects.get(c.id) and subjects[c.id] == norm(hint)]
            if not found and len(mine) == 1:
                found = mine
        if len(found) != 1:
            proposal.missing.append(f"строка {row.line}: «{name or hint}» - договор не найден" if not found
                                    else f"строка {row.line}: «{name or hint}» - договоров несколько")
            continue
        _opening_wanted(found[0], amount, row.line, name or hint, proposal.as_of, ctx, answers, proposal)


# ── Договор за месяц ─────────────────────────────────────────────────────────


#: Формы собственности - не часть имени: «ТОО "Ертис Агро"» и «Ертис Агро ТОО» - одно написание.
_LEGAL = frozenset({"тоо", "ип", "ао", "пао", "оао", "зао", "ооо", "чп", "llp", "llc", "jsc", "ltd"})
_WORD = re.compile(r"[0-9a-zа-яёәіңғүұқөһ]+")


def _same_name(a: str, b: str) -> bool:
    words = lambda text: {word for word in _WORD.findall(norm(text)) if word not in _LEGAL}  # noqa: E731
    return words(a) == words(b)


def _plan_months(reads: list[tuple[Read, Layout | None]], file_name: str, ctx: Context, answers: dict[str, Any], proposal: Proposal,
                 refused: list[str] = ()) -> None:
    """Помесячные листы одной таблицы: сверка с начислениями KORT, сальдо, конец договора.

    `refused` - листы той же таблицы, которые не читаются (две «Сумма», Ф5):
    их месяц в файле есть, и «нет в файле» он не становится.
    """
    months: dict[date, Read] = {}
    present = {month for month in (_month_of(name, ctx.today) for name in refused) if month is not None}
    for read, layout in reads:
        month = None
        if layout and layout.month:
            try:
                month = date.fromisoformat(layout.month)
            except ValueError:
                month = None
        month = month or _month_of(read.sheet, ctx.today) or _month_of(file_name, ctx.today)
        if month is None:
            proposal.setup.append({"kind": "month", "sheet": read.sheet, "text": f"Лист «{read.sheet}»: за какой это месяц?"})
            continue
        months[month] = read
    if not months:
        return
    first, last = min(months), max([*months, *present])
    cursor, gaps = first, []
    while cursor <= last:
        if cursor not in months and cursor not in present:
            gaps.append(cursor)
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    proposal.missing = [f"{month:%m.%Y}: листа нет - KORT начисляет из условий договора" for month in gaps]
    seen_contracts: set[uuid.UUID] = set()
    for month, read in sorted(months.items()):
        layout = next((lay for r, lay in reads if r is read), None)
        entity_default = ctx.own(layout.executor) if layout and layout.executor else None
        hint = _year_hint(read, file_name) or month.year
        for row in read.rows:
            proposal.rows += 1
            name = _text(row.values.get("customer"))
            number = _text(row.values.get("number"))
            entity = ctx.own(row.values.get("own")) if row.values.get("own") else entity_default
            found: list[Contract] = ctx.by_number(number, entity) if number else []
            if len(found) > 1 and name:
                action, party, _d = ctx.party(name, "")
                if party is not None:
                    found = [c for c in found if party.id in (c.customer_id, c.executor_id)]
            if not found and name:
                action, party, _d = ctx.party(name, "")
                found = ctx.contracts_of(party.id, entity) if party is not None else []
            if len(found) != 1:
                proposal.compare.append({"kind": "unknown", "line": row.line, "sheet": read.sheet, "name": name,
                                         "number": number or None, "text": "договора нет в KORT" if not found else "договоров несколько"})
                continue
            contract = found[0]
            if contract.id not in seen_contracts:
                seen_contracts.add(contract.id)
                party_id = contract.customer_id if ctx.registry.is_own(contract.executor_id) else contract.executor_id
                known = ctx.registry.parties.get(party_id)
                aliases = ctx.registry.aliases(party_id)
                if known is not None and name and not _same_name(name, known.name) and party_key(name) not in aliases:
                    proposal.contracts.append({"action": "alias", "line": row.line, "contract_id": str(contract.id),
                                               "party_id": str(party_id), "name": name, "known": known.name})
            accrued = _row_accrual(row)
            kort = _kort_accrual(contract, month, ctx)
            if accrued is not None and kort is not None and accrued != kort:
                proposal.compare.append({"kind": "accrual", "line": row.line, "sheet": read.sheet, "contract_id": str(contract.id),
                                         "number": contract.number or None, "month": month_key(month),
                                         "file": _money(accrued), "kort": _money(kort)})
            elif accrued is not None and kort is None:
                proposal.compare.append({"kind": "accrual", "line": row.line, "sheet": read.sheet, "contract_id": str(contract.id),
                                         "number": contract.number or None, "month": month_key(month),
                                         "file": _money(accrued), "kort": None})
            if row.values.get("opening") is not None:
                column = next((c for c in read.columns if c.key == "opening"), None)
                as_of = _day_of(column.header, ctx.today, hint) if column else None
                amount, _t = _amount(row.values.get("opening"))
                if as_of is not None and amount is not None:
                    proposal.as_of = as_of
                    _opening_wanted(contract, amount, row.line, name or contract.number, as_of, ctx, answers, proposal)
            ended = _ENDED.search(norm(_text(row.values.get("note"))))
            if ended:
                _end_hint(contract, ended, month, row.line, name, ctx, answers, proposal)


def _row_accrual(row: layout_module.Row) -> Decimal | None:
    for key in ("accrued", "sum"):
        if row.values.get(key) is not None:
            return _amount(row.values[key])[0]
    parts = [_amount(row.values[key])[0] for key in ("fixed", "utilities") if row.values.get(key) is not None]
    return sum((part for part in parts if part is not None), _ZERO) if parts else None


def _kort_accrual(contract: Contract, month: date, ctx: Context) -> Decimal | None:
    schedule = ctx.ledger.schedule(contract, since=month, until=month)
    amounts = [item.amount for item in schedule.accruals if item.month == month_key(month) and item.amount is not None]
    return sum(amounts, _ZERO) if amounts else None


def _end_hint(contract: Contract, ended: re.Match, month: date, line: int, name: str, ctx: Context,
              answers: dict[str, Any], proposal: Proposal) -> None:
    day, mon = int(ended.group(2)), int(ended.group(3))
    year = int(ended.group(4)) if ended.group(4) else month.year
    if year < 100:
        year += 2000
    try:
        end = date(year, mon, day)
    except ValueError:
        return
    item = {"line": line, "contract_id": str(contract.id), "number": contract.number or None, "name": name,
            "end": end.isoformat(), "kort": contract.end_date.isoformat() if contract.end_date else None}
    if any(existing["contract_id"] == item["contract_id"] for existing in proposal.ends):
        return
    if contract.end_date == end:
        item["state"] = "same"
    else:
        key = f"end:{contract.id}"
        choice = answers.get(key)
        if choice == "end":
            item["state"] = "take"
        elif choice == "keep":
            item["state"] = "kept"
        else:
            item["state"] = "dispute"
            known = f"KORT считает договор действующим" if contract.end_date is None else f"у KORT окончание {contract.end_date:%d.%m.%Y}"
            proposal.questions.append({
                "kind": "end", "key": key, "line": line,
                "text": f"«{name}» {contract.number or ''}: в таблице «{ended.group(0)}», а {known}. Договор закончился {end:%d.%m.%Y}?",
                "options": [{"value": "end", "label": f"да, {end:%d.%m.%Y}"}, {"value": "keep", "label": "нет, как у KORT"}],
            })
    proposal.ends.append(item)


# ── План и запись ────────────────────────────────────────────────────────────


def plan(session: Session, workspace: Workspace, data: bytes, file_name: str, answers: dict[str, Any] | None = None,
         *, today: date, access: Access | None = None) -> dict[str, Any]:
    """Что файл положит в модель, по листам - и где он расходится с KORT. Ничего не пишет."""
    proposals, _sheets = _plan(session, workspace, data, file_name, answers or {}, today=today, access=access)
    return {"file": file_name, "sheets": [proposal.to_dict() for proposal in proposals]}


def _plan(session: Session, workspace: Workspace, data: bytes, file_name: str, answers: dict[str, Any], *, today: date,
          access: Access | None = None) -> tuple[list[Proposal], list[SheetIn]]:
    sheets = read_file(session, workspace, data, file_name, answers)
    ctx = Context(session, workspace, today=today, access=access)
    questions = answers.get("questions") or {}
    out: list[Proposal] = []
    monthly: list[tuple[Read, Layout | None]] = []
    refused: list[str] = []
    for item in sheets:
        if item.layout is not None and item.layout.skip:
            continue
        if item.form not in FORMS:
            if item.form is None:
                proposal = Proposal(item.sheet.name, CONTRACT)
                proposal.form = ""
                proposal.setup.append({"kind": "form", "text": f"Лист «{item.sheet.name}»: что значит строка?",
                                       "options": [{"value": form.key, "label": form.title, "example": form.examples}
                                                   for form in _catalog(session, workspace) if form.key in FORMS]})
                out.append(proposal)
            continue
        read = item.read
        assert read is not None
        if read.reading is None:
            proposal = Proposal(item.sheet.name, item.form)
            proposal.setup.append({"kind": "columns", "text": f"Лист «{item.sheet.name}»: колонок этой формы не нашлось - разметьте колонки"})
            out.append(proposal)
            continue
        if item.form == CONTRACT_MONTH:
            if read.refusals:
                proposal = Proposal(item.sheet.name, item.form)
                proposal.refusals.extend(read.refusals)
                out.append(proposal)
                refused.append(item.sheet.name)
                continue
            monthly.append((read, item.layout))
            continue
        proposal = Proposal(item.sheet.name, item.form)
        proposal.refusals.extend(read.refusals)
        proposal.questions.extend(_sheet_questions(read))
        if item.form == CONTRACT:
            _plan_contracts(read, item.layout, ctx, questions, proposal)
        elif item.form == BALANCE:
            _plan_balance(read, item.layout, file_name, ctx, questions, proposal)
        out.append(proposal)
    if monthly:
        proposal = Proposal(", ".join(read.sheet for read, _layout in monthly), CONTRACT_MONTH)
        for read, _layout in monthly:
            proposal.questions.extend(_sheet_questions(read))
        _plan_months(monthly, file_name, ctx, questions, proposal, refused)
        out.append(proposal)
    return out, sheets


def _sheet_questions(read: Read) -> list[dict[str, Any]]:
    """Вопросы разметки, которые человек отвечает листом: порядок даты, скрытые строки, спорные колонки."""
    out = []
    for question in read.questions:
        out.append({**question, "key": question.get("key") or f"{question['kind']}:{read.sheet}", "sheet": read.sheet,
                    "layout": question["kind"] in ("date_order", "hidden", "column")})
    return out


def apply(session: Session, workspace: Workspace, access: Access, actor: Actor, data: bytes, file_name: str,
          answers: dict[str, Any], *, today: date) -> dict[str, Any]:
    """Положить файл в модель: участники, договоры, сальдо, окончания - одной пачкой с точкой восстановления.

    Лист с открытой разметкой (форма, наше юрлицо, начисление) не пишется;
    строки со спорным участником или договором ждут ответа. Разметка каждого
    листа запоминается словарём компании.
    """
    from app.finance.contracts.service import Mode, create, patch
    from app.finance.core import participants
    from app.finance.core.changes import status_by_phase

    _require(access)
    proposals, sheets = _plan(session, workspace, data, file_name, answers, today=today, access=access)
    # Вопросы о данных (сумма текстом и т. п.) не держат лист; разметка - держит.
    blocked = [p.sheet for p in proposals if p.setup or p.blocking() or any(q.get("layout") for q in p.questions)]
    for item in sheets:
        if item.layout is not None and item.read is not None and item.read.reading is not None:
            layout = item.layout.to_dict()
            layout["columns"] = {column.header: (column.key or layout_module.SKIP) for column in item.read.columns
                                 if column.how == "answer" or (column.key and column.how == "catalog")}
            core_settings.remember_layout(session, workspace, access, actor, item.signature, layout)
    registry = Registry(session, workspace)
    active = status_by_phase(session, workspace.id, "active")
    terminated = status_by_phase(session, workspace.id, "terminated")
    created: list[str] = []
    filled = 0
    counts: Counter = Counter()
    openings: dict[uuid.UUID, tuple[Decimal, int]] = {}
    as_of: date | None = None
    layouts = {item.sheet.name: item.layout for item in sheets}
    for proposal in proposals:
        if proposal.sheet in blocked:
            continue
        layout = layouts.get(proposal.sheet)
        for item in proposal.contracts:
            if item["action"] == "alias":
                if participants.remember(session, workspace, uuid.UUID(item["party_id"]), item["name"], source="registry"):
                    counts["alias"] += 1
                continue
            if item["action"] in ("waiting", "same"):
                continue
            row = item.get("row")
            if item["action"] == "fill":
                patch(session, workspace, access, actor, uuid.UUID(item["contract_id"]), item["fill"], known_seq=None,
                      mode=Mode(kind="fix", summary=f"из таблицы «{file_name}»"))
                filled += 1
                continue
            party = _ensure_party(session, workspace, registry, item["name"], item.get("bin") or "", item.get("party_id"))
            participants.remember(session, workspace, party.id, item["name"], source="registry")
            values: dict[str, Any] = {
                "customer": {"id": str(party.id)}, "executor": {"id": item["executor"]},
                "billing": (layout.billing if layout and layout.billing in ("total", "month") else None),
            }
            if item.get("number"):
                values["number"] = item["number"]
            if item.get("amount"):
                values["amount"] = item["amount"]
            elif item.get("terms"):
                values["amount"] = item["terms"]
            if item.get("start"):
                values["billing_from"] = item["start"]
                values["signed_at"] = item["start"]
            if row is not None:
                for key in ("subject", "type", "note"):
                    if row.values.get(key) not in (None, ""):
                        values[key] = _text(row.values[key])
            department = (_text(row.values.get("department")) if row is not None and row.values.get("department") else "") or (
                layout.department if layout else "")
            if department:
                values["department"] = department
            if active is not None:
                values["status"] = {"id": str(active.id)}
            values = {key: value for key, value in values.items() if value not in (None, "")}
            try:
                contract = create(session, workspace, access, actor, values)
            except FinanceError:
                if "department" not in values:
                    raise
                values.pop("department")
                contract = create(session, workspace, access, actor, values)
            created.append(str(contract.id))
        for item in proposal.openings:
            if item.get("take"):
                openings[uuid.UUID(item["contract_id"])] = (Decimal(item["amount"]), item["line"])
                as_of = proposal.as_of
        if proposal.form == BALANCE and proposal.as_of is not None and as_of is None:
            as_of = proposal.as_of
        for item in proposal.ends:
            if item.get("state") == "take" and terminated is not None:
                from app.finance.core.changes import status_from

                status_from(session, workspace, access, actor, uuid.UUID(item["contract_id"]), terminated.id,
                            date.fromisoformat(item["end"]) + timedelta(days=1), summary=f"из таблицы «{file_name}»")
                counts["ended"] += 1
    moved: dict[str, Any] = {"done": 0}
    if as_of is not None:
        since = core_settings.payments(session, workspace).since
        if since is None:
            core_settings.set_payments(session, workspace, access, actor, since=as_of)
            since = as_of
        if openings:
            if since != as_of:
                raise ExcelError(f"Учёт идёт с {since:%d.%m.%Y}, а долги в таблице - на {as_of:%d.%m.%Y}: нужны долги на начало учёта")
            moved = opening_module.apply_wanted(
                session, workspace, access, actor, {"accruals": openings, "acts": openings}, since=since, source="file",
                title=f"входящее сальдо на {since:%d.%m.%Y} из таблицы «{file_name}»", only=set(openings),
            )
    point = None
    if created:
        title = f"из таблицы «{file_name}» заведено договоров: {len(created)}"
        point = restore._point(session, workspace, actor, "contracts_create", title, {"ids": created})
    if created or filled or counts or moved.get("done"):
        history.write(
            session, workspace, kind="core.excel", entity="contracts", entity_id=None,
            title=f"из таблицы «{file_name}»: договоров заведено {len(created)}, дополнено {filled}, сальдо {moved.get('done', 0)}",
            after={"created": len(created), "filled": filled, **counts}, actor=actor.email,
        )
    session.flush()
    return {
        "created": len(created), "filled": filled, "aliases": counts["alias"], "ended": counts["ended"],
        "openings": moved.get("done", 0), "blocked": blocked, "point": restore._point_ref(point),
        "sheets": [proposal.to_dict() for proposal in proposals],
    }


def _ensure_party(
    session: Session, workspace: Workspace, registry: Registry, name: str, number: str, party_id: str | None = None
) -> Counterparty:
    """Участник строки: найденный планом (или выбранный человеком), по БИН, по написанию - или новый."""
    known = registry.parties.get(uuid.UUID(party_id)) if party_id else None
    if known is not None:
        if number and not known.bin:
            known.bin, known.kind = number, bins.kind_of(number, known.name)
        return known
    if number:
        owner = session.scalar(
            sa.select(Counterparty).where(
                Counterparty.workspace_id == workspace.id, Counterparty.bin == number, Counterparty.archived_at.is_(None)
            )
        )
        if owner is not None:
            return owner
    resolved = registry.resolve_party(name, slot="customer")
    if resolved.party is None:
        raise ExcelError(f"«{name}»: подходит несколько участников - выберите в вопросах")
    party = resolved.party
    if number and not party.bin:
        party.bin, party.kind = number, bins.kind_of(number, party.name)
    return party


__all__ = ["ExcelError", "FORMS", "Proposal", "apply", "describe", "plan", "read_file"]
