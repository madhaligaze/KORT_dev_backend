"""Карточка счёта пишет в книгу юрлица (`contracts/bill_book.py`) - на книгах в памяти.

Набор держит правила записи из шапки модуля:
* строка - по ключу в книге юрлица, а не по номеру; ровно одна, иначе отказ;
* «Счет» KORT не пишет - его ставит формула книги по «№ Счета»;
* чужая правка между открытием карточки и записью не затирается (409);
* формулы, разлив массива и формулы-массивы над строкой не трогаются;
* запись - одним batchUpdate; после неё строка перечитывается, и если легло
  не то или сломалась формула - книга возвращается как была;
* строку сдвинули посреди записи - KORT говорит, где смотреть;
* новые вкладки («Ноябрь») KORT находит по формулам сам.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.finance.contracts import bill_book
from app.finance.contracts.bills import bill_key
from bill_book_fake import AA, EPOCH, T, X, Y, Z, fake_api, month_tab

import company_layer


@pytest.fixture(autouse=True)
def _company_layer(monkeypatch):
    """Компания теста - с шапкой таблиц копии прода: её знает словарь компании, а не ядро (п.9 «Пакеты»)."""
    company_layer.everywhere(monkeypatch)


SUMMARY = ("summary-id", "Сводка все ЮР лица")
KENESARY = bill_key("BBCL", "ТОО KENESARY AGRO", "№ЮО/44", "ОКТЯБРЬ 2026")
SHOWN = {"bill_no": "", "bill_date": "", "bill_period": "", "note": "звонить Асель"}
ROW = 5  # KENESARY в «Октябре»: строка 6 листа, от нуля - 5


@pytest.fixture(autouse=True)
def fresh_cache():
    bill_book.forget()
    yield
    bill_book.forget()


def october(api):
    return api.books["legal-id"].tabs["Октябрь"]


def issue(api, **extra):
    values = {"bill_no": "312", "bill_date": "2026-10-22", **extra}
    return bill_book.save(api, SUMMARY, KENESARY, SHOWN, values)


def test_kartochka_nahodit_stroku_v_knige_yurlica() -> None:
    api = fake_api()
    card = bill_book.card(api, SUMMARY, KENESARY)
    assert card["place"] == {
        "book": "BBC Legal Реестр",
        "tab": "Октябрь",
        "row": 6,
        "url": "https://docs.google.com/spreadsheets/d/legal-id/edit#gid=2034009495&range=A6",
    }
    assert card["fields"] == SHOWN and card["state"] == "Еще рано" and card["blocked"] == {}
    # Тот же клиент за сентябрь - своя строка на своей вкладке.
    september = bill_book.card(api, SUMMARY, bill_key("BBCL", "ТОО KENESARY AGRO", "№ЮО/44", "СЕНТЯБРЬ 2026"))
    assert september["place"]["tab"] == "Сентябрь" and september["fields"]["bill_no"] == "201"
    assert september["fields"]["bill_date"] == "23.09.2026"
    assert not any(name == "update" for name, _ in api.calls), "карточка книгу не правит"


def test_vystavit_pishet_nomer_i_datu_a_galochku_stavit_kniga() -> None:
    api = fake_api()
    saved = issue(api, bill_period="с 22.10.2026 года по 21.11.2026 года")
    assert saved.changed == ["bill_no", "bill_date", "bill_period"]
    assert saved.after == {
        "bill_no": "312",
        "bill_date": "22.10.2026",
        "bill_period": "с 22.10.2026 года по 21.11.2026 года",
        "note": "звонить Асель",
    }
    assert saved.state == "TRUE" and saved.warning == ""
    [requests] = api.updates
    assert len(requests) == 3, "всё одним batchUpdate"
    cells = {item["updateCells"]["range"]["startColumnIndex"]: item["updateCells"] for item in requests}
    assert set(cells) == {Z, AA, Y}, "в «Счет» KORT не пишет"
    assert all(item["range"]["startRowIndex"] == ROW for item in cells.values())
    assert cells[Z]["rows"][0]["values"][0]["userEnteredValue"] == {"numberValue": 312}, "номер - числом, как человек"
    serial = (date(2026, 10, 22) - EPOCH).days
    assert cells[AA]["rows"][0]["values"][0]["userEnteredValue"] == {"numberValue": serial}
    assert cells[AA]["fields"] == "userEnteredValue", "формат даты у ячейки уже есть - не трогаем"
    assert cells[Y]["rows"][0]["values"][0]["userEnteredValue"] == {"stringValue": "с 22.10.2026 года по 21.11.2026 года"}
    tab = october(api)
    assert tab.cell(ROW, X)["value"] is True and not tab.cell(ROW, X)["entered"], "галочка - формулой книги"
    assert tab.cell(ROW, T)["value"] == "звонить Асель"
    assert tab.cell(ROW - 1, Z)["value"] is None, "соседняя строка цела"


def test_to_zhe_samoe_ne_perepisyvaetsya() -> None:
    api = fake_api()
    first = issue(api)
    again = bill_book.save(api, SUMMARY, KENESARY, first.after, {"bill_no": "312", "bill_date": "2026-10-22"})
    assert again.changed == [] and len(api.updates) == 1


def test_chuzhaya_pravka_ne_zatiraetsya() -> None:
    api = fake_api()
    october(api).put(ROW, Z, 400)  # бухгалтер успел вписать номер в книге
    with pytest.raises(bill_book.BookError) as caught:
        issue(api)
    assert caught.value.status == 409 and "было «», стало «400»" in str(caught.value)
    assert api.updates == []


def test_v_knige_uzhe_to_zhe_samoe_ne_konflikt() -> None:
    """Номер в книге уже наш (повтор записи, ответ на которую потерялся) - не чужая правка."""
    api = fake_api()
    october(api).put(ROW, Z, 312)
    saved = issue(api)
    assert saved.changed == ["bill_date"] and saved.after["bill_no"] == "312"
    october(api).put(ROW, AA, 1)  # а дату за это время поменяли - вот это чужая правка
    with pytest.raises(bill_book.BookError) as caught:
        bill_book.save(api, SUMMARY, KENESARY, SHOWN, {"bill_no": "312", "bill_date": "2026-10-23"})
    assert caught.value.status == 409 and "Дата выстав. Счета" in str(caught.value)


PERIOD = {"bill_period": "с 22.10.2026 года по 21.11.2026 года"}


def test_defaults_pishutsya_tolko_v_pustoe() -> None:
    """«Счет за Период» из шаблона Excel: пусто в книге - пишется; заполнено - не трогается и записи не мешает."""
    api = fake_api()
    saved = bill_book.save(api, SUMMARY, KENESARY, {"bill_no": ""}, {"bill_no": "312", "bill_date": "2026-10-22"}, PERIOD)
    assert saved.changed == ["bill_no", "bill_date", "bill_period"] and len(api.updates) == 1
    assert october(api).cell(ROW, Y)["value"] == PERIOD["bill_period"]

    api = fake_api()
    october(api).put(ROW, Y, "за октябрь, по договорённости")
    saved = bill_book.save(api, SUMMARY, KENESARY, {"bill_no": ""}, {"bill_no": "312", "bill_date": "2026-10-22"}, PERIOD)
    assert saved.changed == ["bill_no", "bill_date"]
    assert october(api).cell(ROW, Y)["value"] == "за октябрь, по договорённости", "вписанное человеком цело"


def test_defaults_mimo_formuly() -> None:
    """Над «Счет за Период» формула: подставленное не пишется, а № и дата - пишутся."""
    api = fake_api()
    october(api).put(ROW, Y, "", formula='=IF(Z6<>""; "по счёту"; "")')
    saved = bill_book.save(api, SUMMARY, KENESARY, {"bill_no": ""}, {"bill_no": "312", "bill_date": "2026-10-22"}, PERIOD)
    assert saved.changed == ["bill_no", "bill_date"]
    assert october(api).cell(ROW, Y)["entered"] == {"formulaValue": '=IF(Z6<>""; "по счёту"; "")'}


def test_formula_v_pole_ne_zatiraetsya() -> None:
    api = fake_api()
    october(api).put(ROW, Z, 7, formula="=MAX(Z4:Z5)+1")
    card = bill_book.card(api, SUMMARY, KENESARY)
    assert "стоит формула" in card["blocked"]["bill_no"]
    with pytest.raises(bill_book.BookError, match="стоит формула"):
        bill_book.save(api, SUMMARY, KENESARY, {}, {"bill_no": "312", "bill_date": "2026-10-22"})
    assert api.updates == []


def test_razliv_massiva_ne_zatiraetsya() -> None:
    api = fake_api()
    october(api).cell(ROW, Z)["value"] = "из массива"  # значение есть, введённого нет
    assert "формула соседней ячейки" in bill_book.card(api, SUMMARY, KENESARY)["blocked"]["bill_no"]
    with pytest.raises(bill_book.BookError, match="формула соседней ячейки"):
        bill_book.save(api, SUMMARY, KENESARY, {}, {"bill_no": "312", "bill_date": "2026-10-22"})
    assert api.updates == []


def test_massiv_nad_kolonkoy_ne_lomaetsya() -> None:
    api = fake_api()
    october(api).put(2, Z, None, formula='=ARRAYFORMULA(IF(H3:H<>""; ROW(H3:H); ""))')
    with pytest.raises(bill_book.BookError, match="Над колонкой «№ Счета» стоит формула"):
        issue(api)
    # Формула-массив ниже шапки, но выше строки - тоже.
    api = fake_api()
    october(api).put(3, Y, None, formula="=ARRAYFORMULA(IF(Z4:Z<>\"\"; \"за месяц\"; \"\"))")
    assert "формула-массив" in bill_book.card(api, SUMMARY, KENESARY)["blocked"]["bill_period"]
    with pytest.raises(bill_book.BookError, match="формула-массив"):
        issue(api, bill_period="за октябрь")
    assert issue(api).changed == ["bill_no", "bill_date"], "номер в соседнюю колонку - можно"


def test_dva_sovpadeniya_otkaz() -> None:
    api = fake_api()
    tab = october(api)
    row = len(tab.rows)
    tab.put(row, 4, "BBCL")
    tab.put(row, 7, "ТОО KENESARY AGRO")
    tab.put(row, 15, "№ЮО/44")
    tab.put(row, 2, "ОКТЯБРЬ 2026")
    with pytest.raises(bill_book.BookError, match="стоит несколько раз"):
        issue(api)
    assert api.updates == []


def test_net_stroki_otkaz() -> None:
    api = fake_api()
    with pytest.raises(bill_book.BookError) as caught:
        bill_book.card(api, SUMMARY, bill_key("BBCL", "Нет такого", "№0", "ОКТЯБРЬ 2026"))
    assert caught.value.status == 404


def test_stroku_sdvinuli_posredi_zapisi() -> None:
    """Вставили строку между чтением и записью: KORT говорит, где смотреть, и пишет в журнал."""
    api = fake_api()
    api.before_update = lambda: october(api).insert_row(4)
    with pytest.raises(bill_book.BookError) as caught:
        issue(api)
    assert caught.value.status == 409 and caught.value.place is not None
    assert "Проверьте строку 6" in str(caught.value) and caught.value.changed == ["bill_no", "bill_date"]


def test_leglo_ne_to_kniga_vozvrashchaetsya() -> None:
    api = fake_api()

    def corrupt(requests):
        bad = [dict(item) for item in requests]
        bad[0] = {"updateCells": {**requests[0]["updateCells"], "rows": [{"values": [{"userEnteredValue": {"numberValue": 999}}]}]}}
        api.corrupt = None
        return bad

    api.corrupt = corrupt
    with pytest.raises(bill_book.BookError, match="Запись отменена: в «№ Счета» легло не то") as caught:
        issue(api)
    assert caught.value.status == 424
    tab = october(api)
    assert tab.cell(ROW, Z)["value"] is None and tab.cell(ROW, AA)["value"] is None, "книга как была"
    assert tab.cell(ROW, X)["value"] == "Еще рано"


def test_slomannaya_formula_vozvrashchaetsya() -> None:
    """Ячейка оказалась в разливе массива, которого проверки не увидели: запись ломает его - откат чинит."""
    api = fake_api()
    tab = october(api)
    tab.hidden_spill.add((ROW, Z))
    with pytest.raises(bill_book.BookError, match="сломалась формула в колонке X") as caught:
        issue(api)
    assert caught.value.status == 424
    assert tab.cell(ROW, Z)["value"] is None and tab.cell(ROW, AA)["value"] is None, "книга как была"
    assert tab.cell(2, X)["value"] is None, "массив в X3 снова цел"


def test_google_ne_podtverdil_zapis() -> None:
    api = fake_api()
    api.fail_update = bill_book.BookError("Google ответил 500", status=424)
    with pytest.raises(bill_book.BookError, match="Google не подтвердил запись") as caught:
        issue(api)
    assert caught.value.place is not None


def test_limit_google_otdelno_i_povtor_bezopasen(monkeypatch) -> None:
    """«Лимит» и после повторов - 429, а не «не подтвердил»: запрос отклонён, книга не тронута; повтор пишет."""
    monkeypatch.setattr(bill_book, "RATE_PAUSES", (0.0, 0.0))
    calls = []

    def limited():
        calls.append(1)
        raise RuntimeError("APIError: [429]: Quota exceeded for quota metric 'Read requests'")

    with pytest.raises(bill_book.BookError) as caught:
        bill_book._google(limited)
    assert caught.value.status == 429 and len(calls) == 3 and "проходит само" in str(caught.value)
    with pytest.raises(bill_book.BookError) as other:
        bill_book._google(lambda: (_ for _ in ()).throw(RuntimeError("APIError: [500]: backend error")))
    assert other.value.status == 424

    api = fake_api()
    api.fail_update = bill_book.BookError("Google временно ограничил чтение", status=429)
    with pytest.raises(bill_book.BookError) as refused:
        bill_book.save(api, SUMMARY, KENESARY, {"bill_no": ""}, {"bill_no": "312", "bill_date": "2026-10-22"}, PERIOD)
    assert refused.value.status == 429 and str(refused.value).endswith("Ничего не записано")
    assert october(api).cell(ROW, Z)["value"] is None
    api.fail_update = None
    saved = bill_book.save(api, SUMMARY, KENESARY, {"bill_no": ""}, {"bill_no": "312", "bill_date": "2026-10-22"}, PERIOD)
    assert saved.changed == ["bill_no", "bill_date", "bill_period"]


def test_znacheniya_proveryayutsya() -> None:
    api = fake_api()
    for bad in ("=SUM(1)", "+7", "@me"):
        with pytest.raises(bill_book.BookError) as caught:
            bill_book.save(api, SUMMARY, KENESARY, SHOWN, {"bill_no": bad, "bill_date": "2026-10-22"})
        assert caught.value.status == 422, bad
    with pytest.raises(bill_book.BookError, match="нужна дата"):
        bill_book.save(api, SUMMARY, KENESARY, SHOWN, {"bill_no": "312"})
    with pytest.raises(bill_book.BookError, match="нужна дата"):
        bill_book.save(api, SUMMARY, KENESARY, SHOWN, {"bill_no": "312", "bill_date": ""})
    with pytest.raises(bill_book.BookError, match="не дата"):
        bill_book.save(api, SUMMARY, KENESARY, SHOWN, {"bill_no": "312", "bill_date": "22.10.2026"})
    with pytest.raises(bill_book.BookError, match="длиннее"):
        bill_book.save(api, SUMMARY, KENESARY, SHOWN, {"note": "я" * 301})
    assert api.updates == []
    assert bill_book.wanted_values({"bill_no": "007"})["bill_no"].text == "007", "ведущие нули - строкой"
    assert bill_book.wanted_values({"bill_no": "248/1"})["bill_no"].text == "248/1", "не дата"
    assert bill_book.wanted_values({"bill_no": " 312 "})["bill_no"].number == 312


def test_snyat_otmetku() -> None:
    api = fake_api()
    first = issue(api)
    cleared = bill_book.save(api, SUMMARY, KENESARY, first.after, {"bill_no": "", "bill_date": ""})
    assert cleared.changed == ["bill_no", "bill_date"] and cleared.after["bill_no"] == ""
    assert cleared.state == "Еще рано" and cleared.warning == ""
    assert october(api).cell(ROW, Z)["entered"] == {}


def test_data_bez_formata_poluchaet_format() -> None:
    api = fake_api()
    october(api).cell(ROW, AA)["fmt"] = None
    saved = issue(api)
    request = next(item for item in api.updates[0] if item["updateCells"]["range"]["startColumnIndex"] == AA)
    assert request["updateCells"]["fields"] == "userEnteredValue,userEnteredFormat.numberFormat"
    assert saved.after["bill_date"] == "22.10.2026"


def test_kolonki_pereehali_mezhdu_otkrytiem_i_zapisyu() -> None:
    api = fake_api()
    bill_book.card(api, SUMMARY, KENESARY)  # книги и шапки - в кэше
    tab = october(api)
    tab.put(1, Z, "Дата выстав.\nСчета")
    tab.put(1, AA, "№ \nСчета")
    with pytest.raises(bill_book.BookError, match="переставили колонки") as caught:
        issue(api)
    assert caught.value.status == 409 and api.updates == []


def test_novuyu_vkladku_kort_nahodit_sam() -> None:
    api = fake_api()
    bill_book.card(api, SUMMARY, KENESARY)
    legal = api.books["legal-id"]
    november = month_tab(
        "Ноябрь", 999, [{"own": "BBCL", "customer": "ТОО KENESARY AGRO", "number": "№ЮО/44", "month": "НОЯБРЬ 2026"}]
    )
    legal.tabs["Ноябрь"] = november
    formula = legal.summary.cell(0, 0)["entered"]["formulaValue"]
    legal.summary.put(0, 0, None, formula=formula.replace("'Октябрь'!$A4:$BF", "'Октябрь'!$A4:$BF;'Ноябрь'!$A4:$BF"))
    card = bill_book.card(api, SUMMARY, bill_key("BBCL", "ТОО KENESARY AGRO", "№ЮО/44", "НОЯБРЬ 2026"))
    assert card["place"]["tab"] == "Ноябрь" and card["place"]["row"] == 4
