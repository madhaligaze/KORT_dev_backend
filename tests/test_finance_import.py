"""Импорт файлов в раздел «Финансы».

Набор написан по протоколу проверки чужого импортёра (17 сентября 2026,
`docs/finmap-audit.md`): каждый тест здесь закрывает случай, на котором тот
ошибался. Это не «проверим, что парсер работает», а «проверим, что он не
повторяет известных ошибок» — и потому тесты названы по симптому.
"""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import openpyxl
import pytest
import sqlalchemy as sa

from app.finance import service
from app.finance.db import finance_session
from app.finance.importing import (
    DateReading,
    ImportError_,
    analyze,
    decide_date_order,
    parse_date,
    parse_money,
)

HEAD = ["Дата платежа", "Сумма", "Со счёта", "На счёт", "Категория", "Контрагент", "Комментарий"]


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    """Своя база на прогон.

    Отдельным файлом, а не общим: имя таблицы `workspaces` объявлено в трёх
    модулях (`books`, `finance`, плюс своё у web-excel), и на SQLite схем нет —
    первый, кто создаст таблицу, определит её вид для остальных. Урок из
    `test_books_rows.py`, где это уже стоило падения на «нет колонки».
    """
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
        space = service.ensure_workspace(session)
        return space.id


def book(rows, head=None, lead_rows=()):
    """Собрать xlsx в памяти."""
    wb = openpyxl.Workbook()
    ws = wb.active
    for lead in lead_rows:
        ws.append(lead)
    ws.append(head or HEAD)
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def row(day, amount, to="Касса", frm=None, category="Выручка", who="Клиент", comment=""):
    return [date(2026, 8, day), amount, frm, to, category, who, comment]


ACCOUNTS = ("Банковский счёт", "Касса")


# ── Разбор чисел и дат ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected, negative",
    [
        (150000, Decimal("150000"), False),
        ("120 500,45", Decimal("120500.45"), False),
        ("1 234.56", Decimal("1234.56"), False),
        ("1.234,56", Decimal("1234.56"), False),
        ("1,234", Decimal("1234"), False),
        ("'45000", Decimal("45000"), False),
        ("-15 000,50", Decimal("15000.50"), True),
        ("(15 000)", Decimal("15000"), True),
        ("1 500-", Decimal("1500"), True),
        ("₸ 99 000", Decimal("99000"), False),
        ("не число", None, False),
        ("", None, False),
    ],
)
def test_summa_chitaetsya_kak_ee_pishet_chelovek(raw, expected, negative):
    money = parse_money(raw)
    if expected is None:
        assert money is None
        return
    assert money.value == expected
    assert money.negative is negative


def test_minus_i_skobki_eto_rashod_a_ne_dohod(workspace):
    """Знак суммы обязан задавать направление, а не теряться.

    Симптом у чужого импортёра: «−15 000» приезжало доходом 15 000. Ошибка
    вдвое больше суммы, и никакого сообщения при этом нет.
    """
    data = book(
        [
            [date(2026, 8, 3), "-15 000", None, "Касса", "Аренда", "Арендодатель", "минус"],
            [date(2026, 8, 4), "(9 000)", None, "Касса", "Аренда", "Арендодатель", "скобки"],
        ]
    )
    preview = analyze(data, "выписка.xlsx", ACCOUNTS)
    kinds = [row_.values["kind"] for row_ in preview.rows]
    assert kinds == ["expense", "expense"]
    # Счёт из колонки «На счёт» должен стать счётом списания: деньги ушли.
    assert [row_.values["account_from"] for row_ in preview.rows] == ["Касса", "Касса"]


def test_poryadok_chastey_daty_odin_na_ves_fayl():
    """Порядок частей даты — свойство файла, а не отдельной строки.

    Симптом у чужого импортёра: в одном файле «12/25/2026» читалось как
    25 декабря (месяц-день), а «08/03/2026» — как 8 марта (день-месяц). Решение
    принималось по каждой строке отдельно, поэтому даты с числом до 12 молча
    уезжали на другой месяц, а ошибки не было.

    Здесь «12/25/2026» доказывает, что файл записан как месяц-день, и это
    чтение применяется ко всем строкам: «08/03/2026» — 3 августа. Важно не
    какое именно чтение выбрано, а что оно одно на весь файл.
    """
    reading = decide_date_order(["12/25/2026", "08/03/2026", "01/13/2026"])
    assert reading.order == "mdy"
    assert not reading.ambiguous
    assert parse_date("12/25/2026", reading) == date(2026, 12, 25)
    assert parse_date("08/03/2026", reading) == date(2026, 8, 3)

    # Тот же текст в файле, доказавшем обратный порядок, читается иначе — и это
    # решение файла, а не догадка строки.
    other = decide_date_order(["25/12/2026", "08/03/2026"])
    assert other.order == "dmy"
    assert parse_date("08/03/2026", other) == date(2026, 3, 8)


def test_dvusmyslennyy_fayl_ne_ugadyvaetsya_a_sprashivaet(workspace):
    """Все даты подходят под оба чтения — парсер обязан спросить."""
    data = book(
        [
            ["08/03/2026", 111000, None, "Касса", "Выручка", "Клиент", "раз"],
            ["05/04/2026", 222000, None, "Касса", "Выручка", "Клиент", "два"],
        ]
    )
    preview = analyze(data, "выписка.xlsx", ACCOUNTS)
    assert preview.question is not None
    assert preview.question["kind"] == "date_order"
    # В вопросе показано, как будет прочитан пример при каждом выборе, —
    # иначе выбор делается наугад, и мы всего лишь переложили догадку.
    assert preview.question["options"][0]["example"].endswith("2026-03-08")
    assert preview.question["options"][1]["example"].endswith("2026-08-03")


def test_protivorechivyy_fayl_nazyvaet_prichinu():
    """Файл записан двумя способами сразу — это отдельный разговор."""
    reading = decide_date_order(["13/01/2026", "01/25/2026"])
    assert reading.ambiguous
    assert "двумя разными способами" in reading.evidence


# ── Форма файла ──────────────────────────────────────────────────────────────


def test_lishnyaya_kolonka_ne_lomaet_razbor(workspace):
    """Незнакомая колонка — не ошибка.

    Симптом: одна лишняя колонка в шаблоне давала «Cannot read properties of
    undefined» без номера строки и без объяснения.
    """
    head = HEAD[:2] + ["Номер счёта-фактуры"] + HEAD[2:]
    rows = [[date(2026, 8, 3), 250000, "СФ-1024", None, "Касса", "Выручка", "Клиент", "первая"]]
    preview = analyze(book(rows, head=head), "книга.xlsx", ACCOUNTS)
    assert preview.counts["ready"] == 1
    assert "Номер счёта-фактуры" in preview.unused_columns


def test_shapka_otcheta_nad_zagolovkami_ne_meshaet(workspace):
    """Две строки шапки сверху — обычный вид любой выгрузки."""
    data = book(
        [row(3, 250000, comment="первая")],
        lead_rows=[["Отчёт по кассе за август 2026"], ["ТОО «Тест»", None, "стр. 1"]],
    )
    preview = analyze(data, "отчёт.xlsx", ACCOUNTS)
    assert preview.header_line == 3
    assert preview.counts["ready"] == 1


def test_perestavlennye_i_pereimenovannye_kolonki(workspace):
    """Колонку назвали «Дата», а не «Дата платежа», и подвинули — всё равно наша."""
    head = ["Комментарий", "Сумма", "На счёт", "Дата", "Категория"]
    rows = [["оплата", 50000, "Касса", date(2026, 8, 3), "Выручка"]]
    preview = analyze(book(rows, head=head), "книга.xlsx", ACCOUNTS)
    assert preview.counts["ready"] == 1
    assert preview.rows[0].values["paid_at"] == "2026-08-03"


def test_stroka_itogo_propuskaetsya_a_ne_otvergaet_fayl(workspace):
    """«Итого» снизу — не операция и не ошибка.

    Симптом: файл с итоговой строкой отвергался целиком сообщением «Итого —
    неверный формат даты».
    """
    rows = [row(3, 250000), row(5, 80000), ["Итого", 330000, None, None, None, None, None]]
    preview = analyze(book(rows), "книга.xlsx", ACCOUNTS)
    assert preview.counts["ready"] == 2
    assert preview.counts["failed"] == 0
    assert preview.rows[-1].state == "skipped"
    assert "итог" in preview.rows[-1].problems[0]["text"]


def test_dvuhkolonochnaya_vypiska_prihod_rashod(workspace):
    """«Приход» и «Расход» двумя колонками — самый частый вид банковской выписки."""
    head = ["Дата операции", "Приход", "Расход", "Счёт", "Назначение платежа"]
    rows = [
        [date(2026, 8, 3), 250000, None, "Касса", "поступление"],
        [date(2026, 8, 4), None, 80000, "Касса", "списание"],
        [date(2026, 8, 5), 100, 100, "Касса", "свод, не операция"],
    ]
    preview = analyze(book(rows, head=head), "выписка.xlsx", ACCOUNTS)
    assert [r.values["kind"] for r in preview.rows[:2]] == ["income", "expense"]
    assert preview.rows[0].values["account_to"] == "Касса"
    assert preview.rows[1].values["account_from"] == "Касса"
    # Обе колонки заполнены — угадывать нельзя, строка откладывается.
    assert preview.rows[2].state == "failed"


def test_neizvestnyy_schet_otkladyvaet_stroku_no_ne_fayl(workspace):
    """Счёт не создаётся импортом никогда, но одна строка не роняет остальные."""
    rows = [row(3, 250000), [date(2026, 8, 4), 17000, None, "Kaspi Gold", "Выручка", "Клиент", "нет счёта"]]
    preview = analyze(book(rows), "книга.xlsx", ACCOUNTS)
    assert preview.counts["ready"] == 1
    assert preview.counts["failed"] == 1
    assert "Kaspi Gold" in preview.accounts_missing
    assert "не найден" in preview.rows[1].problems[0]["text"]


def test_pustaya_data_zhaluetsya_na_svoyu_stroku(workspace):
    """Замечание обязано указывать на ту строку и на то поле, где беда.

    Симптом у чужого импортёра: строка без даты давала сообщение «25000 —
    неверный формат числа» про СОСЕДНЮЮ строку, где всё было правильно.
    Человек уходил чинить исправное.
    """
    rows = [row(3, 25000, comment="целая"), [None, 77777, None, "Касса", "Выручка", "Клиент", "без даты"]]
    preview = analyze(book(rows), "книга.xlsx", ACCOUNTS)
    good, bad = preview.rows
    assert good.state == "imported"
    assert bad.state == "failed"
    assert bad.line == 3
    assert [p["field"] for p in bad.problems] == ["paid_at"]


def test_fayl_bez_shapki_otkazyvaetsya_s_obyasneniem(workspace):
    """Нет ни даты, ни суммы в заголовках — читать нечего, и это честный отказ."""
    with pytest.raises(ImportError_) as exc:
        analyze(book([[1, 2, 3]], head=["Раз", "Два", "Три"]), "непонятно.xlsx", ACCOUNTS)
    assert "заголовков" in str(exc.value)


# ── Применение: всё или не всё ───────────────────────────────────────────────


def test_odna_plohaya_stroka_iz_dvuhsot_ne_otmenyaet_ostalnye(workspace):
    """Главный тест набора.

    Симптом: двести строк, испорчена 137-я — не завелось ни одной. Здесь
    заводится 199, а 137-я остаётся в партии с объяснением.
    """
    rows = [row(1 + (i % 28), 1000 + i, comment=f"строка {i + 1}") for i in range(200)]
    rows[136][1] = "не число"
    data = book(rows)

    with finance_session() as session:
        space = service.ensure_workspace(session)
        preview = analyze(data, "большая.xlsx", [a.name for a in service.list_accounts(session, space.id)])
        batch = service.save_preview(session, space, preview)
        result = service.apply_batch(session, space, batch.id, actor="тест")

    assert result["imported"] == 199
    assert result["failed"] == 1

    with finance_session() as session:
        space = service.ensure_workspace(session)
        operations, total = service.list_operations(session, space.id, limit=1000)
    assert total == 199


def test_povtornaya_zagruzka_togo_zhe_fayla_ne_dvoit(workspace):
    """Отпечаток строки считается по данным, а не по номеру строки."""
    data = book([row(3, 250000, comment="раз"), row(4, 80000, comment="два")])
    with finance_session() as session:
        space = service.ensure_workspace(session)
        names = [a.name for a in service.list_accounts(session, space.id)]
        first = service.save_preview(session, space, analyze(data, "в.xlsx", names))
        service.apply_batch(session, space, first.id)
        second = service.save_preview(session, space, analyze(data, "в.xlsx", names))
        result = service.apply_batch(session, space, second.id)
        _ops, total = service.list_operations(session, space.id, limit=100)

    assert result["imported"] == 0
    assert result["duplicate"] == 2
    assert total == 2


def test_otlozhennuyu_stroku_pravyat_bez_perezagruzki_fayla(workspace):
    """Правка строки в партии и повторная заводка только её."""
    rows = [row(3, 250000, comment="целая"), [None, 77777, None, "Касса", "Выручка", "Клиент", "без даты"]]
    with finance_session() as session:
        space = service.ensure_workspace(session)
        names = [a.name for a in service.list_accounts(session, space.id)]
        batch = service.save_preview(session, space, analyze(book(rows), "книга.xlsx", names))
        service.apply_batch(session, space, batch.id)
        fixed = service.fix_import_row(session, space, batch.id, 3, {"paid_at": "2026-08-09"})
        assert fixed.state == "imported"
        assert fixed.problems == []
        again = service.apply_batch(session, space, batch.id, only_lines=[3])
        _ops, total = service.list_operations(session, space.id, limit=100)

    assert again["imported"] == 1
    assert total == 2


def test_import_zavodit_spravochniki_no_ne_scheta(workspace):
    """Категория и контрагент создаются сами, счёт — никогда.

    Счёт — это место, где лежат деньги; появиться из опечатки в выписке он не
    должен. Категория, наоборот, появляется в работе постоянно.
    """
    rows = [[date(2026, 8, 3), 250000, None, "Касса", "Новая статья", "Новый клиент", "оплата"]]
    with finance_session() as session:
        space = service.ensure_workspace(session)
        names = [a.name for a in service.list_accounts(session, space.id)]
        batch = service.save_preview(session, space, analyze(book(rows), "книга.xlsx", names))
        service.apply_batch(session, space, batch.id)
        categories = [c.name for c in service.list_categories(session, space.id)]
        parties = [c.name for c in service.list_counterparties(session, space.id)]
        accounts = [a.name for a in service.list_accounts(session, space.id)]

    assert "Новая статья" in categories
    assert "Новый клиент" in parties
    assert accounts == ["Банковский счёт", "Касса"]


def test_csv_s_tochkoy_s_zapyatoy_i_cp1251(workspace):
    """CSV из 1С: разделитель «;», кодировка cp1251."""
    text = "Дата;Сумма;На счёт;Категория;Комментарий\r\n03.08.2026;120 500,45;Касса;Выручка;оплата\r\n"
    preview = analyze(text.encode("cp1251"), "выгрузка.csv", ACCOUNTS)
    assert preview.counts["ready"] == 1
    assert preview.rows[0].values["amount"] == "120500.45"

def test_dva_odinakovyh_platezha_v_odin_den_zavodyatsya_oba(workspace):
    """Повтор внутри файла — не дубль.

    Два раза по 77 ₸ в одном магазине за день — обычное дело. Первая версия
    считала их одной операцией: в выписке Kaspi Gold за год так молча потерялись
    219 строк из 2050, а сообщение выглядело буднично — «повторов 219».
    """
    rows = [row(3, 77, comment="Magnum"), row(3, 77, comment="Magnum")]
    with finance_session() as session:
        space = service.ensure_workspace(session)
        names = [a.name for a in service.list_accounts(session, space.id)]
        batch = service.save_preview(session, space, analyze(book(rows), "в.xlsx", names))
        result = service.apply_batch(session, space, batch.id)
        _ops, total = service.list_operations(session, space.id, limit=10)
    assert result["imported"] == 2, result
    assert total == 2


def test_povtornaya_zagruzka_faila_s_odinakovymi_strokami_ne_dvoit(workspace):
    """Но тот же файл, загруженный дважды, по-прежнему не заводит копий."""
    data = book([row(3, 77, comment="Magnum"), row(3, 77, comment="Magnum")])
    with finance_session() as session:
        space = service.ensure_workspace(session)
        names = [a.name for a in service.list_accounts(session, space.id)]
        first = service.save_preview(session, space, analyze(data, "в.xlsx", names))
        service.apply_batch(session, space, first.id)
        second = service.save_preview(session, space, analyze(data, "в.xlsx", names))
        again = service.apply_batch(session, space, second.id)
        _ops, total = service.list_operations(session, space.id, limit=10)
    assert again["imported"] == 0
    assert again["duplicate"] == 2
    assert total == 2


def test_data_s_dnem_nedeli_chitaetsya_kak_data(workspace):
    """«пн 01.06.26» — так пишет дату книга, которую ведут руками.

    Найдено живым переносом «Копии Журнала ГК BBC»: все 1999 строк уходили в
    отложенные с «не понял дату», потому что ячейка начинается не с цифры.
    Причём порядок частей при этом объявлялся решённым — «даты пришли из Excel
    датами», — то есть экран уверял, что с датами всё в порядке.
    """
    reading = decide_date_order(["пн 01.06.26", "вт 15.06.26", "ср 16.06.26"])
    assert reading.order == "dmy"
    assert not reading.ambiguous
    assert parse_date("пн 01.06.26", reading) == date(2026, 6, 1)
    assert parse_date("Mon 15.06.26", reading) == date(2026, 6, 15)
    assert parse_date("понедельник, 15.06.2026", reading) == date(2026, 6, 15)
    # Приставка не должна съедать месяц «мар» и ломать словесные даты.
    assert parse_date("3 мар 2026", reading) == date(2026, 3, 3)
    assert parse_date("пн 3 сентября 2026", reading) == date(2026, 9, 3)
    # И не должна превращать в дату то, что датой не является.
    assert parse_date("среда", reading) is None


def test_povtornyy_razbor_ne_ostavlyaet_broshennyh_partiy(workspace):
    """Ответ на вопрос раздела не должен плодить «разобрано, не заведено».

    Найдено живым прогоном: на вопрос «на какой счёт?» человек отвечает, раздел
    разбирает файл заново — и в истории повисали две записи на один файл. Какую
    из них продолжать, узнать было нельзя.
    """
    from app.finance.models import ImportBatch

    data = book([["01.03.2026", "-1 000", "Касса", "", "Связь", "", "интернет"]])
    with finance_session() as session:
        space = service.get_workspace(session, workspace)
        for _ in range(3):
            preview = analyze(data, "выписка.xlsx", ["Касса"])
            service.save_preview(session, space, preview, actor="тест")
        session.flush()
        previews = session.scalars(
            sa.select(ImportBatch).where(
                ImportBatch.workspace_id == space.id, ImportBatch.status == "preview"
            )
        ).all()
        assert len(previews) == 1, "незавершённых партий на один файл больше одной"
