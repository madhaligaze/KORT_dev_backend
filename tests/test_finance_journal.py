"""Журнал финансистов: строка выписки - строка журнала, и деньги доходят до договора.

Набор держит то, на чём журнал BBC стоит годами, и то, что легко сломать
молча:

* журнал BBC читается как есть - шапка с переносами и пробелами, дата
  «пн 01.06.26», «Комментарии», «№Дог.», «ОПиУ период», «Дробление»;
* перевод между своими счетами из двух строк (выписка каждого счёта) - один
  перевод, а не приход и расход по миллиону; половинка дописывается второй
  выпиской;
* части «Дробления» - отдельные записи, сумма частей = сумма выписки;
* номер договора из назначения - только при единственном договоре юрлица;
* повторная загрузка не двоит, выписка поверх журнала - «возможный повтор»;
* правка в карточке и в таблице - одна и та же правка, сторона меняется;
* «Оплачено» договора реестра берёт номер договора из журнала, возврат
  клиенту уменьшает оплату;
* стресс: шесть тысяч строк - разбор, принятие и чтение без минутных пауз.
"""
from __future__ import annotations

import io
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal

import openpyxl
import pytest
import sqlalchemy as sa

from app.finance import grid, journal, live, service
from app.finance.contracts import payments, setup
from app.finance.contracts import service as contracts
from app.finance.db import finance_session
from app.finance.models import Account, Category, ImportRow, Operation

FULL = contracts.Access(view=True, edit=True, setup=True)
OWNER = contracts.Actor(uuid.uuid4(), "owner@test")

#: Шапка журнала BBC ровно как в листе: пробелы по краям, переносы, точки.
BBC_HEAD = [
    "ДДС Мес (цифра)", "ОПиУ период", "Дата", "Счет", " Приход ", " Расход ", " Контрагент ",
    " Дробление\n1 суммы ", " №Дог. ", " Подкатегория ", " ФОТ/Детали ", " Проект ", " Категория ",
    " Комментарии ", "Доп Комменты\n/Вопросы", ".", ".", ".", " ДДС Мсяц & год ", "Месяц ОПиу&Контрагент",
    "Дата\nДДС (дубль)", " Формула\n(через АПП Скрипт) ", "Продукт", "Л/Б/П", "Фирма", "ДДС\nГод", "ДДС Месяц",
    "Код договора",
]
KASPI_HR = 'Kaspi Юр. ТОО "BBC HR"'
DEPO_HR = 'Депозит ТОО "BBC HR"'
KASPI_BBC = 'Kaspi Юр. ТОО "Big Business Consulting"'


def bbc_row(
    day: str,
    account: str,
    *,
    income: str = "",
    expense: str = "",
    party: str = "",
    split: str = "",
    contract: str = "",
    sub: str = "",
    details: str = "",
    group: str = "",
    comment: str = "",
    note: str = "",
    pnl: str = "",
) -> list[str]:
    row = [""] * len(BBC_HEAD)
    row[1], row[2], row[3], row[4], row[5], row[6] = pnl, day, account, income, expense, party
    row[7], row[8], row[9], row[10], row[12], row[13], row[14] = split, contract, sub, details, group, comment, note
    # Производные колонки BBC - их разбор не читает.
    row[18], row[20], row[24] = "ИЮНЬ 2026", day, "ТОО"
    return row


def book_of(rows: list[list[str]], accounts: list[tuple[str, str]] | None = None) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Журнал"
    sheet.append(BBC_HEAD)
    sheet.append(["12", "", "1"] + [""] * (len(BBC_HEAD) - 3))  # служебная строка BBC
    for row in rows:
        sheet.append(row)
    if accounts:
        reference = workbook.create_sheet("Справочник")
        reference.append(["Подкатегория", "Категория", "", "Счета", "", "Наши ТОО"])
        for name, firm in accounts:
            reference.append(["", "", "", name, "Kaspi Юр.", firm])
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    yield engine


@pytest.fixture
def space(finance_db):
    """Группа из двух юрлиц и договоры юристов: так журнал BBC и встречает финансиста."""
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        setup.add_entity(session, workspace, name="BBC HR", code="BBC HR")
        setup.add_entity(session, workspace, name="Big Business Consulting", code="BBC")
        contracts.create(session, workspace, FULL, OWNER, {
            "executor": "BBC HR", "customer": "ТОО Regardal Hotel", "number": "№ОБО/14",
            "type": "Абонентское обслуживание", "amount": "400 000", "signed_at": "01.12.2025",
        })
        contracts.create(session, workspace, FULL, OWNER, {
            "executor": "Big Business Consulting", "customer": "ТОО RAEG Construction", "number": "№ОБО-31",
            "type": "Разовая услуга", "amount": "1 000 000", "signed_at": "01.03.2026",
        })
        # Тот же номер у другого юрлица - номер без юрлица не решает.
        contracts.create(session, workspace, FULL, OWNER, {
            "executor": "BBC HR", "customer": "ТОО Другой", "number": "№ОБО-31",
            "type": "Разовая услуга", "amount": "5 000", "signed_at": "01.03.2026",
        })
        return workspace.id


def _ws(session, space_id):
    return service.get_workspace(session, space_id)


def _upload(space_id, data: bytes, name: str = "Журнал.xlsx") -> dict:
    with finance_session() as session:
        workspace = _ws(session, space_id)
        preview, extra = journal.analyze_file(session, workspace, data, name)
        batch = service.save_preview(session, workspace, preview)
        batch.decisions = {**(batch.decisions or {}), "accounts_new": extra["accounts_new"], "journal_source": extra["source"]}
        session.flush()
        return journal.preview_payload(session, workspace, batch)


def _accept(space_id, batch_id: str) -> dict:
    with finance_session() as session:
        return journal.accept(session, _ws(session, space_id), uuid.UUID(batch_id))


BASIC = [
    bbc_row("пн 01.06.26 ", KASPI_HR, expense="95 323,00", party='АО "Kaspi Bank"', sub="ФОТ", group="ФОТ",
            comment="Оплата заработной платы", pnl="Старые (до Май/Июня 2026)"),
    # Перевод: строка выписки Kaspi и строка выписки депозита - один перевод.
    bbc_row("пн 01.06.26 ", KASPI_HR, income="614 920,00", party='ТОО "BBC HR"', sub="Текущий счет - Депозит",
            group="Перемещения", comment="Перевод со счета U36315864-002 на счет KaspiPay", pnl="ИЮНЬ 2026"),
    bbc_row("пн 01.06.26 ", DEPO_HR, expense="614 920,00", party="Перевод", sub="Текущий счет - Депозит",
            group="Перемещения", comment="Перевод на KaspiPay"),
    # Номер договора - в назначении: в реестре он один у BBC HR.
    bbc_row("пн 01.06.26 ", KASPI_HR, income="400 000,00", party="ТОО Regardal Hotel", sub="Доходы", group="Доходы",
            comment="За услуги по ведению бухгалтерского учета за апрель 2026г. по договору №ОБО/14 от 01.12.2025",
            pnl="АПРЕЛЬ 2026"),
    # Дробление: одна сумма выписки на два месяца ОПиУ.
    bbc_row("чт 04.06.26 ", KASPI_BBC, income="100 000,00", party="ТОО RAEG Construction", split="Общий 400,000тг",
            contract="№ОБО-31", sub="Доходы", group="Доходы", comment="Оплата по счёту 242", pnl="Старые (до Май/Июня 2026)"),
    bbc_row("чт 04.06.26 ", KASPI_BBC, income="300 000,00", party="ТОО RAEG Construction", split="Общий 400,000тг",
            contract="№ОБО-31", sub="Доходы", group="Доходы", comment="Оплата по счёту 242", pnl="ИЮНЬ 2026"),
    # Половинка: выписки Pay в журнале нет.
    bbc_row("вт 02.06.26 ", KASPI_BBC, income="140 000,00", sub="Пэй - Юр счет", group="Перемещения",
            comment="Перевод с Kaspi Pay", note="сверка с ОП", pnl="2027 год"),
    bbc_row("вт 02.06.26 ", KASPI_BBC, expense="1 950,00", party='АО "KASPI BANK"', sub="Обсл. банка",
            comment="Комиссия за ведение счета", details="тариф Kaspi Pay"),
]
REFERENCE = [(KASPI_HR, 'ТОО "BBC HR"'), (DEPO_HR, 'ТОО "BBC HR"'), (KASPI_BBC, 'ТОО "Big Business Consulting"')]


def test_zhurnal_bbc_chitaetsya_kak_est(space):
    payload = _upload(space, book_of(BASIC, REFERENCE))
    assert payload["counts"]["ready"] == 7, [row["problems"] for row in payload["rows"]]
    assert payload["counts"]["skipped"] == 2  # служебная строка BBC и вторая сторона перевода
    assert {item["name"] for item in payload["accounts_new"]} == {KASPI_HR, DEPO_HR, KASPI_BBC}
    assert all(item["firm_id"] for item in payload["accounts_new"]), "юрлицо счёта - из «Наших ТОО» справочника"
    by_line = {row["line"]: row for row in payload["rows"]}
    # Переводом становится строка, откуда ушли деньги; строка прихода - его вторая сторона.
    transfer = by_line[5]["cells"]
    assert transfer["account"] == f"{DEPO_HR} → {KASPI_HR}" and transfer["category"] == "Текущий счет - Депозит"
    assert by_line[4]["state"] == "skipped" and by_line[4]["paired_with"] == 5
    assert by_line[6]["cells"]["contract_ref"] == "№ОБО/14" and by_line[6]["contract_auto"]
    assert by_line[6]["cells"]["pnl"] == "Апрель 2026"
    assert by_line[3]["cells"]["pnl"] == "Старые (до Май/Июня 2026)"
    assert by_line[7]["cells"]["split"] == "Общий 400 000"

    done = _accept(space, payload["batch_id"])
    assert done["imported"] == 7 and done["failed"] == 0
    with finance_session() as session:
        workspace = _ws(session, space)
        ops = {(item.kind, str(item.amount)): item for item in session.scalars(sa.select(Operation))}
        assert len(ops) == 7
        move = ops[("transfer", "614920.00")]
        accounts = {item.id: item.name for item in session.scalars(sa.select(Account))}
        assert (accounts[move.account_from_id], accounts[move.account_to_id]) == (DEPO_HR, KASPI_HR)
        half = ops[("transfer", "140000.00")]
        assert half.account_to_id and half.account_from_id is None and half.note == "сверка с ОП"
        assert (half.period_start, half.period_end) == (date(2027, 1, 1), date(2027, 12, 31))
        income = ops[("income", "400000.00")]
        assert income.contract_ref == "№ОБО/14" and income.accrued_at == date(2026, 4, 1)
        fot = ops[("expense", "95323.00")]
        assert fot.period_label == "Старые (до Май/Июня 2026)" and fot.accrued_at is None
        parts = [item for item in ops.values() if item.split_group]
        assert len(parts) == 2 and {item.split_total for item in parts} == {Decimal("400000.00")}
        categories = {item.name: item for item in session.scalars(sa.select(Category))}
        assert categories["Текущий счет - Депозит"].side == "transfer"
        assert categories["Доходы"].nature == "revenue" and categories["ФОТ"].side == "expense"
        listing = journal.list_records(session, workspace, journal.JournalFilter(month="2026-06"))
        assert listing["sums"]["income"] == "800000.00"  # переводы - не доход
        assert listing["sums"]["expense"] == "97273.00"
        assert listing["attention"]["half"] == 1 and listing["attention"]["question"] == 1
        record = next(item for item in listing["items"] if item["amount"] == "400000.00")
        assert record["contract"]["number"] == "№ОБО/14" and record["contract"]["paid"] == "400000.00"
        assert record["pnl_title"] == "Апрель 2026" and record["firm"] == "BBC HR"
        balances = {item["name"]: item for item in listing["balances"]}
        assert balances[KASPI_HR]["closing"] == "919597.00"  # 614 920 + 400 000 - 95 323
        assert balances[DEPO_HR]["closing"] == "-614920.00"


def test_den_nedeli_otvechaet_na_poryadok_dat(space):
    """«пн 01.06.26» - 1 июня (понедельник), а не 6 января (вторник): вопроса нет."""
    with finance_session() as session:
        workspace = _ws(session, space)
        preview, _extra = journal.analyze_file(session, workspace, book_of(BASIC, REFERENCE), "Журнал.xlsx")
        assert preview.question is None
        assert preview.rows[2].values["paid_at"] == "2026-06-01"
        # Без дня недели те же даты неоднозначны - тогда спрашиваем, а не угадываем.
        plain = [bbc_row("01.06.2026", KASPI_HR, income="1 000"), bbc_row("02.06.2026", KASPI_HR, income="2 000")]
        preview, _extra = journal.analyze_file(session, workspace, book_of(plain), "Без дней.xlsx")
        assert preview.question and preview.question["kind"] == "date_order"


def test_povtornaya_zagruzka_ne_dvoit_i_vypiska_poverh_zhurnala_povtor(space):
    data = book_of(BASIC, REFERENCE)
    _accept(space, _upload(space, data)["batch_id"])
    again = _upload(space, data)
    assert again["counts"]["ready"] == 0 and again["counts"]["duplicate"] == 7
    # Выписка Kaspi HR - то же самое другим текстом: возможный повтор, не новая запись.
    statement = book_of([
        bbc_row("01.06.2026", KASPI_HR, income="400 000", party="TOO REGARDAL HOTEL",
                comment="Оплата бух. услуг (выписка банка)"),
        bbc_row("05.06.2026", KASPI_HR, income="10 000", party="ТОО Новый", comment="Новая оплата"),
    ])
    payload = _upload(space, statement, "Выписка.xlsx")
    rows = {row["line"]: row for row in payload["rows"]}
    assert rows[3]["state"] == "duplicate" and rows[3]["maybe_duplicate"]
    assert rows[4]["state"] == "imported"
    with finance_session() as session:
        # «Завести всё равно» - решение человека.
        row = journal.fix_row(session, _ws(session, space), uuid.UUID(payload["batch_id"]), 3, {"force": True})
        assert row.state == "imported"
    assert _accept(space, payload["batch_id"])["imported"] == 2


def test_polovinka_dopisyvaetsya_vtoroy_vypiskoy(space):
    _accept(space, _upload(space, book_of(BASIC, REFERENCE))["batch_id"])
    with finance_session() as session:
        workspace = _ws(session, space)
        pay_id = service.create_account(session, workspace, name="Pay BBC").id
    second = book_of([
        bbc_row("02.06.2026", "Pay BBC", expense="140 000", sub="Пэй - Юр счет", group="Перемещения",
                comment="На юр. счёт"),
    ])
    payload = _upload(space, second, "Pay.xlsx")
    assert payload["counts"]["ready"] == 1
    done = _accept(space, payload["batch_id"])
    assert done["completed"] == 1 and done["imported"] == 0
    with finance_session() as session:
        half = session.scalar(sa.select(Operation).where(Operation.kind == "transfer", Operation.amount == Decimal("140000")))
        assert half.account_from_id == pay_id and half.account_to_id is not None
        assert session.scalar(sa.select(sa.func.count(Operation.id)).where(Operation.kind == "transfer")) == 2


def test_pravka_v_predprosmotre(space):
    payload = _upload(space, book_of(BASIC, REFERENCE))
    batch = uuid.UUID(payload["batch_id"])
    with finance_session() as session:
        workspace = _ws(session, space)
        # Сумма переехала из «Расхода» в «Приход» - строка стала приходом.
        row = journal.fix_row(session, workspace, batch, 10, {"income": "1 950", "pnl": "Июль 2026", "category": "Прочие доходы"})
        values = row.parsed
        assert values["kind"] == "income" and values["account_to"] == KASPI_BBC and values["account_from"] is None
        assert values["period_start"] == "2026-07-01" and values["subcategory"] == "Прочие доходы"
        with pytest.raises(service.FinanceError):
            journal.fix_row(session, workspace, batch, 10, {"income": "5", "expense": "5"})
        with pytest.raises(service.FinanceError):
            journal.fix_row(session, workspace, batch, 10, {"bogus": "1"})
        journal.reject(session, workspace, batch)
        with pytest.raises(service.FinanceError):
            journal.accept(session, workspace, batch)
        assert session.scalar(sa.select(sa.func.count(Operation.id))) == 0


def test_pravka_zapisi_menyaet_storonu_i_vid(space):
    _accept(space, _upload(space, book_of(BASIC, REFERENCE))["batch_id"])
    with finance_session() as session:
        workspace = _ws(session, space)
        fee = session.scalar(sa.select(Operation).where(Operation.amount == Decimal("1950")))
        fee_id, account = fee.id, fee.account_from_id
        # Расход → приход: сумма в другой колонке, счёт остаётся.
        changed = journal.edit(session, workspace, fee_id, {"income": "1 950", "category": "Прочие доходы"}, version=fee.version)
        assert changed.kind == "income" and changed.account_to_id == account and changed.account_from_id is None
        assert session.get(Category, changed.category_id).side == "income"
        # Статья-перемещение - половинка перевода; статья прихода - снова приход.
        changed = journal.edit(session, workspace, fee_id, {"category": "Текущий счет - Депозит"})
        assert changed.kind == "transfer" and changed.account_to_id == account and changed.counterparty_id is None
        changed = journal.edit(session, workspace, fee_id, {"category": "Доходы", "contract_ref": "№ОБО-31", "pnl": "Старые"})
        assert changed.kind == "income" and changed.period_label == "Старые" and changed.contract_ref == "№ОБО-31"
        with pytest.raises(service.VersionConflict):
            journal.edit(session, workspace, fee_id, {"note": "x"}, version=1)
        move = session.scalar(sa.select(Operation).where(Operation.amount == Decimal("614920")))
        with pytest.raises(service.FinanceError, match="перевод между двумя"):
            journal.edit(session, workspace, move.id, {"kind": "income"})


def test_droblenie_skhoditsya_do_kopeyki(space):
    _accept(space, _upload(space, book_of(BASIC, REFERENCE))["batch_id"])
    with finance_session() as session:
        workspace = _ws(session, space)
        income = session.scalar(sa.select(Operation).where(Operation.amount == Decimal("400000"), Operation.contract_ref == "№ОБО/14"))
        with pytest.raises(service.FinanceError, match="должно сойтись"):
            journal.split(session, workspace, income.id, [{"amount": "100000"}, {"amount": "200000"}])
        parts = journal.split(
            session, workspace, income.id,
            [{"amount": "100 000", "pnl": "Апрель 2026"}, {"amount": "300 000,00", "pnl": "Май 2026", "note": "за май"}],
        )
        assert [str(item.amount) for item in parts] == ["100000.00", "300000.00"]
        assert parts[0].external_key and parts[1].external_key is None  # повторная выписка узнает строку
        assert parts[1].period_start == date(2026, 5, 1) and parts[1].contract_ref == "№ОБО/14"
        head = journal.unsplit(session, workspace, parts[0].split_group)
        assert head.amount == Decimal("400000") and head.split_group is None
        assert session.get(Operation, parts[1].id).deleted_at is not None


def test_tablichnyy_vid_perevod_dvumya_strokami(space):
    _accept(space, _upload(space, book_of(BASIC, REFERENCE))["batch_id"])
    with finance_session() as session:
        workspace = _ws(session, space)
        payload = journal.grid_payload(session, workspace, journal.JournalFilter(month="2026-06"))
        keys = [column["key"] for column in payload["columns"]]
        assert keys[:5] == ["pnl", "paid_at", "account", "income", "expense"]
        move = session.scalar(sa.select(Operation).where(Operation.amount == Decimal("614920")))
        lines = [row for row in payload["rows"] if row["operation_id"] == str(move.id)]
        assert {row["id"] for row in lines} == {f"{move.id}:out", f"{move.id}:in"}
        incoming = next(row for row in lines if row["side"] == "in")
        assert incoming["cells"]["account"] == KASPI_HR and incoming["cells"]["income"] == "614920.00"
        assert incoming["cells"]["group"] == "Перемещения"
        # Правка суммы на любой стороне - правка одного перевода.
        grid.apply_cell(session, workspace, f"{move.id}:in", "income", "614 000")
        assert session.get(Operation, move.id).amount == Decimal("614000")
        with pytest.raises(service.FinanceError):
            grid.apply_cell(session, workspace, f"{move.id}:in", "expense", "1")
        with pytest.raises(service.FinanceError):
            grid.apply_cell(session, workspace, f"{move.id}:in", "group", "x")
        # Новая строка внизу листа - запись журнала.
        created = grid.append_row(session, workspace, {
            "paid_at": "05.06.2026", "account": KASPI_HR, "expense": "12 500", "category": "Интернет",
            "counterparty": "ТОО Мобайл", "pnl": "Июнь 2026", "note": "счёт не пришёл",
        })
        assert created.kind == "expense" and created.note == "счёт не пришёл"
        # Перевод стал половинкой - лист снимает строку ушедшей стороны.
        journal.edit(session, workspace, move.id, {"account_from": ""})
        assert f"{move.id}:out" in journal.stale_line_ids(session.get(Operation, move.id))


def test_zhivoy_rezhim_vidit_pravku_iz_zapisey(space):
    _accept(space, _upload(space, book_of(BASIC, REFERENCE))["batch_id"])
    with finance_session() as session:
        workspace = _ws(session, space)
        since = live.current_seq(session, workspace.id)
        fee = session.scalar(sa.select(Operation).where(Operation.amount == Decimal("1950")))
        fee_id = fee.id
    with finance_session() as session:
        journal.edit(session, _ws(session, space), fee_id, {"note": "уточнить у банка"})
    with finance_session() as session:
        changes = live.changes(session, _ws(session, space), since)
        assert [row["id"] for row in changes["rows"]] == [str(fee_id)]
        assert changes["rows"][0]["cells"]["note"] == "уточнить у банка"


def test_oplacheno_dogovora_po_nomeru_i_vozvrat(space):
    _accept(space, _upload(space, book_of(BASIC, REFERENCE))["batch_id"])
    with finance_session() as session:
        workspace = _ws(session, space)
        registry = contracts.Registry(session, workspace)
        everything = list(session.scalars(sa.select(contracts.Contract)))
        by_number = {(item.number, item.amount): item for item in everything}
        raeg = by_number[("№ОБО-31", Decimal("1000000"))]
        other = by_number[("№ОБО-31", Decimal("5000"))]
        summary = payments.summaries(session, workspace, FULL)["contracts"]
        # Номер решил у юрлица счёта: тот же номер у BBC HR оплат не получил.
        assert summary[str(raeg.id)]["paid"] == "400000.00" and summary[str(raeg.id)]["remaining"] == "600000.00"
        assert str(other.id) not in summary
        # Возврат клиенту с номером договора уменьшает «Оплачено».
        account = session.scalar(sa.select(Account).where(Account.name == KASPI_BBC))
        service.create_operation(session, workspace, service.OperationInput(
            kind="expense", paid_at=date(2026, 6, 20), amount=Decimal("50000"), account_from_id=account.id,
            contract_ref="NºОБО-31",
        ))
        allocation = payments.allocate(session, workspace, registry, everything)
        hows = sorted(how for _op, how in allocation.paid[raeg.id])
        assert hows == ["number", "number", "refund"]
        assert payments.summaries(session, workspace, FULL)["contracts"][str(raeg.id)]["paid"] == "350000.00"


def test_ref_key_latinica_i_razdeliteli():
    assert journal.ref_key("NºHO/40") == journal.ref_key("№НО/40")
    assert journal.ref_key("№ ЮО-59") != journal.ref_key("№ЮО/59")
    assert journal.refs_in_text("по ДОГОВОРУ N ОКР/15 на возмездное") == [journal.ref_key("ОКР/15")]
    assert journal.refs_in_text("согласно договора № Договор №02 от 17 февраля") == []


def test_shablon_tuda_i_obratno(space):
    _accept(space, _upload(space, book_of(BASIC, REFERENCE))["batch_id"])
    with finance_session() as session:
        workspace = _ws(session, space)
        template = journal.template_xlsx(session, workspace)
        exported = journal.export_xlsx(session, workspace, journal.JournalFilter(month="2026-06"))
    book = openpyxl.load_workbook(io.BytesIO(template))
    assert book.sheetnames == ["Журнал", "Справочник", "Как заполнять"]
    head = [cell.value for cell in book["Журнал"][1]]
    assert head[:5] == ["ОПиУ период", "Дата", "Счёт", "Приход", "Расход"]
    reference = [row for row in book["Справочник"].iter_rows(values_only=True)]
    assert any(row[4] == KASPI_HR and row[5] == "BBC HR" for row in reference[1:])
    # Заполненный шаблон: человек вписал две строки.
    sheet = book["Журнал"]
    sheet.append(["Июль 2026", date(2026, 7, 3), KASPI_HR, 250000, None, "ТОО Новый клиент", None, "№ОБО/14",
                  "Доходы", None, None, None, "Оплата за июль", None])
    sheet.append([None, date(2026, 7, 3), "Касса", None, 5000, "Магазин", None, None, "Хозтовары", None, None, None,
                  "Вода", "какой чек?"])
    stream = io.BytesIO()
    book.save(stream)
    payload = _upload(space, stream.getvalue(), "Шаблон.xlsx")
    assert payload["counts"]["ready"] == 2, [row["problems"] for row in payload["rows"]]
    done = _accept(space, payload["batch_id"])
    assert done["imported"] == 2
    # Выгрузка - тот же шаблон: загруженная обратно, вся - повторы.
    back = _upload(space, exported, "Выгрузка.xlsx")
    assert back["counts"]["ready"] == 0, [row for row in back["rows"] if row["state"] == "imported"][:2]
    assert back["counts"]["duplicate"] >= 6


def _stress_book(rows: int) -> bytes:
    """Журнал на `rows` строк: приходы, расходы, пары переводов, дробления - как у BBC за полгода."""
    workbook = openpyxl.Workbook(write_only=True)
    sheet = workbook.create_sheet("Журнал")
    sheet.append(BBC_HEAD)
    accounts = [f'Kaspi Юр. ТОО "Фирма {index}"' for index in range(8)] + [f'Депозит ТОО "Фирма {index}"' for index in range(8)]
    start = date(2026, 6, 1)
    written = 0
    index = 0
    while written < rows:
        day = (start + timedelta(days=index % 150)).strftime("%d.%m.%Y")
        firm = index % 8
        kind = index % 10
        if kind < 4:
            sheet.append(bbc_row(day, accounts[firm], income=f"{1000 + index},00", party=f"ТОО Клиент {index % 300}",
                                 contract=f"№ОБО/{index % 200}", sub="Доходы", group="Доходы",
                                 comment=f"Оплата по счёту {index}", pnl="ИЮНЬ 2026"))
            written += 1
        elif kind < 8:
            sheet.append(bbc_row(day, accounts[firm], expense=f"{500 + index},50", party=f"ТОО Поставщик {index % 120}",
                                 sub=["ФОТ", "Налоги Общ.", "Интернет", "Аренда Офиса"][kind - 4],
                                 comment=f"Платёж {index}"))
            written += 1
        elif kind == 8:
            amount = f"{20000 + index},00"
            sheet.append(bbc_row(day, accounts[firm], expense=amount, sub="Текущий счет - Депозит", group="Перемещения",
                                 comment=f"На депозит {index}"))
            sheet.append(bbc_row(day, accounts[firm + 8], income=amount, sub="Текущий счет - Депозит", group="Перемещения",
                                 comment=f"С текущего {index}"))
            written += 2
        else:
            mark = f"Общий {2 * (3000 + index)}тг"
            for pnl in ("Старые (до Май/Июня 2026)", "ИЮНЬ 2026"):
                sheet.append(bbc_row(day, accounts[firm], income=f"{3000 + index},00", party=f"ТОО Клиент {index % 300}",
                                     split=mark, sub="Доходы", group="Доходы", comment=f"Две части {index}", pnl=pnl))
            written += 2
        index += 1
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def test_stress_shest_tysyach_strok(space):
    """Журнал за полгода одной загрузкой: разбор, принятие, чтение месяца и листа - секунды, не минуты."""
    data = _stress_book(6000)
    started = time.perf_counter()
    payload = _upload(space, data)
    parsed = time.perf_counter() - started
    assert payload["counts"]["failed"] == 0, [row["problems"] for row in payload["rows"] if row["state"] == "failed"][:3]
    started = time.perf_counter()
    done = _accept(space, payload["batch_id"])
    accepted = time.perf_counter() - started
    pairs = sum(1 for row in payload["rows"] if row["state"] == "skipped" and row["paired_with"])
    assert done["imported"] == payload["counts"]["ready"] and pairs >= 500
    with finance_session() as session:
        workspace = _ws(session, space)
        total = session.scalar(sa.select(sa.func.count(Operation.id)))
        assert total == done["imported"]
        transfers = session.scalar(
            sa.select(sa.func.count(Operation.id)).where(
                Operation.kind == "transfer", Operation.account_from_id.is_not(None), Operation.account_to_id.is_not(None)
            )
        )
        assert transfers == pairs  # каждая пара строк - один перевод с обоими счетами
        started = time.perf_counter()
        month = journal.list_records(session, workspace, journal.JournalFilter(month="2026-07"))
        sheet = journal.grid_payload(session, workspace, journal.JournalFilter(month="2026-07"))
        read = time.perf_counter() - started
        assert month["total"] > 800 and len(sheet["rows"]) > month["total"]  # переводы - по две строки
        groups = session.scalar(sa.select(sa.func.count(sa.distinct(Operation.split_group))))
        assert groups >= 500
    print(f"разбор {parsed:.1f} с, принятие {accepted:.1f} с, чтение {read:.2f} с")
    # Пороги с запасом на медленную машину: прежняя заводка по строке шла минутами.
    assert parsed < 25, f"разбор {parsed:.1f} с"
    assert accepted < 20, f"принятие {accepted:.1f} с"
    assert read < 8, f"чтение месяца и листа {read:.1f} с"


# ── То же на настоящем Postgres ──────────────────────────────────────────────
#
# SQLite в наборах не проверяет внешние ключи, и строка загрузки со ссылкой на
# ещё не записанную операцию проходила там молча, а на Postgres «Принять»
# падало (живой прогон 04.11.2026). Поэтому путь загрузки идёт и здесь.


@pytest.fixture
def pg_space(postgres_server, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    admin = create_engine(f"{postgres_server}/postgres", isolation_level="AUTOCOMMIT", future=True)
    name = f"journal_{uuid.uuid4().hex[:10]}"
    with admin.connect() as connection:
        connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(f"{postgres_server}/{name}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        setup.add_entity(session, workspace, name="BBC HR", code="BBC HR")
        setup.add_entity(session, workspace, name="Big Business Consulting", code="BBC")
        # Автор без учётки: на Postgres у договора внешний ключ на users.
        contracts.create(session, workspace, FULL, contracts.Actor(None, "owner@test"), {
            "executor": "BBC HR", "customer": "ТОО Regardal Hotel", "number": "№ОБО/14",
            "type": "Абонентское обслуживание", "amount": "400 000", "signed_at": "01.12.2025",
        })
        space_id = workspace.id
    try:
        yield space_id
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_postgres_zagruzka_pravka_droblenie(pg_space):
    payload = _upload(pg_space, book_of(BASIC, REFERENCE))
    done = _accept(pg_space, payload["batch_id"])
    assert done["imported"] == 7 and done["failed"] == 0
    with finance_session() as session:
        workspace = _ws(session, pg_space)
        linked = session.scalar(sa.select(sa.func.count(ImportRow.id)).where(ImportRow.operation_id.is_not(None)))
        assert linked == 7
        income = session.scalar(sa.select(Operation).where(Operation.contract_ref == "№ОБО/14"))
        parts = journal.split(session, workspace, income.id, [{"amount": "150000"}, {"amount": "250000", "pnl": "Май 2026"}])
        journal.edit(session, workspace, parts[1].id, {"category": "Текущий счет - Депозит"})
        listing = journal.list_records(session, workspace, journal.JournalFilter(month="2026-06"))
        assert listing["attention"]["half"] == 2
    # Пара перевода с суммой, записанной по-разному, и половинка, которую
    # дописывает строка той же загрузки: обе - до записи ссылки строки.
    mixed = book_of([
        bbc_row("10.06.2026", KASPI_HR, income="77 000,00", sub="Текущий счет - Депозит", group="Перемещения"),
        bbc_row("10.06.2026", DEPO_HR, expense="77000", sub="Текущий счет - Депозит", group="Перемещения"),
        bbc_row("11.06.2026", KASPI_HR, income="5 000", sub="Текущий счет - Депозит", group="Перемещения"),
        bbc_row("12.06.2026", KASPI_HR, expense="5 000", sub="Текущий счет - Депозит", group="Перемещения"),
    ], REFERENCE)
    payload = _upload(pg_space, mixed, "Пары.xlsx")
    rows = {row["line"]: row for row in payload["rows"]}
    assert rows[3]["state"] == "skipped" and rows[4]["paired_with"] == 3
    assert _accept(pg_space, payload["batch_id"])["failed"] == 0
    # Тот же файл ещё раз - повторы, и на Postgres тоже.
    assert _upload(pg_space, book_of(BASIC, REFERENCE))["counts"]["ready"] == 0
    big = _upload(pg_space, _stress_book(1500))
    assert _accept(pg_space, big["batch_id"])["imported"] == big["counts"]["ready"]
