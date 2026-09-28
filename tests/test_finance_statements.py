"""Выписки банков в «Финансах»: любые форматы, один разбор.

Набор написан после 22 сентября 2026: три настоящие Excel-выписки Kaspi
Business (счета трёх ТОО) не заводились ни одной строкой. Нумерация колонок
«1 2 3 … 9» под шапкой становилась операцией от 1 января 1900 года, подпись
банка под таблицей — отложенными строками, счёт не спрашивался, а «Перевод на
Депозит» лёг бы расходом на 1,2 млн. Тесты названы по симптому; данные в них
вымышленные — личные выписки в репозиторий не кладутся.
"""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from app.finance import service
from app.finance.db import finance_session
from app.finance.importing import analyze
from app.finance.service import FinanceError

FIXTURES = Path(__file__).parent / "fixtures"

KASPI = "KZ87722S000025831219"
DEPOSIT = "KZ25722RU00000141328"
OWNER_BIN = "990140001234"


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    """Своя база на прогон — см. объяснение в `test_finance_import.py`."""
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    yield engine


@pytest.fixture
def workspace(finance_db):
    with finance_session() as session:
        return service.ensure_workspace(session).id


def kaspi_business(ops, *, opening=0, closing=200000, number=KASPI):
    """Выписка Kaspi Business в Excel — как её отдаёт банк."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([])
    ws.append([])
    ws.append(["Текущий счет: ", None, number])
    ws.append(["Валюта счета:", None, "KZT"])
    ws.append(["Период:", None, "15.09.2026 - 15.09.2026"])
    ws.append(["Дата последнего движения:", None, "15.09.2026 21:03"])
    ws.append(["ИИН/БИН: ", None, OWNER_BIN])
    ws.append(["Наименование: ", None, 'ТОО "Пример"'])
    ws.append(["Входящий остаток:", None, opening, None, "KZT"])
    ws.append(["Исходящий остаток:", None, closing, None, "KZT"])
    ws.append([])
    ws.append(
        [
            "№\nдокумента", "Дата операции", "Дебет", "Кредит",
            "Наименование бенефициара / отправителя денег", "ИИК бенефициара / отправителя денег",
            "БИК банка бенефициара (отправителя денег)", "КНП", "Назначение платежа",
        ]
    )
    ws.append([1, 2, 3, 4, 5, 6, 7, 8, 9])
    for op in ops:
        ws.append(op)
    ws.append([None, "Итого обороты в валюте счета", 800000, 1000000])
    ws.append([None, "Итого операций за период", "1", "2"])
    ws.append([])
    ws.append([])
    ws.append([None, "Отчет сформирован пользователем Иванов Иван 16.09.2026 15:43"])
    ws.append([None, 'Наименование и БИК обслуживающего Банка: АО "KASPI BANK" Бик: CASPKZKA'])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


OPS = [
    ["2404", "15.09.2026 21:03:05", None, 200000, 'ТОО "Альфа"\nИИН/БИН 200940008138',
     "KZ30722S000007020647", None, "859", "За бухгалтерские услуги"],
    ["5930", "15.09.2026 19:34:02", 800000, None, f'ТОО "Пример"\nИИН/БИН {OWNER_BIN}',
     DEPOSIT, None, "390", "Перевод со счета Kaspi Pay на Депозит U1-001"],
    ["897", "15.09.2026 12:40:49", None, 800000, "ИП БЕТА\nИИН/БИН 001220601096",
     "KZ43722S000047134763", None, "859", "Оплата по счёту № 46"],
]


# ── Выписка Kaspi Business в Excel ───────────────────────────────────────────


def test_nomer_scheta_v_vypiske_sam_nahodit_schet():
    """Номер счёта из шапки выписки записан у счёта — вопроса нет, строки готовы."""
    preview = analyze(
        kaspi_business(OPS),
        "Выписка_по_счету.xlsx",
        ["Kaspi ТОО", "Депозит ТОО", "Касса"],
        account_numbers={KASPI: "Kaspi ТОО", DEPOSIT: "Депозит ТОО"},
    )
    assert preview.question is None
    assert preview.counts["ready"] == 3
    assert preview.counts["failed"] == 0
    assert preview.bank["account"] == "Kaspi ТОО"
    assert preview.bank["account_by"] == "number"


def test_numeraciya_kolonok_i_podpis_banka_ne_operacii():
    """«1 2 3 … 9» и «Отчёт сформирован…» — пропущены, а не отложены."""
    preview = analyze(kaspi_business(OPS), "в.xlsx", ["Kaspi ТОО"], default_account="Kaspi ТОО")
    skipped = {row.line: row.problems[0]["text"] for row in preview.rows if row.state == "skipped"}
    assert "нумерация колонок" in skipped[13]
    assert any("подпись" in text for text in skipped.values())
    assert not any(row.values.get("paid_at") == "1900-01-01" for row in preview.rows)
    assert all(row.state != "failed" or row.values.get("own_transfer") for row in preview.rows)


def test_perevod_na_svoy_depozit_ne_rashod():
    """БИН контрагента = БИН владельца счёта: это перевод, а не расход в 800 000."""
    preview = analyze(
        kaspi_business(OPS),
        "в.xlsx",
        ["Kaspi ТОО", "Депозит ТОО"],
        account_numbers={KASPI: "Kaspi ТОО", DEPOSIT: "Депозит ТОО"},
    )
    transfer = next(row for row in preview.rows if row.line == 15)
    assert transfer.values["kind"] == "transfer"
    assert transfer.values["account_from"] == "Kaspi ТОО"
    assert transfer.values["account_to"] == "Депозит ТОО"
    # Своя же компания — не контрагент.
    assert transfer.values["counterparty"] is None


def test_depozita_net_v_spravochnike_stroka_zhdyot_s_predlozheniem():
    """Свой счёт, которого нет, — не расход и не молчание, а готовое предложение."""
    preview = analyze(kaspi_business(OPS), "в.xlsx", ["Kaspi ТОО"], default_account="Kaspi ТОО")
    transfer = next(row for row in preview.rows if row.line == 15)
    assert transfer.state == "failed"
    assert transfer.values["kind"] == "transfer"
    assert "между своими счетами" in transfer.problems[0]["text"]
    assert transfer.problems[0]["field"] == "account_to"
    assert preview.accounts_suggested == [
        {"name": "Депозит ·1328", "number": DEPOSIT, "currency": "KZT", "rows": 1}
    ]


def test_bez_nomera_schet_sprashivaetsya_odin_raz_s_predlozheniem_zavesti():
    preview = analyze(kaspi_business(OPS), "в.xlsx", ["Касса"])
    assert preview.question["kind"] == "account"
    assert KASPI in preview.question["text"]
    assert preview.question["create"] == {"name": "Kaspi ·1219", "number": KASPI, "currency": "KZT"}


def test_kontragent_otdelyon_ot_bin():
    preview = analyze(kaspi_business(OPS), "в.xlsx", ["Kaspi ТОО"], default_account="Kaspi ТОО")
    first = next(row for row in preview.rows if row.line == 14)
    assert first.values["counterparty"] == 'ТОО "Альфа"'
    assert first.values["counterparty_bin"] == "200940008138"


def test_sverka_s_bankom_dlya_excel_vypiski(workspace):
    """Остатки из шапки Excel-выписки сверяются так же, как у PDF."""
    with finance_session() as session:
        space = service.ensure_workspace(session)
        service.create_account(session, space, name="Kaspi ТОО", number=KASPI)
        service.create_account(session, space, name="Депозит ТОО", number=DEPOSIT)
        preview = analyze(
            kaspi_business(OPS),
            "в.xlsx",
            [a.name for a in service.list_accounts(session, space.id)],
            account_numbers=service.account_numbers(session, space.id),
        )
        check = service.reconcile_statement(session, space, preview)
    assert Decimal(check["file_net"]) == Decimal("200000")
    assert Decimal(check["gap"]) == 0


# ── Заводка ──────────────────────────────────────────────────────────────────


def test_nomer_zapisyvaetsya_schetu_i_sleduyushchaya_vypiska_lozhitsya_sama(workspace):
    with finance_session() as session:
        space = service.ensure_workspace(session)
        service.create_account(session, space, name="Kaspi ТОО")
        names = [a.name for a in service.list_accounts(session, space.id)]
        first = analyze(kaspi_business(OPS[:1]), "в.xlsx", names, default_account="Kaspi ТОО")
        batch = service.save_preview(session, space, first)
        done = service.apply_batch(session, space, batch.id)
        assert done["remembered"]["number"] == KASPI

        second = analyze(
            kaspi_business(OPS[2:]),
            "в2.xlsx",
            names,
            account_numbers=service.account_numbers(session, space.id),
        )
    assert second.question is None
    assert second.bank["account"] == "Kaspi ТОО"


def test_chuzhoy_nomer_ne_peretiraetsya(workspace):
    """У счёта уже есть номер — выписка другого счёта его не перепишет."""
    with finance_session() as session:
        space = service.ensure_workspace(session)
        service.create_account(session, space, name="Kaspi ТОО", number="KZ11722S000000000001")
        batch = service.save_preview(
            session, space, analyze(kaspi_business(OPS[:1]), "в.xlsx", ["Kaspi ТОО"], default_account="Kaspi ТОО")
        )
        done = service.apply_batch(session, space, batch.id)
        number = service.account_numbers(session, space.id)
    assert done["remembered"] is None
    assert number == {"KZ11722S000000000001": "Kaspi ТОО"}


def test_perevod_iz_dvuh_vypisok_zavoditsya_odin_raz(workspace):
    """Перевод виден в выписке счёта и в выписке депозита, операция — одна."""
    mirror = [
        ["77", "15.09.2026 19:34:02", None, 800000, f'ТОО "Пример"\nИИН/БИН {OWNER_BIN}',
         KASPI, None, "390", "Пополнение депозита со счёта Kaspi Pay"],
    ]
    with finance_session() as session:
        space = service.ensure_workspace(session)
        service.create_account(session, space, name="Kaspi ТОО", number=KASPI)
        service.create_account(session, space, name="Депозит ТОО", number=DEPOSIT)
        names = [a.name for a in service.list_accounts(session, space.id)]
        numbers = service.account_numbers(session, space.id)
        first = service.save_preview(
            session, space, analyze(kaspi_business(OPS), "счёт.xlsx", names, account_numbers=numbers)
        )
        service.apply_batch(session, space, first.id)
        second = service.save_preview(
            session,
            space,
            analyze(
                kaspi_business(mirror, number=DEPOSIT, closing=800000),
                "депозит.xlsx",
                names,
                account_numbers=numbers,
            ),
        )
        result = service.apply_batch(session, space, second.id)
    assert result["imported"] == 0
    assert result["duplicate"] == 1


def test_kontragent_uznaetsya_po_bin_a_ne_po_napisaniyu(workspace):
    ops = [
        ["1", "15.09.2026 10:00:00", None, 1000, 'ТОО "Альфа"\nИИН/БИН 200940008138', "", None, "859", "раз"],
        ["2", "15.09.2026 11:00:00", None, 2000, "Альфа ТОО\nИИН/БИН 200940008138", "", None, "859", "два"],
    ]
    with finance_session() as session:
        space = service.ensure_workspace(session)
        service.create_account(session, space, name="Kaspi ТОО", number=KASPI)
        batch = service.save_preview(
            session,
            space,
            analyze(kaspi_business(ops, closing=3000), "в.xlsx", ["Kaspi ТОО"],
                    account_numbers=service.account_numbers(session, space.id)),
        )
        service.apply_batch(session, space, batch.id)
        parties = [(p.name, p.details.get("bin")) for p in service.list_counterparties(session, space.id)]
    assert parties == [('ТОО "Альфа"', "200940008138")]


def test_nomer_odin_na_kompaniyu(workspace):
    with finance_session() as session:
        space = service.ensure_workspace(session)
        service.create_account(session, space, name="Kaspi ТОО", number="kz87 722s 0000 2583 1219")
        with pytest.raises(FinanceError, match="уже записан"):
            service.create_account(session, space, name="Второй", number=KASPI)
        assert service.account_numbers(session, space.id) == {KASPI: "Kaspi ТОО"}


# ── Другие форматы ───────────────────────────────────────────────────────────


def test_staryy_excel_xls_shapka_v_dve_stroki():
    """Excel 97–2003 с шапкой в две строки — как печатает Halyk.

    Раньше: «Формат .xls не читается, пересохраните как .xlsx»; а шапка в две
    строки давала «нет колонки суммы», хотя суммы в файле есть.
    """
    data = (FIXTURES / "statement_97.xls").read_bytes()
    preview = analyze(data, "Выписка.xls", ["Halyk"], account_numbers={"KZ12601A000000001234": "Halyk"})
    ready = [row for row in preview.rows if row.state == "imported"]
    assert [(r.values["paid_at"], r.values["kind"], r.values["amount"]) for r in ready] == [
        ("2026-08-03", "income", "250000"),
        ("2026-08-05", "expense", "40000"),
    ]
    assert ready[0].values["counterparty"] == "ТОО «Альфа»"
    assert ready[0].values["counterparty_bin"] == "200940008138"
    assert preview.bank["opening_balance"] == "100000"
    assert preview.bank["closing_balance"] == "310000"
    assert preview.bank["period_start"] == "2026-08-01"
    assert preview.bank["bank_name"] == "Halyk"


def test_html_pod_vidom_xls():
    """Интернет-банк отдаёт HTML-таблицу с расширением .xls."""
    html = """<html><head><meta charset="utf-8"></head><body>
    <table><tr><td>Номер счета:</td><td>KZ12601A000000001234</td></tr>
    <tr><td>Входящий остаток:</td><td>1 000,00</td></tr></table>
    <table>
    <tr><th>Дата</th><th>Приход</th><th>Расход</th><th>Контрагент</th><th>Назначение платежа</th></tr>
    <tr><td>03.08.2026</td><td>5 000,00</td><td></td><td>ТОО Альфа</td><td>оплата</td></tr>
    <tr><td>04.08.2026</td><td></td><td>1 500,50</td><td>ИП Бета</td><td>аренда<br>за август</td></tr>
    </table></body></html>"""
    preview = analyze(html.encode("utf-8"), "statement.xls", ["Halyk"], default_account="Halyk")
    ready = [row for row in preview.rows if row.state == "imported"]
    assert [(r.values["kind"], r.values["amount"]) for r in ready] == [("income", "5000.00"), ("expense", "1500.50")]
    assert ready[1].values["comment"] == "аренда за август"
    assert preview.bank["opening_balance"] == "1000.00"


def test_xml_excel_2003():
    xml = """<?xml version="1.0"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
 xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
 <Worksheet ss:Name="Выписка"><Table>
  <Row><Cell><Data ss:Type="String">Счет</Data></Cell><Cell ss:Index="3"><Data ss:Type="String">KZ12601A000000001234</Data></Cell></Row>
  <Row><Cell><Data ss:Type="String">Дата</Data></Cell><Cell><Data ss:Type="String">Дебет</Data></Cell>
       <Cell><Data ss:Type="String">Кредит</Data></Cell><Cell><Data ss:Type="String">Назначение платежа</Data></Cell></Row>
  <Row><Cell><Data ss:Type="DateTime">2026-08-03T00:00:00.000</Data></Cell><Cell ss:Index="3"><Data ss:Type="Number">7000</Data></Cell>
       <Cell><Data ss:Type="String">оплата</Data></Cell></Row>
 </Table></Worksheet></Workbook>"""
    preview = analyze(xml.encode("utf-8"), "statement.xls", ["Halyk"], default_account="Halyk")
    row = next(r for r in preview.rows if r.state == "imported")
    assert (row.values["paid_at"], row.values["kind"], row.values["amount"]) == ("2026-08-03", "income", "7000")
    assert preview.bank["account_number"] == "KZ12601A000000001234"


def test_vygruzka_bank_klienta_1c():
    """1CClientBankExchange: направление — по тому, чей счёт у плательщика."""
    text = "\n".join(
        [
            "1CClientBankExchange",
            "ВерсияФормата=1.03",
            "Кодировка=Windows",
            "ДатаНачала=01.08.2026",
            "ДатаКонца=31.08.2026",
            "РасчСчет=KZ12601A000000001234",
            "СекцияРасчСчет",
            "ДатаНачала=01.08.2026",
            "ДатаКонца=31.08.2026",
            "РасчСчет=KZ12601A000000001234",
            "НачальныйОстаток=1000.00",
            "КонечныйОстаток=5500.00",
            "КонецРасчСчет",
            "СекцияДокумент=Платежное поручение",
            "Номер=12",
            "Дата=03.08.2026",
            "Сумма=6000.00",
            "ПлательщикСчет=KZ958562204112820386",
            "Плательщик=ТОО Альфа",
            "ПлательщикБИН=200940008138",
            "ПолучательСчет=KZ12601A000000001234",
            "Получатель=ТОО Пример",
            "ПолучательБИН=990140001234",
            "НазначениеПлатежа=Оплата по счёту 12",
            "КонецДокумента",
            "СекцияДокумент=Платежное поручение",
            "Номер=13",
            "Дата=05.08.2026",
            "Сумма=1500.00",
            "ПлательщикСчет=KZ12601A000000001234",
            "Плательщик=ТОО Пример",
            "ПолучательСчет=KZ466010002022210490",
            "Получатель=ИП Бета",
            "ПолучательБИН=951001400061",
            "НазначениеПлатежа=Аренда",
            "КонецДокумента",
            "КонецФайла",
        ]
    )
    preview = analyze(text.encode("cp1251"), "kl_to_1c.txt", ["Halyk"], account_numbers={"KZ12601A000000001234": "Halyk"})
    ready = [row for row in preview.rows if row.state == "imported"]
    assert [(r.values["kind"], r.values["amount"], r.values["counterparty"]) for r in ready] == [
        ("income", "6000.00", "ТОО Альфа"),
        ("expense", "1500.00", "ИП Бета"),
    ]
    assert preview.question is None
    assert preview.bank["opening_balance"] == "1000.00"
    assert preview.bank["closing_balance"] == "5500.00"


# ── Мелкие, но тихие ─────────────────────────────────────────────────────────


def book(rows, head):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(head)
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def test_popolnenie_balansa_ne_stroka_itoga():
    """Слово «баланс» в операции с датой больше не делает её итоговой."""
    data = book(
        [[date(2026, 8, 3), 2000, "Kaspi", "Пополнение баланса Tele2"]],
        ["Дата", "Сумма", "Со счёта", "Комментарий"],
    )
    preview = analyze(data, "к.xlsx", ["Kaspi"])
    assert preview.rows[0].state == "imported"
    assert preview.rows[0].values["kind"] == "expense"


def test_perenesyonnoe_naznachenie_platezha_skleivaetsya():
    data = book(
        [
            [date(2026, 8, 3), None, 1500, "Оплата по договору № 7"],
            [None, None, None, "за август 2026 года"],
        ],
        ["Дата", "Приход", "Расход", "Назначение платежа"],
    )
    preview = analyze(data, "в.xlsx", ["Kaspi"], default_account="Kaspi")
    assert preview.rows[0].values["comment"] == "Оплата по договору № 7 за август 2026 года"
    assert preview.rows[1].state == "skipped"


def test_znak_v_vypiske_odnogo_scheta():
    """Плюсы и минусы в одной колонке: плюс — поступление. Без минусов — вопрос."""
    signed = book(
        [[date(2026, 8, 3), "+5 000", "перевод"], [date(2026, 8, 4), "-1 000", "покупка"]],
        ["Дата", "Сумма", "Описание"],
    )
    preview = analyze(signed, "в.xlsx", ["Kaspi"], default_account="Kaspi")
    assert [row.values["kind"] for row in preview.rows] == ["income", "expense"]

    unsigned = book([[date(2026, 8, 3), 5000, "что-то"]], ["Дата", "Сумма", "Описание"])
    preview = analyze(unsigned, "в.xlsx", ["Kaspi"], default_account="Kaspi")
    assert preview.rows[0].state == "failed"
    assert preview.rows[0].problems[0]["field"] == "kind"


def test_priznak_d_k_odnoy_bukvoy():
    data = book(
        [[date(2026, 8, 3), 5000, "К"], [date(2026, 8, 4), 700, "Д"]],
        ["Дата", "Сумма", "Д/К"],
    )
    preview = analyze(data, "в.xlsx", ["Kaspi"], default_account="Kaspi")
    assert [row.values["kind"] for row in preview.rows] == ["income", "expense"]


def test_izobrazhenie_ne_chitaetsya_kak_tablica():
    from app.finance.importing import ImportError_

    with pytest.raises(ImportError_, match="изображение"):
        analyze(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "скрин.png", ["Kaspi"])

