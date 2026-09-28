"""Книга Google, открытая в «Финансах».

Смысл набора: книга из Google обязана читаться ровно так же, как скачанный
файл. Соблазн был обратный — написать «быстрый разбор для Google», раз строки
уже пришли списком. Тогда одна и та же книга читалась бы по-разному в
зависимости от того, скачали её или открыли, и объяснить расхождение было бы
нечем.

Сеть здесь не нужна: чтение подменяется, проверяется разбор.
"""
from __future__ import annotations

import pytest

from app.finance import sheets

#: Так выглядит книга, которую ведут руками: приход и расход разными колонками.
HEAD = ["Дата платежа", "Приход", "Расход", "Счёт", "Категория", "Комментарий"]


def reader_of(rows):
    """Чтение вкладки, подменённое списком строк."""

    def read(book_id: str, tab: str) -> list[list[str]]:
        assert book_id and tab
        return rows

    return read


def test_vkladka_chitaetsya_tem_zhe_razborom_chto_i_fayl():
    rows = [
        HEAD,
        ["01.03.2026", "", "15 000", "Касса", "Аренда", "март"],
        ["02.03.2026", "240 000", "", "Касса", "Выручка", "оплата"],
    ]
    preview = sheets.preview_tab("book", "Журнал", ["Касса"], reader=reader_of(rows))

    assert preview.counts["ready"] == 2
    kinds = [row.values["kind"] for row in preview.rows]
    assert kinds == ["expense", "income"]
    assert preview.file_name == "Журнал · Google Таблицы"


def test_shapka_otcheta_nad_tablitsey_ne_meshaet():
    """В книгах, которые ведут руками, над таблицей всегда что-то написано."""
    rows = [
        ["Журнал ГК BBC", "", "", "", "", ""],
        ["за март 2026", "", "", "", "", ""],
        [],
        HEAD,
        ["03.03.2026", "", "1 000", "Касса", "Связь", "интернет"],
    ]
    preview = sheets.preview_tab("book", "Журнал", ["Касса"], reader=reader_of(rows))

    assert preview.header_line == 4
    assert preview.counts["ready"] == 1


def test_kolonki_nayden_po_nazvaniyu_a_ne_po_nomeru():
    """Колонку в книгу вставят, и это не должно ничего сдвинуть.

    Тот же урок, что в дашборде: прибитые номера ломались дважды, причём тихо —
    цифры продолжали выглядеть цифрами.
    """
    rows = [
        ["№", "Комментарий", "Дата платежа", "Кто", "Сумма", "Счёт", "Категория"],
        ["1", "аванс", "04.03.2026", "Иванов", "-50 000", "Касса", "Зарплата"],
    ]
    preview = sheets.preview_tab("book", "Журнал", ["Касса"], reader=reader_of(rows))

    row = preview.rows[0]
    assert row.state == "imported"
    assert str(row.values["amount"]) == "50000"
    assert row.values["comment"] == "аванс"


def test_odna_isporchennaya_stroka_ne_otmenyaet_vkladku():
    rows = [
        HEAD,
        ["05.03.2026", "", "2 000", "Касса", "Связь", "ок"],
        ["", "", "3 000", "Касса", "Связь", "без даты"],
        ["07.03.2026", "", "4 000", "Касса", "Связь", "ок"],
    ]
    preview = sheets.preview_tab("book", "Журнал", ["Касса"], reader=reader_of(rows))

    assert preview.counts["ready"] == 2
    assert preview.counts["failed"] == 1


def test_plyus_pri_odnoy_kolonke_scheta_ne_ugadyvaetsya():
    """«240 000» и колонка «Счёт» — это приход или расход? Неизвестно.

    Отказ читать строку здесь честнее догадки: догадка развернула бы четверть
    миллиона не в ту сторону и не сказала бы об этом ни слова. Строка
    откладывается и называет, чего ей не хватает, — остальные заводятся.
    """
    rows = [
        ["Дата платежа", "Сумма", "Счёт", "Категория"],
        ["01.03.2026", "240 000", "Касса", "Выручка"],
        ["02.03.2026", "-15 000", "Касса", "Аренда"],
    ]
    preview = sheets.preview_tab("book", "Журнал", ["Касса"], reader=reader_of(rows))

    assert preview.counts["ready"] == 1
    assert preview.counts["failed"] == 1
    problems = " ".join(str(p) for p in preview.rows[0].problems)
    assert "вид" in problems or "доход" in problems or "расход" in problems


def test_pustaya_vkladka_govorit_chto_perenosit_nechego():
    with pytest.raises(sheets.SheetsError) as exc:
        sheets.preview_tab("book", "Лист1", ["Касса"], reader=reader_of([[], ["", ""]]))
    assert "пустая" in str(exc.value)


def test_otkaz_google_prihodit_tekstom_dlya_cheloveka():
    def read(book_id: str, tab: str):
        raise RuntimeError("У сервисного аккаунта нет доступа к этой книге")

    with pytest.raises(sheets.SheetsError) as exc:
        sheets.preview_tab("book", "Журнал", ["Касса"], reader=read)
    assert "доступа" in str(exc.value)


def test_ssylka_vedet_na_samu_knigu():
    """Работа в Google не запрещается — она продолжается рядом."""
    assert sheets.book_url("abc") == "https://docs.google.com/spreadsheets/d/abc/edit"
    assert sheets.book_url("abc", 12).endswith("#gid=12")


def test_skrytye_vkladki_ne_predlagayutsya():
    class FakeGoogle:
        @staticmethod
        def spreadsheet_meta(book_id: str):
            return {
                "id": book_id,
                "title": "Книга",
                "tabs": [
                    {"title": "Журнал", "hidden": False, "sheet_id": 1},
                    {"title": "Служебный", "hidden": True, "sheet_id": 2},
                ],
            }

    result = sheets.tabs("abc", google=FakeGoogle)
    assert [tab["title"] for tab in result["tabs"]] == ["Журнал"]
    assert result["url"].endswith("/edit")
