"""Шаблон счетов Excel (`contracts/bill_template.py`): скачать в разделе «Счета», заполнить, загрузить.

Набор держит обещания шаблона:
* колонки и порядок - как в шаблоне финансистов BBC (22.10.2026), строка на
  счёт, известное вписано, «№ Счета» и «Дата выстав. Счета» - жёлтые и пустые;
* загруженный шаблон разбирается по названиям колонок, строка счёта - по
  скрытому коду KORT или по юрлицу, клиенту, № договора и «Периоду с»; шаблон
  финансистов без кода тоже подходит;
* каждая строка - со своим состоянием: записать, уже в книге, не заполнена,
  отказ словами; отказ одной строки не роняет остальные;
* не нашлась, нашлось две, счёт в файле дважды, правили договор, нет номера
  или даты, в книге уже другой номер - отказ строке, а не догадка;
* пустые «Счет за Период» и «Доп. Коммент.» не стирают книгу; «Счет за
  Период» при выставлении подставляется, как в карточке.
"""
from __future__ import annotations

import dataclasses
import io
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from openpyxl import Workbook, load_workbook

from app.finance.contracts import bill_template
from app.finance.contracts.bills import DONE, Bill, Board

import company_layer


@pytest.fixture(autouse=True)
def _company_layer(monkeypatch):
    """Компания теста - с шапкой таблиц копии прода: её знает словарь компании, а не ядро (п.9 «Пакеты»)."""
    company_layer.everywhere(monkeypatch)


#: Имена листов - такие, чтобы Excel рисовал ярлыки на одной линии (выбор владельца 23.10.2026).
REG, ABON = "Общий реестр", "Абонентские услуги"

AYA = Bill(
    line=10, group="overdue", text="Просрочка 2–5 дней", own="BBCS", customer="ИП АЙЯ", number="№НО/39",
    month="ОКТЯБРЬ 2026", month_no=10, kind="Абон.П.", staff="Ануар", department="НО",
    amount=Decimal("91000"), period_from=date(2026, 10, 20), period_to=date(2026, 11, 19), paid="", status="",
    note="", onec_name="ИП Айя", signed_at="20.01.2026",
)
RAJA = dataclasses.replace(AYA, line=11, customer="ИП РАЖА", number="№НО/37", amount=Decimal("78000"), onec_name="ИП Ража")
KADYR = dataclasses.replace(
    AYA, line=12, customer="ИП Кадыркулова Торгын Карашевна", number="№НО/19", staff="Дана", amount=Decimal("25000"),
    period_from=date(2026, 10, 17), period_to=date(2026, 11, 16), onec_name="ИП Кадыркулова", signed_at="17.01.2026",
)
KENESARY = dataclasses.replace(
    AYA, line=13, own="BBCL", customer="ТОО KENESARY AGRO", number="№ЮО/44", amount=Decimal("150000"),
    period_from=date(2026, 10, 22), period_to=date(2026, 11, 21), onec_name="", signed_at="",
)
#: Тот же договор АЙЯ за сентябрь - выставлен: номер 248.
AYA_SEPTEMBER = dataclasses.replace(
    AYA, line=5, group=DONE, text="TRUE", month="СЕНТЯБРЬ 2026", period_from=date(2026, 9, 20),
    period_to=date(2026, 10, 19), bill_no="248", bill_date="21.09.2026",
)


def board(*done: Bill) -> Board:
    return Board(
        read_at=datetime(2026, 10, 22, tzinfo=timezone.utc), title="Сводка", worksheet="Сводка", spreadsheet_id="s",
        bills=[KADYR, AYA, RAJA, KENESARY], done=[AYA_SEPTEMBER, *done],
    )


def workbook(data: bytes):
    return load_workbook(io.BytesIO(data))


def saved(book) -> bytes:
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def filled(data: bytes, cells: dict[str, object]) -> bytes:
    book = workbook(data)
    for address, value in cells.items():
        book.active[address].value = value
    return saved(book)


def statuses(result: dict) -> list[tuple[str, str]]:
    return [(row["customer"], row["status"]) for row in result["rows"]]


def kind(bill: Bill) -> str:
    return bill_template.RENT_SHEET if bill.department == "Аренда" else bill_template.ABON_SHEET


def test_shablon_kak_u_finansistov() -> None:
    sheet = workbook(bill_template.build([AYA, RAJA, KENESARY], kind_of=kind)).active
    assert sheet.title == REG
    heads = {cell.column_letter: cell.value for cell in sheet[1] if cell.value}
    assert heads == {
        # Подписи - названия колонок компании из её словаря; перенос строки -
        # шириной колонки (`wrap_text`), а не знаком в тексте.
        "B": "Наша Фирма", "C": "Наш Сотрудник", "D": "Заказчик (Название Фирмы)", "E": "По 1C",
        "F": "№ Договора", "G": "Дата заключения Договора", "I": "Период с (нач.)", "J": "Период по (зав.)",
        "L": "Сумма Договора", "N": "№ Счета", "O": "Дата выстав. Счета", "Q": "Код KORT",
    }
    row = {cell.column_letter: cell.value for cell in sheet[2] if cell.value is not None}
    assert row["B"] == "BBCS" and row["C"] == "Ануар" and row["D"] == "ИП АЙЯ" and row["E"] == "ИП Айя"
    assert row["F"] == "№НО/39" and row["L"] == 91000
    assert row["G"].date() == date(2026, 1, 20) and row["I"].date() == date(2026, 10, 20)
    assert row["J"].date() == date(2026, 11, 19)
    assert [sheet[f"D{n}"].value for n in (2, 3, 4)] == ["ИП АЙЯ", "ИП РАЖА", "ТОО KENESARY AGRO"], "строка на счёт, по порядку"
    assert [sheet[f"Q{n}"].value for n in (2, 3, 4)] == [AYA.id, RAJA.id, KENESARY.id]
    assert sheet.column_dimensions["Q"].hidden, "код счёта - в скрытой колонке"
    assert sheet["E4"].value is None, "«По 1C» пусто - пусто, а не «None»"
    for address in ("G2", "I2", "J2", "O3"):
        # Строчными: «DD.MM.YYYY» просмотр Apple показывал «296.10.2026» (23.10.2026, айфон).
        assert sheet[address].number_format == "dd.mm.yyyy", address
    assert sheet["L2"].number_format == "#,##0"
    assert sheet.freeze_panes == "A2"
    assert bill_template.filename(date(2026, 10, 22)) == "Счета к выставлению 22.10.2026.xlsx"


def test_listy_obshchiy_reestr_vid_i_yurlica() -> None:
    """«Общий реестр» → «Аренда» → «Абонентские услуги» → лист на юрлицо; вписывают на листе юрлица."""
    rent = dataclasses.replace(AYA, line=20, own="BBC Omar", customer="ТОО Молфуд", number="№RENT/93", department="Аренда")
    odd = dataclasses.replace(AYA, line=21, own="Фирма: «А/Б»?", customer="ИП Странный", number="№1")
    book = workbook(bill_template.build([AYA, KENESARY, rent, RAJA, odd], kind_of=kind))
    assert book.sheetnames == [REG, "Аренда", ABON, "BBC Omar", "BBCL", "BBCS", "Фирма- «А-Б»-"]
    assert book.calculation.fullCalcOnLoad, "формулы считаются при открытии"

    def customers(title: str) -> list[str]:
        sheet = book[title]
        return [sheet[f"D{n}"].value for n in range(2, sheet.max_row + 1)]

    assert customers(REG) == ["ИП АЙЯ", "ТОО KENESARY AGRO", "ТОО Молфуд", "ИП РАЖА", "ИП Странный"]
    assert customers("Аренда") == ["ТОО Молфуд"]
    assert customers(ABON) == ["ИП АЙЯ", "ТОО KENESARY AGRO", "ИП РАЖА", "ИП Странный"]
    assert customers("BBCS") == ["ИП АЙЯ", "ИП РАЖА"] and customers("BBCL") == ["ТОО KENESARY AGRO"]

    # Лист юрлица - где вписывают: жёлтое, пустое, с проверкой даты.
    firm = book["BBCS"]
    assert firm["N2"].value is None and firm["O3"].value is None
    for address in ("N1", "O1", "N2", "O3"):
        assert firm[address].fill.fgColor.rgb == "FFFFF2CC", f"{address}: жёлтое, не прозрачное"
    [rule] = firm.data_validations.dataValidation
    assert str(rule.sqref) == "O2:O3", "дата проверяется у каждой строки"
    assert firm["Q2"].value == AYA.id and firm.column_dimensions["Q"].hidden

    # Общие листы тянут № и дату по коду счёта, а не по адресу клетки.
    summary = book[REG]
    assert summary["N2"].value == (
        "=IFERROR(IF(INDEX('BBCS'!$N$2:$N$2001,MATCH($Q2,'BBCS'!$Q$2:$Q$2001,0))=\"\",\"\","
        "INDEX('BBCS'!$N$2:$N$2001,MATCH($Q2,'BBCS'!$Q$2:$Q$2001,0))),\"\")"
    )
    assert "'BBC Omar'!$O$2:$O$2001" in summary["O4"].value and "$Q4" in summary["O4"].value
    assert "'Фирма- «А-Б»-'!" in summary["N6"].value
    assert summary["N1"].fill.fgColor.rgb != "FFFFF2CC" and summary["N1"].comment is not None, "здесь не вписывают"
    assert "'BBC Omar'!" in book["Аренда"]["N2"].value
    [rule] = summary.data_validations.dataValidation
    assert str(rule.sqref) == "O2:O6", "впишут дату прямо в общий реестр - тоже проверится"
    assert book["Аренда"].max_row == 2


def test_pustoy_vid_list_s_odnoy_shapkoy() -> None:
    book = workbook(bill_template.build([AYA], kind_of=kind))
    assert book.sheetnames == [REG, "Аренда", ABON, "BBCS"]
    assert book["Аренда"].max_row == 1 and book["Аренда"]["N1"].value == "№ Счета"


def fill_sheet(data: bytes, title: str, cells: dict[str, object]) -> bytes:
    book = workbook(data)
    for address, value in cells.items():
        book[title][address].value = value
    return saved(book)


def test_zapolneno_na_liste_yurlica_odna_zapis() -> None:
    """Вписали на листе юрлица - одна строка с его адресом; общий лист с теми же значениями (Excel пересчитал) - не дубль."""
    template = bill_template.build([AYA, RAJA, KENESARY], kind_of=kind)
    on_firm = fill_sheet(template, "BBCS", {"N2": 312, "O2": datetime(2026, 10, 22)})
    result = bill_template.parse(on_firm, board())
    assert statuses(result) == [("ИП АЙЯ", "ready"), ("ИП РАЖА", "empty"), ("ТОО KENESARY AGRO", "empty")]
    aya = result["rows"][0]
    assert (aya["sheet"], aya["row"], aya["values"]) == ("BBCS", 2, {"bill_no": "312", "bill_date": "2026-10-22"})
    assert result["counts"] == {"ready": 1, "same": 0, "empty": 2, "error": 0}, "счёт с трёх листов - один"

    # Как после пересчёта Excel: общие листы показывают то же самое - всё равно одна запись.
    recalculated = workbook(on_firm)
    for title, row in ((REG, 2), (ABON, 2)):
        recalculated[title][f"N{row}"].value = 312
        recalculated[title][f"O{row}"].value = datetime(2026, 10, 22)
    result = bill_template.parse(saved(recalculated), board())
    assert statuses(result)[0] == ("ИП АЙЯ", "ready") and result["rows"][0]["sheet"] == "BBCS"
    assert result["counts"]["ready"] == 1

    # Вписали прямо в общий реестр, лист юрлица пуст - тоже принимается.
    on_summary = fill_sheet(template, REG, {"N4": "248/1", "O4": "22.10.2026"})
    result = bill_template.parse(on_summary, board())
    kenesary = next(row for row in result["rows"] if row["customer"] == "ТОО KENESARY AGRO")
    assert (kenesary["status"], kenesary["sheet"], kenesary["values"]["bill_no"]) == ("ready", REG, "248/1")


def test_shablon_so_starymi_imenami_listov() -> None:
    """Шаблон, скачанный до 23.10.2026 («Счета», «Абонентская плата»): общие листы узнаются и по старым именам."""
    book = workbook(bill_template.build([AYA, RAJA], kind_of=kind))
    book[REG].title, book[ABON].title = "Счета", "Абонентская плата"
    book["BBCS"]["N2"].value, book["BBCS"]["O2"].value = 312, datetime(2026, 10, 22)
    book["Счета"]["N2"].value, book["Счета"]["O2"].value = 312, datetime(2026, 10, 22)  # как после пересчёта Excel
    result = bill_template.parse(saved(book), board())
    assert (result["rows"][0]["sheet"], result["rows"][0]["status"]) == ("BBCS", "ready")
    assert result["counts"]["ready"] == 1


def test_raznoe_na_listah_otkaz() -> None:
    template = bill_template.build([AYA, RAJA], kind_of=kind)
    book = workbook(fill_sheet(template, "BBCS", {"N2": 312, "O2": datetime(2026, 10, 22)}))
    book[REG]["N2"].value = 313  # на общем листе вписали поверх формулы другое
    book[REG]["O2"].value = datetime(2026, 10, 22)
    result = bill_template.parse(saved(book), board())
    aya = result["rows"][0]
    assert aya["status"] == "error" and aya["values"] == {}
    assert aya["problem"] == (
        "На листах «Общий реестр» строка 2, «BBCS» строка 2 у этого счёта вписано разное - какой номер и дата верные, KORT не угадывает"
    )
    assert result["counts"] == {"ready": 0, "same": 0, "empty": 1, "error": 1}


def test_sortirovka_lista_yurlica() -> None:
    """Лист юрлица отсортировали со скрытым кодом - всё на месте; без кода - строки не совпали с кодами, отказ."""
    template = bill_template.build([AYA, RAJA], kind_of=kind)
    book = workbook(fill_sheet(template, "BBCS", {"N2": 312, "O2": datetime(2026, 10, 22)}))
    firm = book["BBCS"]
    rows = [[cell.value for cell in firm[n]] for n in (2, 3)]
    for n, values in zip((2, 3), reversed(rows)):  # строки поменяли местами целиком, с кодом
        for column, value in enumerate(values, start=1):
            firm.cell(row=n, column=column).value = value
    result = bill_template.parse(saved(book), board())
    assert statuses(result) == [("ИП АЙЯ", "ready"), ("ИП РАЖА", "empty")]
    assert (result["rows"][0]["sheet"], result["rows"][0]["row"]) == ("BBCS", 3)

    book = workbook(fill_sheet(template, "BBCS", {"N2": 312, "O2": datetime(2026, 10, 22)}))
    firm = book["BBCS"]
    for column in "BCDEFGIJLNO":  # отсортировали видимые колонки, скрытый код остался на месте
        firm[f"{column}2"].value, firm[f"{column}3"].value = firm[f"{column}3"].value, firm[f"{column}2"].value
    result = bill_template.parse(saved(book), board())
    raja = next(row for row in result["rows"] if row["status"] != "empty")
    assert raja["status"] == "error" and "Строку правили" in raja["problem"]
    assert result["counts"]["ready"] == 0, "номер не уходит чужому счёту"


def test_zapolnennyy_shablon_v_stroki() -> None:
    data = filled(
        bill_template.build([AYA, RAJA, KENESARY]),
        {"N2": 312, "O2": datetime(2026, 10, 22), "N4": "248/1", "O4": "22.10.2026"},
    )
    result = bill_template.parse(data, board())
    assert statuses(result) == [("ИП АЙЯ", "ready"), ("ИП РАЖА", "empty"), ("ТОО KENESARY AGRO", "ready")]
    aya, raja, kenesary = result["rows"]
    assert aya["id"] == AYA.id and aya["sheet"] == REG and aya["row"] == 2
    assert aya["values"] == {"bill_no": "312", "bill_date": "2026-10-22"}
    assert aya["defaults"] == {"bill_period": "с 20.10.2026 года по 19.11.2026 года"}, "как подставляет карточка"
    assert aya["own"] == "BBCS" and aya["period_from"] == "2026-10-20" and aya["amount"] == "91000"
    assert raja["values"] == {} and raja["problem"] == "", "не заполнена - не ошибка"
    assert kenesary["values"] == {"bill_no": "248/1", "bill_date": "2026-10-22"}, "номер и дата текстом - тоже"
    assert result["counts"] == {"ready": 2, "same": 0, "empty": 1, "error": 0}


def finance_template(rows: list[list]) -> bytes:
    """Шаблон в точности как прислали финансисты 22.10.2026: шапка в 1-й строке, без кода KORT."""
    book = Workbook()
    sheet = book.active
    sheet.title = "Лист1"
    heads = {
        "B": "Наша\nФирма", "C": "Наш\nСотрудник", "D": "Заказчик\n(Название Фирмы)", "E": "По 1C",
        "F": "№\nДоговора", "G": "Дата заключения \nДоговора", "I": "Период\nс (нач.)", "J": "Период\nпо (зав.)",
        "L": "Сумма\nДоговора", "N": "№ \nСчета", "O": "Дата выстав.\nСчета",
    }
    for letter, title in heads.items():
        sheet[f"{letter}1"] = title
    for number, values in enumerate(rows, start=2):
        for letter, value in zip(("B", "C", "D", "E", "F", "G", "I", "J", "L", "N", "O"), values):
            sheet[f"{letter}{number}"] = value
    return saved(book)


THREE = [
    ["BBCS", "Дана", "ИП Кадыркулова Торгын Карашевна", "ИП Кадыркулова", "№НО/19", datetime(2026, 1, 17),
     datetime(2026, 10, 17), datetime(2026, 11, 16), "25\xa0000", 501, datetime(2026, 10, 22)],
    ["BBCS", "Ануар", "ИП АЙЯ", "ИП Айя", "№НО/39", datetime(2026, 1, 20),
     datetime(2026, 10, 20), datetime(2026, 11, 19), "91\xa0000", 502, datetime(2026, 10, 22)],
    ["BBCS", "Ануар", "ИП РАЖА", "ИП Ража", "№НО/37", datetime(2026, 1, 20),
     datetime(2026, 10, 20), datetime(2026, 11, 19), "78\xa0000", None, None],
]


def test_shablon_finansistov_bez_koda() -> None:
    """Их файл: строки - по юрлицу, клиенту, № договора и «Периоду с»; сентябрь АЙЯ не путается с октябрём."""
    result = bill_template.parse(finance_template(THREE), board())
    assert statuses(result) == [
        ("ИП Кадыркулова Торгын Карашевна", "ready"), ("ИП АЙЯ", "ready"), ("ИП РАЖА", "empty"),
    ]
    kadyr, aya, _ = result["rows"]
    assert kadyr["id"] == KADYR.id and kadyr["values"] == {"bill_no": "501", "bill_date": "2026-10-22"}
    assert aya["id"] == AYA.id and aya["row"] == 3


def test_ne_nashlas_i_neskolko_mesyatsev() -> None:
    stranger = ["BBCL", "", "ТОО Неизвестный", "", "№ЮО/99", None, datetime(2026, 10, 22), None, "1", 9, datetime(2026, 10, 22)]
    result = bill_template.parse(finance_template([stranger, THREE[2]]), board())
    assert statuses(result) == [("ТОО Неизвестный", "error"), ("ИП РАЖА", "empty")]
    assert result["rows"][0]["problem"] == "В сводной таблице нет счёта BBCL, ТОО Неизвестный, №ЮО/99 с периодом от 22.10.2026"
    # Без «Периода с»: у АЙЯ два месяца (сентябрь выставлен, октябрь нет) - не угадывается.
    no_period = [*THREE[1][:6], None, None, *THREE[1][8:]]
    result = bill_template.parse(finance_template([no_period]), board())
    assert result["rows"][0]["status"] == "error"
    assert "несколько месяцев - впишите «Период с»" in result["rows"][0]["problem"]


def test_dvazhdy_na_liste_otkaz() -> None:
    result = bill_template.parse(finance_template([THREE[1], THREE[0], THREE[1]]), board())
    assert statuses(result) == [("ИП АЙЯ", "error"), ("ИП Кадыркулова Торгын Карашевна", "ready")]
    assert "на листе «Лист1» несколько раз (строки 2, 4)" in result["rows"][0]["problem"]
    # Незаполненный двойник не мешает заполненному.
    blank_twin = [*THREE[1][:9], None, None]
    result = bill_template.parse(finance_template([THREE[1], blank_twin]), board())
    assert statuses(result) == [("ИП АЙЯ", "ready")]


def test_pravili_dogovor_v_shablone() -> None:
    book = workbook(filled(bill_template.build([AYA, KENESARY]), {"N2": 312, "O2": datetime(2026, 10, 22)}))
    book.active["D2"].value = "ИП Другой"
    result = bill_template.parse(saved(book), board())
    assert result["rows"][0]["status"] == "error" and "Строку правили" in result["rows"][0]["problem"]
    assert result["rows"][1]["status"] == "empty", "остальные строки разбираются"


def test_net_nomera_net_daty_plokhaya_data() -> None:
    data = filled(
        bill_template.build([AYA, RAJA, KENESARY, KADYR]),
        {"N2": 312, "O3": datetime(2026, 10, 22), "N4": 7, "O4": "завтра", "N5": "+312", "O5": datetime(2026, 10, 22)},
    )
    rows = bill_template.parse(data, board())["rows"]
    assert [row["status"] for row in rows] == ["error"] * 4
    assert [row["problem"] for row in rows] == [
        "Дата счёта - пусто",
        "Не вписан номер счёта",
        "Дата счёта - не дата: «завтра»",
        "«№ счёта» не может начинаться с =, + или @",
    ]
    assert all(row["values"] == {} for row in rows), "отказанной строке нечего записывать"


def test_uzhe_v_knige() -> None:
    """Выставленный счёт: тот же номер - «уже в книге»; другой - отказ, а не перезапись."""
    template = bill_template.build([AYA])
    same = bill_template.parse(filled(template, {"N2": 248, "O2": datetime(2026, 9, 21), "Q2": AYA_SEPTEMBER.id}), board())
    [row] = [row for row in same["rows"] if row["status"] != "empty"]
    assert (row["status"], row["note"], row["values"]) == ("same", "В таблице уже № 248 от 21.09.2026", {})
    other = bill_template.parse(filled(template, {"N2": 300, "O2": datetime(2026, 9, 21), "Q2": AYA_SEPTEMBER.id}), board())
    [row] = [row for row in other["rows"] if row["status"] != "empty"]
    assert row["status"] == "error" and row["problem"].startswith("В таблице уже № 248 от 21.09.2026 - KORT его не перезаписывает")


def test_nomer_s_razdelitelem_tysyach_tot_zhe() -> None:
    """Книга показывает 990101 как «990 101» (формат «#,##0»): это тот же номер, не «в книге уже другой»."""
    issued = dataclasses.replace(AYA_SEPTEMBER, bill_no="990\xa0101")
    data = filled(bill_template.build([AYA]), {"N2": 990101, "O2": datetime(2026, 9, 21), "Q2": issued.id})
    other = Board(read_at=datetime(2026, 10, 22, tzinfo=timezone.utc), title="", worksheet="", spreadsheet_id="",
                  bills=[AYA], done=[issued])
    [row] = [row for row in bill_template.parse(data, other)["rows"] if row["status"] != "empty"]
    assert row["status"] == "same" and row["note"] == "В таблице уже № 990\xa0101 от 21.09.2026"
    # Две записи одного счёта на разных листах: «990 101» текстом и 990101 числом - одно и то же.
    template = bill_template.build([AYA], kind_of=kind)
    book = workbook(fill_sheet(template, "BBCS", {"N2": 990101, "O2": datetime(2026, 10, 22)}))
    book[REG]["N2"].value, book[REG]["O2"].value = "990 101", datetime(2026, 10, 22)
    result = bill_template.parse(saved(book), board())
    assert result["rows"][0]["status"] == "ready" and result["counts"]["ready"] == 1


def test_ne_xlsx_otkaz() -> None:
    with pytest.raises(bill_template.TemplateError, match="не файл .xlsx"):
        bill_template.parse(b"%PDF-1.4 not a workbook", board())
    with pytest.raises(bill_template.TemplateError, match="пустой"):
        bill_template.parse(b"", board())
    with pytest.raises(bill_template.TemplateError, match="больше 2 МБ"):
        bill_template.parse(b"PK" + b"0" * (bill_template.MAX_BYTES + 1), board())
    plain = Workbook()
    plain.active["A1"] = "просто таблица"
    with pytest.raises(bill_template.TemplateError, match="нет шапки шаблона"):
        bill_template.parse(saved(plain), board())
    with pytest.raises(bill_template.TemplateError, match="нет ни одной строки"):
        bill_template.parse(bill_template.build([]), board())


def test_kolonki_po_nazvaniyam_i_dop_polya() -> None:
    """Вставили колонку и добавили «Счет за Период» и «Доп. Коммент.»: читается по названиям; пустое не стирает."""
    book = workbook(filled(bill_template.build([AYA, KENESARY]), {"N2": 312, "O2": datetime(2026, 10, 22), "N3": 5, "O3": "22.10.2026"}))
    sheet = book.active
    sheet.insert_cols(1)
    sheet["S1"], sheet["S2"] = "Счет за\nПериод (Коммент)", "за октябрь"
    sheet["T1"], sheet["T2"], sheet["T3"] = "Доп. \nКоммент.", None, "оплатят частями"
    aya, kenesary = bill_template.parse(saved(book), board())["rows"]
    assert aya["values"] == {"bill_no": "312", "bill_date": "2026-10-22", "bill_period": "за октябрь"}
    assert aya["defaults"] == {}, "период вписан в файле - подставлять нечего"
    assert kenesary["values"] == {"bill_no": "5", "bill_date": "2026-10-22", "note": "оплатят частями"}
    assert kenesary["defaults"] == {"bill_period": "с 22.10.2026 года по 21.11.2026 года"}
