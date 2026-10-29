"""Команды ручного ввода и денег - компания заводится через API модели целиком.

Входов в KORT три: 1С, таблицы и руки. До этих команд API модели умело
договоры и виды, а всё остальное шло маршрутами экранов: наше юрлицо, счёт,
участник, оплата и решение по ней, деление оплаты, сальдо, документы месяца,
загрузки файлов. Фронт-холст или чужой сервис не мог завести компанию с нуля,
не выходя за версию модели.

Команды - те же функции ядра, что у экранов (`core/entry.py`,
`core/participants.py`, `core/documents.py`, `core/payments.py`,
`core/opening.py`, `core/excel.py`, `core/onec_start.py`): права, журнал
действий, «не угадывать» - одни. Файл приходит в команде строкой base64:
«предложить» и «применить» получают один и тот же файл и не хранят его.
Вопрос, без ответа на который файл не положить (порядок даты, чей счёт), -
отказ `question` с вариантами: ответ приходит в следующей команде.
"""
from __future__ import annotations

import base64
import binascii
import uuid
from datetime import date
from typing import Any, Literal

from pydantic import Field

from app.finance.model.base import Command, CommandError, Done, Step, _actor, _label, _Params
from app.finance.model.context import Ctx

#: Файл в команде - base64; потолок как у загрузки файла экраном, с запасом на кодирование.
FILE_CHARS = 22 * 1024 * 1024


class Question(CommandError):
    """Без ответа человека файл не положить: вопрос с вариантами (`kind`, `title`, `options`)."""

    def __init__(self, text: str, question: dict[str, Any]) -> None:
        super().__init__(text)
        self.question = question


def _file(name: str, data: str) -> bytes:
    from app.finance.config import finance_settings

    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CommandError(f"Файл «{name}»: data - не base64") from exc
    if len(raw) > finance_settings.import_max_bytes:
        raise CommandError(f"Файл «{name}» больше {finance_settings.import_max_mb} МБ")
    if not raw:
        raise CommandError(f"Файл «{name}» пуст")
    return raw


def _contract_label(ctx: Ctx, contract_id: uuid.UUID) -> str:
    from app.finance.contracts import service

    return _label(service.get_contract(ctx.session, ctx.workspace, contract_id))


# ── Участники, наши юрлица, счета ────────────────────────────────────────────


class ParticipantCreate(_Params):
    name: str = Field(min_length=1, max_length=300)
    #: БИН/ИИН - проверяется страной компании; уже записан у участника - это он.
    bin: str | None = Field(default=None, max_length=20)
    full_name: str | None = Field(default=None, max_length=500)
    #: Поставщик, а не клиент - для новой карточки.
    supplier: bool = False


def _participant_create(ctx: Ctx, p: ParticipantCreate) -> Done:
    from app.finance.core import participants

    found = participants.create(
        ctx.session, ctx.workspace, ctx.access, _actor(ctx), name=p.name, bin=p.bin or "", full_name=p.full_name or "",
        supplier=p.supplier,
    )
    title = f"участник «{found['name']}» заведён" if found["created"] else f"участник «{found['name']}» уже есть"
    return Done(title=title, result=found)


class ParticipantUpdate(_Params):
    id: uuid.UUID
    full_name: str | None = Field(default=None, max_length=500)
    #: Пусто - снять.
    bin: str | None = Field(default=None, max_length=20)
    #: Ещё одно написание участника (как его пишут банк, 1С, отдел).
    alias: str | None = Field(default=None, max_length=300)


def _participant_update(ctx: Ctx, p: ParticipantUpdate) -> Done:
    from app.finance.core import participants

    actor = _actor(ctx)
    given = p.model_dump(exclude_unset=True)
    out: dict[str, Any] = {"id": str(p.id)}
    if "full_name" in given:
        out.update(participants.update(ctx.session, ctx.workspace, ctx.access, actor, p.id, {"full_name": p.full_name or ""}))
    if "bin" in given:
        out.update(participants.set_bin(ctx.session, ctx.workspace, ctx.access, actor, p.id, p.bin or ""))
    if p.alias:
        out.update(participants.add_alias(ctx.session, ctx.workspace, ctx.access, actor, p.id, p.alias))
    if len(out) == 1:
        raise CommandError("Нечего менять: full_name, bin или alias")
    return Done(title="участник изменён", result=out)


class EntityCreate(_Params):
    name: str = Field(min_length=1, max_length=300)
    code: str = Field(default="", max_length=40)
    bin: str | None = Field(default=None, max_length=20)
    full_name: str = Field(default="", max_length=500)
    vat_payer: bool = False


def _entity_create(ctx: Ctx, p: EntityCreate) -> Done:
    from app.finance.core import entry

    found = entry.add_entity(
        ctx.session, ctx.workspace, ctx.access, _actor(ctx), name=p.name, code=p.code, bin=p.bin or "", full_name=p.full_name,
        vat_payer=p.vat_payer,
    )
    return Done(title=f"наше юрлицо «{found['name']}»", result=found)


class AccountCreate(_Params):
    #: Наше юрлицо - чей счёт (`describe` → `entities`).
    entity: uuid.UUID
    #: Пусто - «Счёт <юрлицо>».
    name: str = Field(default="", max_length=200)
    currency: str | None = Field(default=None, max_length=3)
    number: str = Field(default="", max_length=40)


def _account_create(ctx: Ctx, p: AccountCreate) -> Done:
    from app.finance.core import entry

    account = entry.add_account(
        ctx.session, ctx.workspace, ctx.access, _actor(ctx), entity_id=p.entity, name=p.name, currency=p.currency, number=p.number,
    )
    return Done(title=f"счёт «{account.name}»", result={"id": str(account.id), "name": account.name})


# ── Начало учёта, источник оплат, сальдо ─────────────────────────────────────


class AccountingStart(_Params):
    #: С какого дня оплаты в KORT гасят начисления; `null` - по первой оплате.
    since: date | None


def _accounting_start(ctx: Ctx, p: AccountingStart) -> Done:
    from app.finance.core import entry

    out = entry.start(ctx.session, ctx.workspace, ctx.access, _actor(ctx), p.since)
    return Done(
        title=f"учёт с {p.since:%d.%m.%Y}" if p.since else "начало учёта - по первой оплате", result=out,
        steps=[Step("entry_since", {"before": out["before"]})],
    )


class PaymentsSource(_Params):
    entity: uuid.UUID
    source: Literal["journal", "1c"]


def _payments_source(ctx: Ctx, p: PaymentsSource) -> Done:
    from app.finance.core import entry

    out = entry.use_source(ctx.session, ctx.workspace, ctx.access, _actor(ctx), p.entity, p.source)
    words = {"journal": "журнал денег", "1c": "1С"}
    return Done(title=f"оплаты юрлица - из {words[p.source]}", result=out,
                steps=[Step("entry_source", {"entity_id": str(p.entity), "before": out["before"]})])


class OpeningSet(_Params):
    contract: uuid.UUID
    #: Сальдо на начало учёта; минус - аванс клиента; `null` - снять.
    amount: str | None = Field(max_length=40)
    basis: Literal["accruals", "acts"] = "accruals"


def _opening_set(ctx: Ctx, p: OpeningSet) -> Done:
    from app.finance.contracts.service import read_money, today
    from app.finance.core import opening
    from app.finance.core.payments import Money

    value = None
    if p.amount not in (None, ""):
        value, terms = read_money(p.amount, field="Сальдо")
        if value is None:
            raise CommandError(f"Сальдо: «{terms}» не похоже на число")
    since = Money(ctx.session, ctx.workspace, today=today()).since
    if since is None:
        raise CommandError("Начало учёта не задано и оплат ещё нет: сначала команда accounting.start")
    was = opening.current(ctx.session, ctx.workspace, since, p.basis).get(p.contract)
    before = format(was.amount, "f") if was is not None and was.amount is not None else None
    opening.set_one(ctx.session, ctx.workspace, ctx.access, _actor(ctx), p.contract, since=since, amount=value, basis=p.basis)
    return Done(
        title=f"{_contract_label(ctx, p.contract)}: сальдо на {since:%d.%m.%Y}", contracts=[str(p.contract)],
        result={"since": since.isoformat(), "amount": format(value, "f") if value is not None else None, "basis": p.basis},
        steps=[Step("entry_opening", {"contract_id": str(p.contract), "since": since.isoformat(), "basis": p.basis, "before": before})],
    )


# ── Оплаты ───────────────────────────────────────────────────────────────────


class PaymentRecord(_Params):
    #: `in` - поступление, `out` - списание (у договора продажи - возврат клиенту).
    direction: Literal["in", "out"] = "in"
    day: date
    amount: str = Field(min_length=1, max_length=40)
    #: Счёт (`describe` - нет, справочник счетов); или наше юрлицо - его единственный счёт.
    account: uuid.UUID | None = None
    entity: uuid.UUID | None = None
    #: Плательщик или получатель - участник по id или имени; у договора - его вторая сторона.
    party: str | None = Field(default=None, max_length=300)
    #: Договор - оплата сразу ложится на него (решение человека).
    contract: uuid.UUID | None = None
    #: «За какой месяц» - 2026-07.
    month: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    comment: str = Field(default="", max_length=1000)
    #: Код клиента: повтор команды не заводит вторую оплату.
    key: str | None = Field(default=None, max_length=80)


def _payment_record(ctx: Ctx, p: PaymentRecord) -> Done:
    from app.finance.core import entry

    out = entry.record_payment(
        ctx.session, ctx.workspace, ctx.access, _actor(ctx), direction=p.direction, day=p.day, amount=p.amount,
        account_id=p.account, entity_id=p.entity, party=p.party, contract_id=p.contract, month=p.month, comment=p.comment, key=p.key,
    )
    words = "поступление" if p.direction == "in" else "списание"
    return Done(
        title=f"{words} {p.day:%d.%m.%Y} на {p.amount}" if out["created"] else "оплата уже записана",
        result=out, contracts=[str(p.contract)] if p.contract else [],
        steps=[Step("entry_operation", {"operation_id": out["operation_id"], "created": True})] if out["created"] else [],
    )


class PaymentUpdate(_Params):
    id: uuid.UUID
    day: date | None = None
    amount: str | None = Field(default=None, max_length=40)
    party: str | None = Field(default=None, max_length=300)
    month: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    comment: str | None = Field(default=None, max_length=1000)


def _payment_update(ctx: Ctx, p: PaymentUpdate) -> Done:
    from app.finance.core import entry

    values = p.model_dump(exclude_unset=True)
    values.pop("id", None)
    out = entry.update_payment(ctx.session, ctx.workspace, ctx.access, _actor(ctx), p.id, values)
    return Done(title="оплата изменена", result=out,
                steps=[Step("entry_operation", {"operation_id": str(p.id), "before": out["before"]})])


class PaymentDelete(_Params):
    id: uuid.UUID


def _payment_delete(ctx: Ctx, p: PaymentDelete) -> Done:
    from app.finance.core import entry

    out = entry.delete_payment(ctx.session, ctx.workspace, ctx.access, _actor(ctx), p.id)
    return Done(title="оплата удалена", result=out, steps=[Step("entry_operation", {"operation_id": str(p.id), "deleted": True})])


def _decision_contracts(ctx: Ctx, operation_id: uuid.UUID) -> list[str]:
    from app.finance.core import entry

    return [item["contract_id"] for item in entry._decisions(ctx.session, ctx.workspace.id, operation_id) if item["contract_id"]]


class PaymentDecide(_Params):
    operation: uuid.UUID
    #: `link` - к договору, `none` - не по договору, `auto` - снять решение, разнести по выписке.
    action: Literal["link", "none", "auto"]
    contract: uuid.UUID | None = None


def _payment_decide(ctx: Ctx, p: PaymentDecide) -> Done:
    from app.finance.core import entry
    from app.finance.core import payments as payments_module

    before = entry._decisions(ctx.session, ctx.workspace.id, p.operation)
    touched = {item["contract_id"] for item in before if item["contract_id"]}
    out = payments_module.decide(ctx.session, ctx.workspace, ctx.access, _actor(ctx), p.operation, action=p.action, contract_id=p.contract)
    if p.contract:
        touched.add(str(p.contract))
    words = {"link": "к договору", "none": "не по договору", "auto": "разнесение снова по выписке"}
    return Done(title=f"оплата - {words[p.action]}", result=out, contracts=sorted(touched),
                steps=[Step("entry_decision", {"operation_id": str(p.operation), "before": before})])


class Part(_Params):
    contract: uuid.UUID
    amount: str = Field(min_length=1, max_length=40)


class PaymentSplit(_Params):
    operation: uuid.UUID
    parts: list[Part] = Field(min_length=2, max_length=20)


def _payment_split(ctx: Ctx, p: PaymentSplit) -> Done:
    from app.finance.core import entry
    from app.finance.core import payments as payments_module

    before = entry._decisions(ctx.session, ctx.workspace.id, p.operation)
    out = payments_module.split(
        ctx.session, ctx.workspace, ctx.access, _actor(ctx), p.operation, [(part.contract, part.amount) for part in p.parts],
    )
    touched = {item["contract_id"] for item in before if item["contract_id"]} | {str(part.contract) for part in p.parts}
    return Done(title=f"оплата разделена на {len(p.parts)} договора", result=out, contracts=sorted(touched),
                steps=[Step("entry_decision", {"operation_id": str(p.operation), "before": before})])


# ── Документы месяца ─────────────────────────────────────────────────────────


class DocumentRecord(_Params):
    contract: uuid.UUID
    kind: Literal["bill", "act", "esf"]
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    number: str = Field(default="", max_length=60)
    day: date | None = None
    #: Пусто у счёта и АВР - начисление месяца по условиям договора.
    amount: str | None = Field(default=None, max_length=40)
    #: АВР подписан клиентом.
    signed: date | None = None
    note: str = Field(default="", max_length=500)


def _document_record(ctx: Ctx, p: DocumentRecord) -> Done:
    from app.finance.core import documents
    from app.finance.core.accruals import month_start

    if p.kind == "bill" and not ctx.can("invoices", "edit"):
        raise PermissionError("Счета вам не открыты на правку")
    was = documents.find(ctx.session, ctx.workspace.id, p.contract, p.kind, month_start(p.month))
    before = documents.state(was)
    doc = documents.record(
        ctx.session, ctx.workspace, ctx.access, _actor(ctx), p.contract, kind=p.kind, month=p.month, number=p.number,
        day=p.day, amount=p.amount, signed=p.signed, note=p.note,
    )
    return Done(
        title=f"{_contract_label(ctx, p.contract)}: {documents.KIND_TITLES[p.kind]} за {p.month}", result=documents.out(doc),
        contracts=[str(p.contract)], steps=[Step("entry_document", {"document_id": str(doc.id), "before": before})],
    )


class DocumentRemove(_Params):
    id: uuid.UUID


def _document_remove(ctx: Ctx, p: DocumentRemove) -> Done:
    from app.finance.core import documents

    doc = documents.remove(ctx.session, ctx.workspace, ctx.access, _actor(ctx), p.id)
    before = documents.state(doc)
    return Done(
        title=f"{documents.KIND_TITLES[doc.kind]} снят", result={"id": str(doc.id), "removed": True},
        contracts=[str(doc.contract_id)] if doc.contract_id else [],
        steps=[Step("entry_document", {"document_id": str(doc.id), "before": before})],
    )


# ── Файлы: таблица, выписка, 1С ──────────────────────────────────────────────


class TableApply(_Params):
    name: str = Field(min_length=1, max_length=255)
    data: str = Field(min_length=1, max_length=FILE_CHARS)
    #: Ответы на вопросы разбора - те же, что у экрана «Разметить таблицу».
    answers: dict[str, Any] = Field(default_factory=dict)


def _table_apply(ctx: Ctx, p: TableApply) -> Done:
    from app.finance.contracts.service import today
    from app.finance.core import excel

    out = excel.apply(ctx.session, ctx.workspace, ctx.access, _actor(ctx), _file(p.name, p.data), p.name, p.answers, today=today())
    blocked = ", ".join(f"«{name}»" for name in out.get("blocked") or [])
    title = f"таблица «{p.name}»: заведено {out.get('created', 0)}, дополнено {out.get('filled', 0)}"
    return Done(title=title + (f"; ждут разметки {blocked}" if blocked else ""), result=out)


class TableCreate(_Params):
    """Договоры из таблицы «договор за месяц», которых нет в KORT, - группы «нет в реестре» вызова «сравнить»."""

    name: str = Field(min_length=1, max_length=255)
    data: str = Field(min_length=1, max_length=FILE_CHARS)
    #: Ключи групп (`compare` → `accruals.unknown[].key`).
    keys: list[str] = Field(min_length=1, max_length=500)
    entity: uuid.UUID | None = None
    answers: dict[str, Any] = Field(default_factory=dict)


def _table_create(ctx: Ctx, p: TableCreate) -> Done:
    from app.finance.contracts import restore
    from app.finance.contracts.service import today
    from app.finance.core import changes, shadow
    from app.finance.intake import compare

    actor = _actor(ctx)
    book = compare.table_book(ctx.session, ctx.workspace, _file(p.name, p.data), p.name, today=today(), entity=p.entity, answers=p.answers)
    groups = {item["key"]: item for item in shadow.Reconciler(ctx.session, ctx.workspace, ctx.access, book).unknown_groups()}
    created: list[str] = []
    for key in dict.fromkeys(p.keys):
        group = groups.get(key)
        if group is None:
            raise CommandError(f"Группы {key} в таблице нет - сравните таблицу заново")
        if group["candidates"]:
            # Строки похожи на договоры KORT (тот же клиент, номер у двоих) - не заводить второй, решает человек.
            raise CommandError(f"«{group['customer']}» {group['number'] or ''}: похоже на договор KORT - решите в сверке, а не заводите второй")
        created.append(str(changes.create_from_book(ctx.session, ctx.workspace, ctx.access, actor, book, key).id))
    restore._point(ctx.session, ctx.workspace, actor, "contracts_create", f"из таблицы «{p.name}» заведено договоров: {len(created)}",
                   {"ids": created})
    return Done(title=f"из таблицы «{p.name}» заведено договоров: {len(created)}", result={"created": created}, contracts=created)


class TableConnect(_Params):
    """Таблица «договор за месяц» файлом - сводная компании (вместо листа Google или впервые)."""

    name: str = Field(min_length=1, max_length=255)
    data: str = Field(min_length=1, max_length=FILE_CHARS)
    entity: uuid.UUID | None = None
    answers: dict[str, Any] = Field(default_factory=dict)


def _table_connect(ctx: Ctx, p: TableConnect) -> Done:
    from app.finance.contracts.service import today
    from app.finance.intake import compare

    out, before = compare.connect(
        ctx.session, ctx.workspace, ctx.access, _actor(ctx), _file(p.name, p.data), p.name, today=today(), entity=p.entity,
        answers=p.answers,
    )
    return Done(title=f"сводная таблица компании - файл «{p.name}»", result=out, steps=[Step("entry_summary", {"before": before})])


class LinkSwitch(_Params):
    """«Переход»: звено нашего юрлица - из KORT или снова из таблицы."""

    link: Literal["bills", "payments"]
    entity: uuid.UUID
    source: Literal["kort", "book"]


def _link_switch(ctx: Ctx, p: LinkSwitch) -> Done:
    from app.finance.core import switch

    before = switch.sources(ctx.session, ctx.workspace.id).get(p.link, {}).get(str(p.entity), "book")
    out = switch.set_source(ctx.session, ctx.workspace, ctx.access, _actor(ctx), link=p.link, entity_id=p.entity, source=p.source)
    switch.forget(ctx.workspace.id)
    words = {"kort": "из KORT", "book": "снова из таблицы"}
    return Done(
        title=f"{switch.LINK_TITLES[p.link]} юрлица - {words[p.source]}", result={"sources": out},
        steps=[Step("entry_link", {"link": p.link, "entity_id": str(p.entity), "before": before})],
    )


class StatementApply(_Params):
    name: str = Field(min_length=1, max_length=255)
    data: str = Field(min_length=1, max_length=FILE_CHARS)
    #: Ответ на «как записаны даты».
    date_order: Literal["dmy", "mdy"] | None = None
    #: Счёт всей выписки - id или название; или новый счёт нашего юрлица.
    account: str | None = Field(default=None, max_length=200)
    new_account: dict[str, Any] | None = None
    #: Счета из файла, которых нет в справочнике: название → наше юрлицо.
    accounts: dict[str, uuid.UUID] = Field(default_factory=dict)


def _statement_apply(ctx: Ctx, p: StatementApply) -> Done:
    from app.finance import history
    from app.finance import service as finance_service
    from app.finance.core import entry
    from app.finance.importing import ImportError_, analyze

    actor = _actor(ctx)
    raw = _file(p.name, p.data)
    accounts = {account.id: account for account in finance_service.list_accounts(ctx.session, ctx.workspace.id)}
    chosen = None
    if p.account:
        try:
            chosen = accounts.get(uuid.UUID(p.account))
        except ValueError:
            chosen = next((item for item in accounts.values() if item.name.casefold() == p.account.strip().casefold()), None)
        if chosen is None:
            raise CommandError(f"Счёта «{p.account}» нет")
    elif p.new_account:
        try:
            entity = uuid.UUID(str(p.new_account.get("entity")))
        except ValueError as exc:
            raise CommandError("new_account: entity - наше юрлицо") from exc
        chosen = entry.add_account(
            ctx.session, ctx.workspace, ctx.access, actor, entity_id=entity, name=str(p.new_account.get("name") or ""),
            number=str(p.new_account.get("number") or ""), currency=p.new_account.get("currency"),
        )
        accounts[chosen.id] = chosen
    def read() -> Any:
        try:
            return analyze(
                raw, p.name, [item.name for item in finance_service.list_accounts(ctx.session, ctx.workspace.id)],
                date_order=p.date_order, default_account=chosen.name if chosen is not None else None,
                account_numbers=finance_service.account_numbers(ctx.session, ctx.workspace.id),
            )
        except ImportError_ as exc:
            raise CommandError(str(exc)) from exc

    preview = read()
    if preview.question:
        raise Question(f"{preview.question.get('title')}: {preview.question.get('text')}", preview.question)
    missing = [name for name in preview.accounts_missing if name not in p.accounts]
    if missing:
        names = ", ".join(f"«{name}»" for name in missing)
        raise Question(
            f"Счетов {names} нет в справочнике - чьи они из наших юрлиц? (accounts: название → юрлицо)",
            {"kind": "accounts", "title": "Чьи это счета?", "names": missing},
        )
    named = [name for name in preview.accounts_missing if name in p.accounts]
    for name in named:
        entry.add_account(ctx.session, ctx.workspace, ctx.access, actor, entity_id=p.accounts[name], name=name)
    if named:
        # Строки с незаведённым счётом разбор не готовит: со счетами - разобрать заново.
        preview = read()
    batch = finance_service.save_preview(ctx.session, ctx.workspace, preview, actor=actor.email)
    done = finance_service.apply_batch(ctx.session, ctx.workspace, batch.id, actor=actor.email, only_lines=None, create_dictionaries=True)
    history.write(
        ctx.session, ctx.workspace, kind="import.apply", entity="import_batch", entity_id=batch.id,
        title=f"загрузка «{p.name}» · заведено {done.get('imported', 0)}",
        after={key: done.get(key) for key in ("imported", "failed", "skipped", "duplicate", "total")}, actor=actor.email,
    )
    return Done(
        title=f"выписка «{p.name}»: заведено {done.get('imported', 0)}",
        result={"batch_id": str(batch.id), **{key: done.get(key) for key in ("imported", "failed", "skipped", "duplicate", "total")}},
        steps=[Step("entry_import", {"batch_id": str(batch.id)})] if done.get("imported") else [],
    )


class FilePart(_Params):
    name: str = Field(min_length=1, max_length=255)
    data: str = Field(min_length=1, max_length=FILE_CHARS)


class OnecFiles(_Params):
    #: Журналы реализаций и счетов, контрагенты, ОСВ по 1210 - отчёты 1С файлами.
    files: list[FilePart] = Field(min_length=1, max_length=12)


def _onec_files(ctx: Ctx, p: OnecFiles) -> Done:
    from app.finance import packs
    from app.finance.core import onec_start
    from app.finance.onec import files as onec_files

    upload = onec_files.read([(item.name, _file(item.name, item.data)) for item in p.files], packs.country_of(ctx.workspace).ids)
    out = onec_start.from_files(ctx.session, ctx.workspace, ctx.access, _actor(ctx), upload)
    names = ", ".join(item["organization"] for item in out["bases"].values()) or "организаций не найдено"
    return Done(title=f"1С файлами: {names}", result=out)


ENTRY_COMMANDS: tuple[Command, ...] = (
    Command("participant.create", "Новый участник (клиент, поставщик)", "contracts", ParticipantCreate, _participant_create),
    Command("participant.update", "Участник: полное имя, БИН, написание", "contracts", ParticipantUpdate, _participant_update),
    Command("entity.create", "Наше юрлицо", "contracts", EntityCreate, _entity_create, setup=True),
    Command("account.create", "Счёт нашего юрлица", "dictionaries", AccountCreate, _account_create),
    Command("accounting.start", "Начало учёта оплат", "contracts", AccountingStart, _accounting_start, setup=True),
    Command("payments.source", "Откуда оплаты юрлица: журнал или 1С", "contracts", PaymentsSource, _payments_source, setup=True),
    Command("opening.set", "Входящее сальдо договора на начало учёта", "contracts", OpeningSet, _opening_set,
            lambda p: [str(p.contract)], setup=True),
    Command("payment.record", "Оплата руками", "journal", PaymentRecord, _payment_record, lambda p: [str(p.contract)] if p.contract else []),
    Command("payment.update", "Правка оплаты", "journal", PaymentUpdate, _payment_update),
    Command("payment.delete", "Удалить оплату", "journal", PaymentDelete, _payment_delete),
    Command("payment.decide", "Оплата: к договору, не по договору, снять решение", "contracts", PaymentDecide, _payment_decide,
            lambda p: [str(p.contract)] if p.contract else []),
    Command("payment.split", "Разделить оплату между договорами", "contracts", PaymentSplit, _payment_split,
            lambda p: [str(part.contract) for part in p.parts]),
    Command("document.record", "Счёт, АВР или ЭСФ месяца договора", "contracts", DocumentRecord, _document_record,
            lambda p: [str(p.contract)]),
    Command("document.remove", "Снять документ месяца", "contracts", DocumentRemove, _document_remove),
    Command("table.apply", "Таблица Excel любой формы: договоры, сальдо, помесячные", "contracts", TableApply, _table_apply),
    Command("table.create", "Договоры из таблицы «договор за месяц», которых нет в KORT", "contracts", TableCreate, _table_create),
    Command("table.connect", "Таблица «договор за месяц» файлом - сводная компании", "contracts", TableConnect, _table_connect,
            setup=True),
    Command("link.switch", "«Переход»: звено юрлица из KORT или снова из таблицы", "contracts", LinkSwitch, _link_switch, setup=True),
    Command("statement.apply", "Выписка банка", "import", StatementApply, _statement_apply),
    Command("onec.files", "1С файлами: журналы, контрагенты, ОСВ", "contracts", OnecFiles, _onec_files, setup=True),
)


__all__ = ["ENTRY_COMMANDS", "FILE_CHARS", "Question"]
