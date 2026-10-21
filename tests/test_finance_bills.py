"""«Счета → Не выставлены» и бот счетов: книга-сводка → KORT и канал Telegram.

Google и Telegram подменены: проверяется разбор книги и что бот шлёт, а не
сеть. Набор держит обещания раздела:

* колонка «Счет» находится по названию рядом с «№ Счета», «Счет за Период» и
  «Счет2» и после вставленной колонки; состояние - словами книги, незнакомое -
  в «Без срока», а не угадано;
* пост: договор строкой, месяцы через запятую и «июн-окт», юрлицо с большей
  просрочкой выше; длинный список режется с хвоста просрочки, ближайшие -
  целиком;
* одна сводка на день: второй проход и второй процесс не выкладывают её снова,
  отказ Telegram освобождает день, изменение книги правит пост, а не пишет новый;
* в выходные и до часа сводки - ничего; пустой список не выкладывается;
* раздел открыт только с правом «Счета», без подключённой сводки - пусто.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.finance import auth, bills_bot
from app.finance.config import finance_settings
from app.finance.contracts import bills
from app.finance.support_bot import TelegramError

BASE = "/api/v1/finance"
PASSWORD = "pass-12345"
CHAT = "@bbc_bills"

HEADER = [
    "Техн. 2Наша \nФирма",
    "Мес",
    "Техн. 1",
    "Наша \nФирма",
    "Вид \nУслуги",
    "Наш \nСотрудник",
    "Заказчик\n(Название Фирмы)",
    "Сумма\nДоговора",
    "Отдел",
    "№\nДоговора",
    "Период\nс (нач.)",
    "Доп. \nКоммент.",
    "Счет",
    "Счет за\nПериод (Коммент)",
    "№ \nСчета",
    "Факт Оплата",
    "Сумма Факт\nПоступ.",
    "Статус\n(Продл./Приостн./Разв./На исп.)",
    "Счет2",
]


def row(
    month_no: int,
    month: str,
    own: str,
    customer: str,
    number: str,
    bill: str,
    *,
    amount: str = "100 000",
    period: str = "",
    staff: str = "Меруерт",
    paid: str = "FALSE",
) -> list[str]:
    return [
        "", str(month_no), month, own, "Абон.П.", staff, customer, amount, "ОБО", number,
        period, "", bill, "", "", paid, "0", "", bill,
    ]


SHEET = [
    HEADER,
    row(6, "ИЮНЬ 2026", "BBC Omar", "ТОО Молфуд", "№RENT/93", "Просрочка более 5 дней", amount="50 000", period="04.06.2026"),
    row(7, "ИЮЛЬ 2026", "BBC Omar", "ТОО Молфуд", "№RENT/93", "Просрочка более 5 дней", amount="50 000", period="04.07.2026"),
    row(8, "АВГУСТ 2026", "BBC Omar", "ТОО Молфуд", "№RENT/93", "Просрочка более 5 дней", amount="50 000", period="04.08.2026"),
    row(10, "ОКТЯБРЬ 2026", "BBC Omar", "ТОО Молфуд", "№RENT/93", "Просрочка более 5 дней", amount="50 000", period="04.10.2026"),
    row(10, "ОКТЯБРЬ 2026", "BBC", "ADK Home", "№ОБО/110", "Просрочка 2–5 дней", period="17.10.2026", paid="Часть"),
    row(10, "ОКТЯБРЬ 2026", "BBC", "Алатау Пауэр", "№ОБО/146", "TRUE", period="25.09.2026"),
    row(10, "ОКТЯБРЬ 2026", "BBCL", "TrioSynergy", "№ЮО/58", "3 дня до выставления", period="24.10.2026"),
    row(10, "ОКТЯБРЬ 2026", "BBCL", "Meliora", "№ЮО/35-2", "Еще рано", period="26.10.2026"),
    row(8, "АВГУСТ 2026", "BBCL", "TrioSynergy", "№ЮО/136", ""),
    row(5, "Старые (до Май/Июня 2026)", "BBCS", "Интер Табыс", "№НО/108", "FALSE", period="15.05.2026"),
    ["", "", "", "", "", "", "", "", "", "", "", "", "FALSE"],  # пустая строка листа - не счёт
]


def test_sostoyanie_slovami_knigi() -> None:
    assert bills.state_of("TRUE") is None and bills.state_of("ИСТИНА") is None
    assert bills.state_of("Просрочка 1 день") == bills.OVERDUE
    assert bills.state_of("1 день до выставления") == bills.SOON
    assert bills.state_of("Ещё рано") == bills.EARLY
    for strange in ("", "FALSE", "Не нужен"):
        assert bills.state_of(strange) == bills.UNDATED, strange


def test_kniga_razbiraetsya_po_nazvaniyam() -> None:
    board = bills.build_board(SHEET, title="Осн.Общая сводка BBC 2026", worksheet="Сводка все ЮР лица")
    assert board.counts() == {bills.OVERDUE: 5, bills.SOON: 1, bills.EARLY: 1, bills.UNDATED: 2}
    assert board.rows == 10, "пустая строка листа - не договор"
    assert all(bill.customer != "Алатау Пауэр" for bill in board.bills), "выставленный счёт не в списке"
    first = board.bills[0]
    assert (first.customer, first.month, first.line) == ("ТОО Молфуд", "ИЮНЬ 2026", 2), "старое - первым"
    adk = next(bill for bill in board.bills if bill.customer == "ADK Home")
    assert adk.text == "Просрочка 2–5 дней" and adk.paid == "part" and adk.amount == Decimal("100000")
    assert board.total(bills.OVERDUE) == Decimal("300000")
    assert {bill.text for bill in board.of(bills.UNDATED)} == {"", "FALSE"}

    # В книгу вставили колонку перед «Счет»: читается то же самое.
    shifted = [line[:5] + ["вставили"] + line[5:] for line in SHEET]
    again = bills.build_board(shifted, title="", worksheet="Сводка все ЮР лица")
    assert [bill.out()["text"] for bill in again.bills] == [bill.out()["text"] for bill in board.bills]
    assert "«Счет»" in again.drift


def test_bez_kolonki_schet_otkaz() -> None:
    broken = [[cell for cell in HEADER if cell != "Счет"]]
    with pytest.raises(bills.BillsError, match="Счет"):
        bills.build_board(broken, title="", worksheet="Сводка")


NOW = datetime(2026, 10, 22, 5, 30, tzinfo=timezone.utc)  # четверг, 10:30 в Астане


def test_post_dogovor_strokoy() -> None:
    board = bills.build_board(SHEET, title="Осн.Общая сводка BBC 2026", worksheet="Сводка")
    post = bills_bot.render(board, NOW)
    assert post is not None
    assert post.text.startswith("<b>Не выставлены счета</b> · четверг, 22.10 10:30")
    # Окно 22.10 - сентябрь и октябрь: июнь-август Молфуда - старый хвост одной строкой.
    assert "<b>Просрочено: 2 счёта на 150 000</b>" in post.text
    assert "• <b>ТОО Молфуд</b> · №RENT/93 · окт · 50 000" in post.text
    assert "Старый хвост, раньше сентября: 3 счёта на 150 000 - список в KORT" in post.text
    assert "июн" not in post.text
    assert "• <b>ADK Home</b> · №ОБО/110 · окт · 100 000 · Меруерт" in post.text
    assert "• через 3 дня · <b>TrioSynergy</b> · №ЮО/58 · BBCL" in post.text
    assert "Без срока в книге: 2" in post.text
    assert "Meliora" not in post.text, "«Еще рано» в пост не идёт"
    assert chr(0x2014) not in post.text, "длинного тире в посте нет (CLAUDE.md)"


def test_dlinnyy_post_rezhetsya_s_hvosta_prosrochki() -> None:
    many = [HEADER] + [
        row(10, "ОКТЯБРЬ 2026", f"Юрлицо {index % 3}", f"ТОО Клиент с длинным названием {index}", f"№ОБО/{index}",
            "Просрочка более 5 дней", period="01.10.2026")
        for index in range(150)
    ] + [row(10, "ОКТЯБРЬ 2026", "BBCL", "TrioSynergy", "№ЮО/58", "1 день до выставления", period="23.10.2026")]
    post = bills_bot.render(bills.build_board(many, title="Сводка", worksheet="Сводка"), NOW)
    assert post is not None and len(post.text) <= 4096
    assert "через 1 день · <b>TrioSynergy</b>" in post.text, "ближайшие не режутся"
    assert "<b>Просрочено: 150 счетов" in post.text
    hidden = int(post.text.split("И ещё ", 1)[1].split(" ", 1)[0])
    shown = post.text.count("ТОО Клиент с длинным названием")
    assert shown + hidden == 150


def test_pustoy_spisok_ne_vykladyvaetsya() -> None:
    board = bills.build_board([HEADER, SHEET[6]], title="", worksheet="Сводка")
    assert bills_bot.render(board, NOW) is None


# ── Один пост на день ────────────────────────────────────────────────────────


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail: TelegramError | None = None
        self.next_id = 100

    def call(self, method: str, params: dict | None = None, **_: object) -> dict:
        if self.fail is not None:
            raise self.fail
        self.calls.append((method, params or {}))
        self.next_id += 1
        return {"message_id": self.next_id}

    def sent(self, method: str) -> list[dict]:
        return [params for name, params in self.calls if name == method]


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.core.config import settings
    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    monkeypatch.setattr(settings, "environment", "test")
    monkeypatch.setattr(finance_settings, "support_bot_token", "")
    monkeypatch.setattr(finance_settings, "bills_bot_token", "test-token")
    monkeypatch.setattr(finance_settings, "bills_chat_id", CHAT)
    monkeypatch.setattr(finance_settings, "bills_workspace_id", "")
    monkeypatch.setattr(finance_settings, "bills_hour", "10")
    monkeypatch.setattr(finance_settings, "bills_every", "3")
    monkeypatch.setattr(finance_settings, "app_url", "https://kort.cards")
    auth._ATTEMPTS.clear()
    bills._slots.clear()
    yield engine
    bills._slots.clear()
    auth._ATTEMPTS.clear()


@pytest.fixture
def app(finance_db) -> FastAPI:
    from finance_routes import finance_app

    return finance_app()


def client(app: FastAPI) -> TestClient:
    return TestClient(app, headers={"x-client-ip": "10.0.0.7", "user-agent": "pytest-agent"})


def register(app: FastAPI) -> TestClient:
    person = client(app)
    response = person.post(
        f"{BASE}/auth/register",
        json={"email": "owner@bbc.kz", "password": PASSWORD, "company": "BBC (тест)", "full_name": "Ермеков Нурболат"},
    )
    assert response.status_code == 201, response.text
    return person


@pytest.fixture
def book(monkeypatch):
    """Книга в памяти: тест правит `book["values"]` - «в книге поставили галочку»."""
    from app.finance.contracts import summary

    state = {"values": [list(line) for line in SHEET]}
    monkeypatch.setattr(
        summary, "_read",
        lambda spreadsheet_id, tab: summary.build_index(state["values"], title="Осн.Общая сводка BBC 2026", worksheet=tab),
    )
    monkeypatch.setattr(
        bills, "_read",
        lambda spreadsheet_id, tab: bills.build_board(
            state["values"], title="Осн.Общая сводка BBC 2026", worksheet=tab, spreadsheet_id=spreadsheet_id
        ),
    )
    return state


def connect(owner: TestClient) -> None:
    response = owner.post(
        f"{BASE}/contracts/setup/summary",
        json={"link": "https://docs.google.com/spreadsheets/d/abc123/edit", "worksheet": "Сводка все ЮР лица"},
    )
    assert response.status_code == 200, response.text


def fresh() -> None:
    """Книгу перечитать сразу: бот берёт прочитанную, свежая доедет к следующему проходу."""
    with bills._guard:
        slots = list(bills._slots.values())
    for slot in slots:
        board = bills._read(*slot.source)
        with bills._guard:
            slot.board, slot.at = board, time.monotonic()


def test_odna_svodka_v_den_i_pravka_posta(app: FastAPI, book) -> None:
    owner = register(app)
    connect(owner)
    api = FakeBot()

    assert bills_bot.tick(api, CHAT, NOW) == "posted"
    [post] = api.sent("sendMessage")
    assert post["chat_id"] == CHAT and post["parse_mode"] == "HTML"
    assert post["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://kort.cards/?s=invoices"
    assert "ТОО Молфуд" in post["text"]

    # Второй проход и «второй процесс» (тот же день в базе) - не выкладывают снова.
    assert bills_bot.tick(api, CHAT, NOW + timedelta(minutes=1)) == "same"
    assert bills_bot.claim(bills_bot.target_workspace(), bills_bot.slot_of(NOW)) is None
    assert len(api.sent("sendMessage")) == 1 and not api.sent("editMessageText")

    # В книге поставили галочку - пост правится, нового нет.
    for line in book["values"][1:5]:
        line[12] = "TRUE"
    fresh()
    assert bills_bot.tick(api, CHAT, NOW + timedelta(minutes=5)) == "edited"
    [edit] = api.sent("editMessageText")
    assert edit["message_id"] == 101 and "ТОО Молфуд" not in edit["text"] and "обновлено 10:35" in edit["text"]
    fresh()
    assert bills_bot.tick(api, CHAT, NOW + timedelta(minutes=6)) == "same"

    # Выставили всё - сводки дня в канале больше нет.
    for line in book["values"][1:]:
        if len(line) > 12:
            line[12] = "TRUE"
    fresh()
    assert bills_bot.tick(api, CHAT, NOW + timedelta(minutes=7)) == "deleted"
    assert api.sent("deleteMessage") == [{"chat_id": CHAT, "message_id": 101}]
    assert bills_bot.tick(api, CHAT, NOW + timedelta(minutes=8)) == "wait"

    # Завтра - новый пост.
    book["values"] = [list(line) for line in SHEET]
    fresh()
    assert bills_bot.tick(api, CHAT, NOW + timedelta(days=1)) == "posted"
    assert len(api.sent("sendMessage")) == 2


def test_otkaz_telegram_osvobozhdaet_den(app: FastAPI, book) -> None:
    owner = register(app)
    connect(owner)
    api = FakeBot()
    api.fail = TelegramError("нет связи с Telegram")
    with pytest.raises(TelegramError):
        bills_bot.tick(api, CHAT, NOW)
    api.fail = None
    assert bills_bot.tick(api, CHAT, NOW + timedelta(minutes=1)) == "posted"


def test_svodka_kazhdyy_den_v_svoy_chas(app: FastAPI, book) -> None:
    owner = register(app)
    connect(owner)
    api = FakeBot()
    early = datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)  # 08:00 в Астане
    late = datetime(2026, 10, 22, 16, 0, tzinfo=timezone.utc)  # 21:00
    saturday = datetime(2026, 10, 24, 5, 0, tzinfo=timezone.utc)  # 10:00
    # Первый проход запоминает список молча: сто строк придут сводкой, а не сообщениями.
    assert bills_bot.tick(api, CHAT, early) == "seeded"
    assert bills_bot.tick(api, CHAT, late) == "wait", "сводка дня после 20:00 не выходит"
    assert api.calls == []
    assert bills_bot.tick(api, CHAT, saturday) == "posted", "и в выходные"
    assert len(api.sent("sendMessage")) == 1


NIGHT = datetime(2026, 10, 21, 22, 0, tzinfo=timezone.utc)  # 03:00 в Астане, четверг


def test_novoe_srazu_a_vystavlennoe_udalyaetsya(app: FastAPI, book) -> None:
    owner = register(app)
    connect(owner)
    api = FakeBot()
    assert bills_bot.tick(api, CHAT, NIGHT) == "seeded"

    meliora = book["values"][8]
    meliora[12] = "1 день до выставления"  # в книге сменился день
    fresh()
    assert bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=1)) == "noticed"
    [first] = api.sent("sendMessage")
    assert "<b>Подошёл срок: 1</b>" in first["text"] and "• через 1 день · <b>Meliora</b>" in first["text"]
    assert "ТОО Молфуд" not in first["text"], "старое уже сказано - не повторяется"
    assert "disable_notification" not in first, "ночью - со звуком (решение владельца)"
    assert bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=2)) == "wait"

    # Старый хвост «новым» не бывает: в напоминании он одной строкой.
    book["values"][10][12] = "Просрочка более 5 дней"  # «Старые», май
    fresh()
    assert bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=2, seconds=30)) == "wait"

    # Два новых разом - одно сообщение; выставили один - он уходит из сообщения.
    meliora[12] = "Просрочка 1 день"
    trio = book["values"][9]
    trio[1], trio[2], trio[12] = "10", "ОКТЯБРЬ 2026", "Просрочка 1 день"
    fresh()
    assert bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=3)) == "noticed"
    second = api.sent("sendMessage")[-1]
    assert "<b>Срок прошёл: 2</b>" in second["text"]
    assert "<b>Meliora</b> · №ЮО/35-2 · окт" in second["text"] and "<b>TrioSynergy</b> · №ЮО/136 · окт" in second["text"]

    meliora[12] = "TRUE"
    fresh()
    assert bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=4)) == "notices-edited+notices-deleted"
    assert api.sent("deleteMessage") == [{"chat_id": CHAT, "message_id": 101}], "о Meliora одной - удалено"
    [edit] = api.sent("editMessageText")
    assert edit["message_id"] == 102 and "Meliora" not in edit["text"] and "TrioSynergy" in edit["text"]
    assert "Выставлено 1 из 2 · обновлено 03:04" in edit["text"]

    trio[12] = "TRUE"
    fresh()
    assert bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=5)) == "notices-deleted"
    assert [item["message_id"] for item in api.sent("deleteMessage")] == [101, 102]
    assert bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=6)) == "wait"

    # Сняли галочку - бот пишет о счёте заново.
    meliora[12] = "Просрочка 1 день"
    fresh()
    assert bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=7)) == "noticed"
    assert "Meliora" in api.sent("sendMessage")[-1]["text"]

    # Утренняя сводка не превращает уже сказанное в новые сообщения.
    assert bills_bot.tick(api, CHAT, NOW) == "posted"
    assert bills_bot.tick(api, CHAT, NOW + timedelta(minutes=1)) == "same"
    assert len(api.sent("sendMessage")) == 4


def test_napominanie_kazhdye_tri_chasa(app: FastAPI, book) -> None:
    """10, 13, 16, 19: новое напоминание звенит, прошлое удаляется; строка - что изменилось."""
    owner = register(app)
    connect(owner)
    api = FakeBot()
    morning = datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc)  # 10:00 в Астане
    assert bills_bot.tick(api, CHAT, morning) == "posted"
    assert bills_bot.tick(api, CHAT, morning + timedelta(hours=2, minutes=59)) == "same", "12:59 - ещё 10:00"

    assert bills_bot.tick(api, CHAT, morning + timedelta(hours=3)) == "posted"
    second = api.sent("sendMessage")[-1]
    assert second["text"].startswith("<b>Не выставлены счета</b> · четверг, 22.10 13:00\n<i>С 10:00 без изменений</i>")
    assert api.sent("deleteMessage") == [{"chat_id": CHAT, "message_id": 101}], "прошлое напоминание - прочь"

    book["values"][5][12] = "TRUE"  # ADK Home выставили
    book["values"][8][12] = "2 дня до выставления"  # Meliora подошла
    fresh()
    # Напоминание назвало Meliora - отдельного «новое» о ней нет.
    assert bills_bot.tick(api, CHAT, morning + timedelta(hours=6)) == "posted"
    assert "С 13:00: выставлено 1, добавилось 1" in api.sent("sendMessage")[-1]["text"]

    late = morning + timedelta(hours=10)  # 20:00 - напоминаний больше нет
    reminders = len([item for item in api.sent("sendMessage") if "Не выставлены счета" in item["text"]])
    assert "posted" not in bills_bot.tick(api, CHAT, late)
    assert bills_bot.tick(api, CHAT, morning + timedelta(hours=22)) != "posted", "08:00 - ещё рано"
    assert len([item for item in api.sent("sendMessage") if "Не выставлены счета" in item["text"]]) == reminders
    assert bills_bot.slot_of(morning + timedelta(hours=24)) == 2026102310
    assert bills_bot.tick(api, CHAT, morning + timedelta(hours=24)) == "posted"
    assert "<i>С 22.10 16:00" in api.sent("sendMessage")[-1]["text"], "вчерашнее - с датой"


def test_staroe_soobshchenie_ne_udalit_pravitsya(app: FastAPI, book) -> None:
    """Telegram не удаляет сообщения старше 48 часов - тогда «Всё выставлено» текстом."""
    owner = register(app)
    connect(owner)
    api = FakeBot()
    assert bills_bot.tick(api, CHAT, NIGHT) == "seeded"
    book["values"][8][12] = "Просрочка 1 день"
    fresh()
    assert bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=1)) == "noticed"
    book["values"][8][12] = "TRUE"
    fresh()

    refuse = TelegramError("Bad Request: message can't be deleted", code=400)
    real = api.call

    def call(method: str, params: dict | None = None, **extra: object) -> dict:
        if method == "deleteMessage":
            raise refuse
        return real(method, params, **extra)

    api.call = call  # type: ignore[method-assign]
    assert bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=2)) == "notices-edited"
    assert "Всё выставлено" in api.sent("editMessageText")[-1]["text"]


def test_novoe_ne_teryaetsya_pri_otkaze_telegram(app: FastAPI, book) -> None:
    owner = register(app)
    connect(owner)
    api = FakeBot()
    assert bills_bot.tick(api, CHAT, NIGHT) == "seeded"
    book["values"][7][12] = "Просрочка 1 день"  # TrioSynergy: подошёл срок → срок прошёл
    fresh()
    api.fail = TelegramError("нет связи с Telegram")
    with pytest.raises(TelegramError):
        bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=1))
    api.fail = None
    assert bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=2)) == "noticed"
    assert "TrioSynergy" in api.sent("sendMessage")[0]["text"]


def test_pustoy_list_ne_znachit_vse_vystavleno() -> None:
    with pytest.raises(bills.BillsError, match="ещё собирается"):
        bills.build_board([HEADER], title="", worksheet="Сводка")


def test_nevernyy_chas_vyklyuchaet_tolko_bota(monkeypatch) -> None:
    """«24» в FINANCE_BILLS_HOUR роняло весь API на старте (Render, 22.10.2026)."""
    from app.finance.config import FinanceSettings

    monkeypatch.setenv("FINANCE_BILLS_BOT_TOKEN", "test-token")
    monkeypatch.setenv("FINANCE_BILLS_CHAT_ID", CHAT)
    for raw in ("24", "-1", "десять", ""):
        monkeypatch.setenv("FINANCE_BILLS_HOUR", raw)
        settings = FinanceSettings()
        assert settings.bills_hour_value is None and not settings.bills_ready, raw
    monkeypatch.setenv("FINANCE_BILLS_HOUR", '"9"')
    for raw in ("0", "25", "три"):
        monkeypatch.setenv("FINANCE_BILLS_EVERY", raw)
        assert not FinanceSettings().bills_ready, raw
    monkeypatch.setenv("FINANCE_BILLS_EVERY", "2")
    assert FinanceSettings().bills_hour_value == 9 and FinanceSettings().bills_ready


def test_bez_svodki_bot_molchit(app: FastAPI, book) -> None:
    register(app)
    assert bills_bot.tick(FakeBot(), CHAT, NOW) == "no-workspace"


def test_razdel_ne_vystavleny(app: FastAPI, book) -> None:
    owner = register(app)
    empty = owner.get(f"{BASE}/bills").json()
    assert empty == {"source": None, "items": [], "counts": None, "channel": None}
    connect(owner)
    data = owner.get(f"{BASE}/bills").json()
    assert data["counts"]["overdue"] == 5 and data["counts"]["overdue_total"] == "300000"
    assert data["source"]["title"] == "Осн.Общая сводка BBC 2026" and data["source"]["error"] == ""
    assert data["items"][0]["customer"] == "ТОО Молфуд" and data["items"][0]["period_from"] == "2026-06-04"
    assert data["channel"]["url"] == "https://t.me/bbc_bills" and data["channel"]["hours"] == [10, 13, 16, 19]
    me = owner.get(f"{BASE}/auth/me").json()
    assert me["bills"] == {"overdue": 5, "soon": 1}


def test_razdel_tolko_s_pravom_scheta(app: FastAPI, book) -> None:
    owner = register(app)
    connect(owner)
    card = owner.post(
        f"{BASE}/people/employees", json={"full_name": "Юрист Без Счетов", "phone": "+77010000001", "access": True}
    )
    assert card.status_code == 201, card.text
    lawyer = client(app)
    assert lawyer.post(f"{BASE}/auth/phone/start", json={"phone": "+77010000001"}).json() == {"step": "set_password"}
    done = lawyer.post(f"{BASE}/auth/phone/set-password", json={"phone": "+77010000001", "password": "secret-123"})
    assert done.status_code == 200, done.text
    assert lawyer.get(f"{BASE}/auth/me").json()["access"]["invoices"] == "none"
    assert lawyer.get(f"{BASE}/bills").status_code == 403
    assert lawyer.get(f"{BASE}/auth/me").json()["bills"] is None

    access = owner.put(f"{BASE}/access/employee/{card.json()['id']}", json={"changes": {"invoices": "view"}})
    assert access.status_code == 200, access.text
    assert lawyer.get(f"{BASE}/bills").status_code == 200
    assert lawyer.get(f"{BASE}/auth/me").json()["bills"] == {"overdue": 5, "soon": 1}


def test_kanal_tolko_dlya_svoey_kompanii(app: FastAPI, book, monkeypatch) -> None:
    owner = register(app)
    connect(owner)
    monkeypatch.setattr(finance_settings, "bills_workspace_id", str(uuid.uuid4()))
    assert owner.get(f"{BASE}/bills").json()["channel"] is None
