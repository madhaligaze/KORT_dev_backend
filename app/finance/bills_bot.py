"""Бот «Счета к выставлению»: книга-сводка → канал Telegram, два поста - абонентская плата и аренда.

Как складывалось (22.10.2026, владелец и заказчик)
──────────────────────────────────────────────────
* Ночью - владелец: окно - текущий и прошлый месяц; пост по часам, прошлый
  удаляется; новое - сразу, круглосуточно и со звуком; выставленное уходит.
* Вечером - заказчик, шаблон v1.3: день счёта - «Период с», выставлен - «Счет»;
  только абонентская плата и аренда; не выставили - «вчерашнее», дальше
  «просрочено»; кнопка «прислать сейчас».
* Позже вечером - владелец: абонентская плата и аренда - отдельными постами,
  а не разделами одного; без служебных строк шаблона (черты, «шаблон v1.3»):
  шаблон был примером, а не образцом для копии. Имя клиента в посте -
  ссылка на карточку счёта в KORT, где его отмечают выставленным прямо в книге
  (`contracts/bill_book.py`).

Что попадает в пост
───────────────────
* **Галочка «Счет» (колонка Y) - выставлен**, в посте его нет.
* **День счёта - «Период с» (колонка S).** Счёт приходит в свой день, а не
  «через 3 дня». Не выставили - назавтра раздел «вчерашнее», дальше -
  «просрочено» с датой срока у строки. Срок - по дате, а не словами книги:
  так велел заказчик, и 22.10 слова книги считались от той же колонки S.
* **Вид - «Вид Услуги»**: «Абон.П.» - пост абонентской платы, «Аренда» - пост
  аренды; «Разовый» и «Прочее» не присылаются. Где вида нет («нет», пусто),
  аренду узнаём по отделу «Аренда». Прочее без вида не угадывается: строка
  «Без вида услуги» в посте абонентской платы называет такие счета, чтобы
  книгу поправили, а не чтобы счёт тихо пропал. Так же - «Без даты».
* **Окно - прошлый и текущий месяц** по «Периоду с» (владелец: 84
  просроченных разом - «странно и неудобно»). Старше - одной строкой.

Когда
─────
* **По часам** - с `FINANCE_BILLS_HOUR` через `FINANCE_BILLS_EVERY` до 20:00
  по Астане (9, 11 … 19), каждый день: оба поста заново, прошлые удаляются, в
  канале по одному актуальному. Строка под шапкой - что изменилось с прошлого,
  если изменилось.
* **Новое - сразу**, в любое время и со звуком: счёт впервые попал в пост
  (наступил его день, в книгу добавили строку, сняли отметку) - его пост
  выходит заново. Чаще раза в `NEW_GAP` не выходит, а правится: книгу
  заполняют строка за строкой, и каждая звенела бы.
* **Кнопка «Прислать сейчас»** - в закреплённом сообщении канала и под каждым
  постом: бот перечитывает книгу и, если что-то изменилось, присылает посты
  заново, без звука; итог («Новых счетов нет…») он кладёт в канал и через
  5 секунд убирает - иначе кнопка выглядела сломанной. `/bind` в канале
  ставит закреплённое сообщение заново.
* Между ними бот **правит текущие посты**: счёт отметили (в книге или в
  KORT) - строка уходит; выставлено всё - пост удаляется.

Не дважды
─────────
Час поста (`counters`: `bills.day`, `bills.post`, `bills.hash` - абонентская
плата, `….rent` - аренда) и «счёт уже был в посте» (`bill_notices`, группа
`due`) помнит база, а не процесс: перезапуск и два экземпляра API на время
выкладки не повторят пост. Telegram не принял - час и счета освобождаются,
следующий проход (через минуту) пробует снова.

Первый запуск запоминает текущий список молча (`bills.seeded`): иначе бот на
выкладке выдал бы весь список «новым». Переход с одного поста на два
(`bills.seeded` 2 → 3): висящий пост правится в пост абонентской платы, пост
аренды выходит рядом без звука.

Пост не длиннее `LIMIT` видимых знаков и `ENTITIES` выделений: Telegram режет
на 4096 и после сотни выделений перестаёт их показывать. Не влезло - уходят
самые старые просроченные, строкой «и ещё N - в KORT».
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.finance.config import finance_settings
from app.finance.contracts import bills
from app.finance.contracts.models import BillNotice, Counter, SummarySource
from app.finance.db import finance_session
from app.finance.layout import norm
from app.finance.support_bot import TZ, Bot, TelegramError

log = logging.getLogger(__name__)

#: Сколько видимых знаков в посте. Telegram принимает 4096.
LIMIT = 3900
#: Сколько выделений (жирное, ссылки, курсив) в посте: после сотни Telegram их не показывает.
ENTITIES = 100
#: Посты по часам - до этого часа (не включая): ночью выходит только новое.
LAST_HOUR = 20
#: Как часто проверять, не пора ли выложить или поправить пост.
TICK = 60
#: Новое заново не выкладывается чаще этого - пост правится.
NEW_GAP = 600.0
#: Кнопка «Прислать сейчас» чаще этого не присылает.
PULL_GAP = 30.0
#: Сколько ждать книгу по кнопке. Нажавшему уже ответили «Проверяю книгу…»,
#: так что ждать можно и холодное чтение (на свежем запуске - до 15 с).
PULL_WAIT = 25.0
#: Сколько висит в канале итог нажатия - прочитать и не засорять канал.
NOTE_SECONDS = 5.0
#: Как часто перечитывать сведения о канале (название, ссылка) для «Счетов».
CHANNEL_TTL = 3600.0
#: Запись «счёт был в посте» старше этого стирается: такой счёт давно вне окна.
NOTICE_KEEP = timedelta(days=100)

ABON = "abon"
RENT = "rent"
KINDS = (ABON, RENT)
SKIP = "skip"
UNKNOWN = "unknown"
TITLES = {ABON: "Абонентская плата", RENT: "Аренда"}

#: Счётчики поста каждого вида. Абонентская плата - прежние имена: висящий
#: пост одного вида правится в её пост, а не выходит заново.
DAYS = {ABON: "bills.day", RENT: "bills.day.rent"}
POSTS = {ABON: "bills.post", RENT: "bills.post.rent"}
HASHES = {ABON: "bills.hash", RENT: "bills.hash.rent"}
SEEDED = "bills.seeded"
PIN = "bills.pin"
COUNTERS = (*DAYS.values(), *POSTS.values(), *HASHES.values(), SEEDED, PIN)
#: `bills.seeded` нынешнего вида: 1 - сообщения «новое», 2 - один пост на оба вида.
SEED_VERSION = 3
#: Группа записи `bill_notices`: счёт был в посте. Прежние `soon`/`overdue` стираются при первом запуске.
DUE = "due"

PULL = "bills:now"
PULL_BUTTON = "Прислать сейчас"

TODAY = "today"
YESTERDAY = "yesterday"
LATE = "late"
STAGES = (TODAY, YESTERDAY, LATE)

MONTH_NUMBERS = {
    name: index
    for index, name in enumerate(
        (
            "январь", "февраль", "март", "апрель", "май", "июнь",
            "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
        ),
        start=1,
    )
}
#: «раньше сентября» - в строке хвоста.
MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _clean(value: str) -> str:
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def money(value: Decimal) -> str:
    """«2 345 000»; копейки - только если они есть."""
    whole = value.quantize(Decimal("1"))
    if whole == value:
        return f"{int(whole):,}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ")


def plural(count: int, one: str, few: str, many: str) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(count % 10, many)


def _bills(count: int) -> str:
    return f"{count} {plural(count, 'счёт', 'счёта', 'счетов')}"


def _sum(items: Iterable[bills.Bill]) -> Decimal:
    return sum((bill.amount or Decimal("0") for bill in items), Decimal("0"))


def app_url() -> str:
    """Адрес KORT для ссылок: `FINANCE_APP_URL`, иначе первый https из ALLOWED_ORIGINS."""
    raw = _clean(finance_settings.app_url).rstrip("/")
    if raw:
        return raw
    from app.core.config import settings

    return next((origin.rstrip("/") for origin in settings.cors_origins if origin.startswith("https://")), "")


def section_link() -> str:
    base = app_url()
    return f"{base}/?s=invoices" if base.startswith("https://") else ""


def bill_link(bill: bills.Bill) -> str:
    """Адрес карточки счёта в KORT: там его отмечают выставленным."""
    base = section_link()
    return f"{base}&bill={bill.id}" if base else ""


# ── Отбор ────────────────────────────────────────────────────────────────────


def kind_of(bill: bills.Bill) -> str:
    """Абонентская плата, аренда, «не присылать» или вид не указан.

    «Вид Услуги» главнее отдела. Отдел «Аренда» решает только там, где вида
    нет («нет», пусто): так заведена часть аренды BBC Omar (22.10.2026 -
    «ТОО NKK Строй», №RENT/98). Незнакомый вид не угадывается.
    """
    kind = norm(bill.kind)
    if kind.startswith("абон"):
        return ABON
    if kind.startswith("аренд"):
        return RENT
    if kind.startswith(("разов", "проч")):
        return SKIP
    if kind in ("", "нет") and norm(bill.department) == "аренда":
        return RENT
    return UNKNOWN


def month_of(bill: bills.Bill) -> tuple[int, int] | None:
    """Месяц строки (год, месяц) из «ОКТЯБРЬ 2026» - для счетов без «Периода с»."""
    words = bill.month.lower().split()
    if len(words) == 2 and words[0] in MONTH_NUMBERS and words[1].isdigit():
        return int(words[1]), MONTH_NUMBERS[words[0]]
    return None


def window_start(now: datetime) -> date:
    """Первый день окна - первое число прошлого месяца по Астане."""
    return bills.window_start(now.astimezone(TZ).date())


def stage_of(bill: bills.Bill, today: date) -> str:
    if bill.period_from == today:
        return TODAY
    if bill.period_from == today - timedelta(days=1):
        return YESTERDAY
    return LATE


@dataclass(frozen=True)
class Plan:
    """Что пора выставить на сегодня - и что в пост не попало, но назвать надо."""

    today: date
    start: date
    #: Счета, чей день наступил, - в окне.
    due: tuple[bills.Bill, ...]
    #: То же, но день раньше окна: одной строкой.
    tail: tuple[bills.Bill, ...]
    #: Без даты в «Периоде с».
    undated: tuple[bills.Bill, ...]
    #: День наступил, а вид услуги в книге не указан.
    unknown: tuple[bills.Bill, ...]

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(bill.key for bill in self.due)

    @property
    def extras(self) -> bool:
        return bool(self.tail or self.undated or self.unknown)


def plan(board: bills.Board, now: datetime, kind: str | None = None) -> Plan:
    """Счета поста `kind`; `None` - обоих. «Без вида услуги» - в посте абонентской платы."""
    today = now.astimezone(TZ).date()
    start = window_start(now)
    due: list[bills.Bill] = []
    tail: list[bills.Bill] = []
    undated: list[bills.Bill] = []
    unknown: list[bills.Bill] = []
    for bill in board.bills:
        sort = kind_of(bill)
        if sort == SKIP or (kind is not None and sort != kind and not (sort == UNKNOWN and kind == ABON)):
            continue
        when = bill.period_from
        if when is None:
            month = month_of(bill)
            if sort in KINDS and month is not None and month >= (start.year, start.month):
                undated.append(bill)
            continue
        if when > today:
            continue
        if when < start:
            if sort in KINDS:
                tail.append(bill)
            continue
        (due if sort in KINDS else unknown).append(bill)
    return Plan(today, start, tuple(due), tuple(tail), tuple(undated), tuple(unknown))


# ── Пост ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Section:
    """Юрлицо и день: сегодня и вчера - по периоду, просроченное - одним разделом."""

    stage: str
    firm: str
    items: tuple[bills.Bill, ...]

    @property
    def total(self) -> Decimal:
        return _sum(self.items)

    def head(self) -> str:
        title = f"<b>{_esc(self.firm or 'без юрлица')}</b>"
        if self.stage == LATE:
            return f"{title} · просрочено"
        first = self.items[0]
        period = f"{first.period_from:%d.%m}" + (f" - {first.period_to:%d.%m}" if first.period_to else "")
        return f"{title} · " + ("вчерашнее, " if self.stage == YESTERDAY else "") + period


def sections(items: Iterable[bills.Bill], today: date) -> list[Section]:
    """Сегодня, вчера, раньше; юрлица по алфавиту.

    Сегодня и вчера - по разделу на юрлицо и период, строки в порядке книги.
    Раньше - раздел на юрлицо, свежий срок выше: иначе каждый просроченный
    день был бы своим разделом, и пост не влез бы.
    """
    grouped: dict[tuple[Any, ...], list[bills.Bill]] = {}
    for bill in items:
        stage = stage_of(bill, today)
        until = bill.period_to.toordinal() if bill.period_to and stage != LATE else 0
        key = (STAGES.index(stage), bill.own.casefold(), bill.own, until)
        grouped.setdefault(key, []).append(bill)
    out: list[Section] = []
    for key in sorted(grouped):
        stage, _, firm, _ = key
        rows = grouped[key]
        if STAGES[stage] == LATE:
            rows.sort(key=lambda bill: (-bill.period_from.toordinal(), bill.line))
        else:
            rows.sort(key=lambda bill: bill.line)
        out.append(Section(STAGES[stage], firm, tuple(rows)))
    return out


def listed(board: bills.Board) -> tuple[date, list[bills.Bill]]:
    """Счета постов в порядке постов - для шаблона Excel раздела «Счета» (`contracts/bill_template.py`).

    Абонентская плата, затем аренда - по разделам поста; за ними те, что пост
    называет строкой: «без вида услуги» и «без даты». Финансисты заполняют те
    же счета, что видят в канале. Возвращает и день, на который список.
    """
    now = _now()
    due: list[bills.Bill] = []
    named: list[bills.Bill] = []
    for kind in KINDS:
        p = plan(board, now, kind)
        due.extend(bill for part in sections(p.due, p.today) for bill in part.items)
        named.extend((*p.unknown, *p.undated))
    named.sort(key=lambda bill: (bill.own.casefold(), bill.line))
    seen: set[str] = set()
    out: list[bills.Bill] = []
    for bill in (*due, *named):
        if bill.key not in seen:
            seen.add(bill.key)
            out.append(bill)
    return now.astimezone(TZ).date(), out


def _name(bill: bills.Bill) -> str:
    link = bill_link(bill)
    name = _esc(bill.customer)
    return f'<a href="{_esc(link)}">{name}</a>' if link else name


def _item(index: int, bill: bills.Bill, *, late: bool) -> str:
    number = f" - {_esc(bill.number)}" if bill.number else ""
    amount = f" = {money(bill.amount)} ₸" if bill.amount is not None else ""
    when = f" · с {bill.period_from:%d.%m}" if late and bill.period_from else ""
    return f"{index}) {_name(bill)}{number}{amount}{when}"


def _names(items: tuple[bills.Bill, ...], limit: int = 3) -> str:
    shown = ", ".join(_esc(bill.customer) for bill in items[:limit])
    rest = len(items) - limit
    return f"{shown} и ещё {rest}" if rest > 0 else shown


def _visible(text: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def _entities(text: str) -> int:
    return text.count("<b>") + text.count("<i>") + text.count("<a ")


def _fits(text: str) -> bool:
    return _visible(text) <= LIMIT and _entities(text) <= ENTITIES


@dataclass(frozen=True)
class Post:
    body: str
    text: str
    keys: frozenset[str]

    @property
    def digest(self) -> int:
        """Отпечаток поста - чтобы править его, только когда он изменился."""
        return int(hashlib.sha1(self.body.encode()).hexdigest()[:15], 16)


def _body(kind: str, p: Plan, parts: list[Section], shown: set[int], note: str) -> str:
    lines = [f"<b>{TITLES[kind]}: счета к выставлению ({p.today:%d.%m.%Y})</b>"]
    if note:
        lines.append(f"<i>{_esc(note)}</i>")
    dropped: list[bills.Bill] = []
    for part in parts:
        visible = [bill for bill in part.items if id(bill) in shown]
        if not visible:
            dropped.extend(part.items)
            continue
        late = part.stage == LATE
        lines.extend(("", part.head()))
        lines.extend(_item(index, bill, late=late) for index, bill in enumerate(visible, start=1))
        rest = len(part.items) - len(visible)
        if rest:
            lines.append(f"и ещё {rest} - в KORT")
        if len(part.items) > 1:
            lines.append(f"Итого: {money(part.total)} ₸")
    if dropped:
        lines.extend(("", f"Не вошли в пост: {_bills(len(dropped))} на {money(_sum(dropped))} ₸ - список в KORT"))
    if p.due:
        lines.extend(("", f"<b>ВСЕГО: {_bills(len(p.due))} = {money(_sum(p.due))} ₸</b>"))
    extra: list[str] = []
    if p.tail:
        extra.append(
            f"Раньше {MONTHS_GENITIVE[p.start.month - 1]}: ещё {_bills(len(p.tail))} "
            f"на {money(_sum(p.tail))} ₸ - список в KORT"
        )
    if p.undated:
        extra.append(f"Без даты «Период с» в книге: {_names(p.undated)}")
    if p.unknown:
        extra.append(f"Без вида услуги в книге: {_names(p.unknown)}")
    if extra:
        lines.append("")
        lines.extend(extra)
    return "\n".join(lines)


def render(
    board: bills.Board, now: datetime, kind: str = ABON, *, note: str = "", extras_alone: bool = False
) -> Post | None:
    """Пост вида `kind` или `None`, если напоминать не о чем.

    `note` - строка «что изменилось»; `extras_alone` - выложить и тогда, когда
    счетов на сегодня нет, а старое или «без даты» есть (первый пост дня, кнопка).
    """
    p = plan(board, now, kind)
    if not p.due and not (p.extras and extras_alone):
        return None
    parts = sections(p.due, p.today)
    # Не влезло - прячем с конца: самые старые просроченные, затем вчерашние.
    order = sorted(
        p.due, key=lambda bill: (STAGES.index(stage_of(bill, p.today)), -bill.period_from.toordinal(), bill.line)
    )
    count = len(order)
    while True:
        body = _body(kind, p, parts, {id(bill) for bill in order[:count]}, note)
        if count == 0 or _fits(body):
            break
        count -= 1
    return Post(body=body, text=body, keys=p.keys)


def since_line(previous: frozenset[str], previous_at: datetime, board: bills.Board, now: datetime, kind: str) -> str:
    """«С 13:00 выставлено 3, добавилось 1»; ничего не изменилось - пусто."""
    current = plan(board, now, kind).keys
    issued = len(previous & board.issued)
    added = len(current - previous)
    if not issued and not added:
        return ""
    before, local = previous_at.astimezone(TZ), now.astimezone(TZ)
    at = f"С {before:%H:%M}" if before.date() == local.date() else f"С {before:%d.%m %H:%M}"
    parts = []
    if issued:
        parts.append(f"выставлено {issued}")
    if added:
        parts.append(f"добавилось {added}")
    return f"{at} {', '.join(parts)}"


def _cleared(kind: str, now: datetime) -> str:
    """Telegram не дал удалить пост (старше 48 часов) - пост так и говорит, а не висит списком."""
    local = now.astimezone(TZ)
    return f"<b>{TITLES[kind]}: счета к выставлению ({local:%d.%m.%Y})</b>\n\nВсё выставлено."


def keyboard() -> dict[str, Any]:
    row: list[dict[str, str]] = [{"text": PULL_BUTTON, "callback_data": PULL}]
    link = section_link()
    if link:
        row.append({"text": "Открыть в KORT", "url": link})
    return {"inline_keyboard": [row]}


# ── Счётчики ─────────────────────────────────────────────────────────────────


def _ensure(workspace_id: uuid.UUID) -> None:
    for name in COUNTERS:
        try:
            with finance_session() as session:
                if session.get(Counter, (workspace_id, name)) is None:
                    session.add(Counter(workspace_id=workspace_id, name=name, value=0))
        except IntegrityError:
            pass  # завёл соседний процесс


def _value(workspace_id: uuid.UUID, name: str) -> int:
    with finance_session() as session:
        row = session.get(Counter, (workspace_id, name))
        return int(row.value) if row is not None else 0


def _set(workspace_id: uuid.UUID, name: str, value: int) -> None:
    with finance_session() as session:
        session.execute(
            sa.update(Counter).where(Counter.workspace_id == workspace_id, Counter.name == name).values(value=value)
        )


def _swap(workspace_id: uuid.UUID, name: str, was: int, value: int) -> bool:
    """`UPDATE … WHERE value = was` - ровно один процесс из двух получит `True`."""
    with finance_session() as session:
        result = session.execute(
            sa.update(Counter)
            .where(Counter.workspace_id == workspace_id, Counter.name == name, Counter.value == was)
            .values(value=value)
        )
        return (result.rowcount or 0) == 1


def slot_of(now: datetime) -> int | None:
    """Пост, который пора выложить, - `ГГГГММДДЧЧ` его часа; `None` - не время.

    Часы - с `FINANCE_BILLS_HOUR` через `FINANCE_BILLS_EVERY` до `LAST_HOUR`:
    9, 11, 13 … 19. API, поднявшийся в 10:30, выкладывает пост 9:00 сразу, а
    не ждёт 11:00.
    """
    hour, every = finance_settings.bills_hour_value, finance_settings.bills_every_value
    if hour is None or every is None:
        return None
    local = now.astimezone(TZ)
    if not hour <= local.hour < LAST_HOUR:
        return None
    at = hour + (local.hour - hour) // every * every
    return (local.year * 10000 + local.month * 100 + local.day) * 100 + at


def reminder_hours() -> list[int]:
    """Часы постов по Астане: [9, 11, 13, 15, 17, 19]; пусто - бот выключен неверной настройкой."""
    hour, every = finance_settings.bills_hour_value, finance_settings.bills_every_value
    if hour is None or every is None:
        return []
    return list(range(hour, LAST_HOUR, every))


def slot_time(slot: int) -> datetime:
    day, hour = divmod(slot, 100)
    year, rest = divmod(day, 10000)
    month, mday = divmod(rest, 100)
    return datetime(year, month, mday, hour, tzinfo=TZ)


def claim(workspace_id: uuid.UUID, day: int, kind: str = ABON) -> int | None:
    """Пост этого часа - этому процессу. Вернёт прежний час или `None`, если его уже взяли.

    `UPDATE … WHERE value < day` под блокировкой строки: второй процесс дождётся
    первого и увидит, что час уже не меньше, - и ничего не выложит.
    """
    _ensure(workspace_id)
    name = DAYS[kind]
    with finance_session() as session:
        previous = session.scalar(
            sa.select(Counter.value).where(Counter.workspace_id == workspace_id, Counter.name == name)
        )
        result = session.execute(
            sa.update(Counter)
            .where(Counter.workspace_id == workspace_id, Counter.name == name, Counter.value < day)
            .values(value=day)
        )
        return int(previous or 0) if (result.rowcount or 0) == 1 else None


def release(workspace_id: uuid.UUID, day: int, previous: int, kind: str = ABON) -> None:
    """Telegram не принял - час снова свободен, следующий проход повторит."""
    _swap(workspace_id, DAYS[kind], day, previous)


def target_workspace() -> uuid.UUID | None:
    """Чью книгу читает бот: `FINANCE_BILLS_WORKSPACE_ID`, иначе единственная подключённая.

    Две компании со сводкой и без переменной - не выбор наугад, а отказ:
    в канал BBC не должна уйти чужая книга.
    """
    raw = _clean(finance_settings.bills_workspace_id)
    if raw:
        try:
            return uuid.UUID(raw)
        except ValueError:
            log.error("счета: FINANCE_BILLS_WORKSPACE_ID - не id компании: %s", raw)
            return None
    with finance_session() as session:
        ids = list(session.scalars(sa.select(SummarySource.workspace_id)))
    if len(ids) == 1:
        return ids[0]
    if ids:
        log.error("счета: книг-сводок %s - задайте FINANCE_BILLS_WORKSPACE_ID", len(ids))
    return None


# ── Telegram ─────────────────────────────────────────────────────────────────


def _send(api: Bot, chat: str, text: str, *, silent: bool = False, markup: dict[str, Any] | None = None) -> int:
    sent = api.call(
        "sendMessage",
        {
            "chat_id": chat,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": markup or keyboard(),
            "link_preview_options": {"is_disabled": True},
            "disable_notification": True if silent else None,
        },
    )
    return int(sent["message_id"])


def _edit(api: Bot, chat: str, message_id: int, text: str, *, markup: dict[str, Any] | None = None) -> bool:
    """Поправить своё сообщение. «Не изменилось» - не ошибка; `False` - сообщения больше нет."""
    try:
        api.call(
            "editMessageText",
            {
                "chat_id": chat,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": markup or keyboard(),
                "link_preview_options": {"is_disabled": True},
            },
        )
    except TelegramError as exc:
        reason = str(exc).lower()
        if "not modified" in reason:
            return True
        if "not found" in reason:
            log.info("счета: сообщение %s удалили из канала - править нечего", message_id)
            return False
        raise
    return True


def _delete(api: Bot, chat: str, message_id: int) -> bool:
    """Удалить своё сообщение. `False` - Telegram не даёт (старше 48 часов): тогда оно правится."""
    try:
        api.call("deleteMessage", {"chat_id": chat, "message_id": message_id})
    except TelegramError as exc:
        if "not found" in str(exc).lower():
            return True  # уже удалили руками
        if exc.refused:
            return False
        raise
    return True


# ── «Счёт уже был в посте» ───────────────────────────────────────────────────


def _insert(session: Any, rows: list[dict[str, Any]]) -> set[str]:
    """Записать, чего ещё нет, и вернуть ключи, записанные именно этим вызовом.

    `ON CONFLICT DO NOTHING … RETURNING`: второй процесс, взявшийся за те же
    счета, получит пустой ответ и не выложит пост второй раз.
    """
    if not rows:
        return set()
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement = insert(BillNotice).values(rows).on_conflict_do_nothing().returning(BillNotice.key)
    return {key for (key,) in session.execute(statement)}


def _rows(workspace_id: uuid.UUID, keys: Iterable[str]) -> list[dict[str, Any]]:
    stamp = _now()
    return [
        {"workspace_id": workspace_id, "key": key, "bill_group": DUE, "notified_at": stamp, "line": ""}
        for key in sorted(keys)
    ]


def _known(workspace_id: uuid.UUID) -> set[str]:
    with finance_session() as session:
        return set(
            session.scalars(
                sa.select(BillNotice.key).where(BillNotice.workspace_id == workspace_id, BillNotice.bill_group == DUE)
            )
        )


def _drop(workspace_id: uuid.UUID, keys: Iterable[str]) -> None:
    keys = list(keys)
    if not keys:
        return
    with finance_session() as session:
        session.execute(
            sa.delete(BillNotice).where(
                BillNotice.workspace_id == workspace_id, BillNotice.bill_group == DUE, BillNotice.key.in_(keys)
            )
        )


def remember(workspace_id: uuid.UUID, board: bills.Board, now: datetime) -> None:
    """Всё, что сейчас в постах, - сказано: новым оно больше не станет.

    Первый запуск после сообщений «новое» заодно стирает их записи («подошёл
    срок», «срок прошёл»): у них другой смысл, и сравнивать с ними нечего.
    """
    seeded = _value(workspace_id, SEEDED)
    with finance_session() as session:
        if seeded < 2:
            session.execute(
                sa.delete(BillNotice).where(BillNotice.workspace_id == workspace_id, BillNotice.bill_group != DUE)
            )
        _insert(session, _rows(workspace_id, plan(board, now).keys))
    if seeded < SEED_VERSION:
        _set(workspace_id, SEEDED, SEED_VERSION)


def claim_new(workspace_id: uuid.UUID, board: bills.Board, now: datetime) -> set[str]:
    """Счета, которых ещё не было в посте, - за этим процессом. Пусто - нового нет."""
    fresh = plan(board, now).keys - _known(workspace_id)
    if not fresh:
        return set()
    with finance_session() as session:
        return _insert(session, _rows(workspace_id, fresh))


def forget_issued(workspace_id: uuid.UUID, board: bills.Board, now: datetime) -> None:
    """Выставленное - из памяти: снимут отметку, и счёт снова придёт новым.

    Пропавшая из книги строка не забывается: её могла на минуту потерять
    формула сводки, и вернувшись, она звенела бы как новая.
    """
    _drop(workspace_id, _known(workspace_id) & board.issued)
    with finance_session() as session:
        session.execute(
            sa.delete(BillNotice).where(
                BillNotice.workspace_id == workspace_id, BillNotice.notified_at < now - NOTICE_KEEP
            )
        )


# ── Проход ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Last:
    """Прошлый пост вида - для строки «С 13:00 выставлено 3» и паузы `NEW_GAP`."""

    at: datetime
    keys: frozenset[str]
    note: str
    sent: float


_last: dict[tuple[uuid.UUID, str], _Last] = {}
_last_lock = threading.Lock()
#: Проход, кнопка и `/bind` - по одному: иначе два поста разом.
_work = threading.Lock()
#: Когда кнопка присылала посты (`time.monotonic`).
_pulled: dict[uuid.UUID, float] = {}


def _first_of_day(slot: int) -> bool:
    return slot % 100 == finance_settings.bills_hour_value


def _publish(
    api: Bot,
    chat: str,
    workspace_id: uuid.UUID,
    board: bills.Board,
    now: datetime,
    kind: str,
    *,
    at: datetime,
    silent: bool = False,
    extras_alone: bool = False,
) -> str:
    """Новый пост вида - и прошлый прочь: в канале один актуальный. `empty` - напоминать не о чем."""
    old = _value(workspace_id, POSTS[kind])
    with _last_lock:
        last = _last.get((workspace_id, kind))
    note = since_line(last.keys, last.at, board, now, kind) if last is not None else ""
    post = render(board, now, kind, note=note, extras_alone=extras_alone)
    result = "empty"
    if post is None:
        _set(workspace_id, POSTS[kind], 0)
    else:
        message_id = _send(api, chat, post.text, silent=silent)
        _set(workspace_id, POSTS[kind], message_id)
        _set(workspace_id, HASHES[kind], post.digest)
        with _last_lock:
            _last[(workspace_id, kind)] = _Last(at=at, keys=post.keys, note=note, sent=time.monotonic())
        result = "posted"
    if old:
        _delete(api, chat, old)  # старше 48 часов не удалится - пусть висит, новый всё равно ниже
    remember(workspace_id, board, now)
    return result


def _edit_post(api: Bot, chat: str, workspace_id: uuid.UUID, board: bills.Board, now: datetime, kind: str) -> str:
    """Поправить текущий пост вида, если он изменился; выставлено всё - удалить. `""` - поста нет."""
    post_id = _value(workspace_id, POSTS[kind])
    if not post_id:
        return ""
    with _last_lock:
        last = _last.get((workspace_id, kind))
    post = render(
        board,
        now,
        kind,
        note=last.note if last is not None else "",
        # Пост, вышедший одним хвостом (первый дня, кнопка), хвостом и живёт до следующего.
        extras_alone=last is not None and not last.keys,
    )
    if post is None:
        if _delete(api, chat, post_id):
            _set(workspace_id, POSTS[kind], 0)
            return "deleted"
        post = Post(body="", text=_cleared(kind, now), keys=frozenset())
    if post.digest == _value(workspace_id, HASHES[kind]):
        return "same"
    if not _edit(api, chat, post_id, post.text):
        _set(workspace_id, POSTS[kind], 0)  # пост удалили руками - следующий выйдет по часу или новому
        return "deleted"
    _set(workspace_id, HASHES[kind], post.digest)
    return "edited"


def _announce(
    api: Bot, chat: str, workspace_id: uuid.UUID, board: bills.Board, now: datetime, kind: str, fresh: set[str]
) -> str:
    """Новое - сразу: пост вида заново и со звуком. Только что выходил - его правка, а не второй звонок."""
    with _last_lock:
        last = _last.get((workspace_id, kind))
    if last is not None and time.monotonic() - last.sent < NEW_GAP and _value(workspace_id, POSTS[kind]):
        return ""
    try:
        result = _publish(api, chat, workspace_id, board, now, kind, at=now)
    except TelegramError:
        _drop(workspace_id, fresh)
        raise
    return "new" if result == "posted" else result


def tick(api: Bot, chat: str, now: datetime | None = None) -> str:
    """Один проход: посты по часу, новое - сразу, правка текущих.

    Возвращает сделанное через «+» с видом поста (`abon-posted+rent-posted`,
    `rent-new`, `seeded`, `abon-edited`, `rent-deleted`, `abon-same`), `wait` -
    ничего; `no-workspace` / `no-board` - читать нечего.
    """
    now = now or _now()
    workspace_id = target_workspace()
    if workspace_id is None:
        return "no-workspace"
    with finance_session() as session:
        board, _ = bills.current(session, workspace_id)
    if board is None:
        return "no-board"
    _ensure(workspace_id)
    with _work:
        return _tick(api, chat, workspace_id, board, now)


def _tick(api: Bot, chat: str, workspace_id: uuid.UUID, board: bills.Board, now: datetime) -> str:
    done: list[str] = []
    fresh_post: set[str] = set()
    if _value(workspace_id, SEEDED) == 2:
        # Переход с одного поста на два: час аренды - тот же, что у висящего
        # поста, иначе в этот же час вышел бы второй пост аренды. Висящий пост
        # правится в пост абонентской платы ниже, аренда выходит рядом без звука.
        _set(workspace_id, DAYS[RENT], max(_value(workspace_id, DAYS[RENT]), _value(workspace_id, DAYS[ABON])))
        remember(workspace_id, board, now)
        done.append("seeded")
        if _value(workspace_id, POSTS[ABON]) and not _value(workspace_id, POSTS[RENT]):
            done.append("rent-" + _publish(api, chat, workspace_id, board, now, RENT, at=now, silent=True, extras_alone=True))
            fresh_post.add(RENT)
    hour = slot_of(now)
    if hour is not None:
        for kind in KINDS:
            previous = claim(workspace_id, hour, kind)
            if previous is None:
                continue
            try:
                result = _publish(api, chat, workspace_id, board, now, kind, at=slot_time(hour), extras_alone=_first_of_day(hour))
            except TelegramError:
                release(workspace_id, hour, previous, kind)
                raise
            done.append(f"{kind}-{result}")
            fresh_post.add(kind)
    if _value(workspace_id, SEEDED) < SEED_VERSION:
        # Первый запуск: всё, что сейчас в постах, - запомнить молча.
        remember(workspace_id, board, now)
        done.append("seeded")
    elif "seeded" not in done:
        fresh = claim_new(workspace_id, board, now)
        if fresh:
            p = plan(board, now)
            for kind in KINDS:
                mine = {bill.key for bill in p.due if bill.key in fresh and kind_of(bill) == kind}
                if not mine or kind in fresh_post:
                    continue
                announced = _announce(api, chat, workspace_id, board, now, kind, mine)
                if announced:
                    done.append(f"{kind}-{announced}")
                    fresh_post.add(kind)
    forget_issued(workspace_id, board, now)
    for kind in KINDS:
        if kind in fresh_post:
            continue
        edited = _edit_post(api, chat, workspace_id, board, now, kind)
        if edited and edited != "same":
            done.append(f"{kind}-{edited}")
        elif edited == "same":
            done.append(f"{kind}-same")
    return "+".join(done) or "wait"


def _counts(board: bills.Board, now: datetime) -> str:
    """«абонентская плата - 11 счетов, аренда - 32 счёта»: сколько счетов в каждом посте."""
    return ", ".join(f"{TITLES[kind].lower()} - {_bills(len(plan(board, now, kind).due))}" for kind in KINDS)


def pull(api: Bot, chat: str, now: datetime | None = None) -> str:
    """Кнопка «Прислать сейчас»: книга перечитывается; изменилось - посты выходят заново, без звука.

    Пост, который и так совпадает с книгой, не перевыкладывается: в канале он
    и без того последний. Возвращает итог словами - его бот на миг кладёт в
    канал (`_on_button`), чтобы кнопка не молчала.
    """
    now = now or _now()
    workspace_id = target_workspace()
    if workspace_id is None:
        return "Книга-сводка не подключена в KORT."
    recent = "Списки только что обновлены - они выше."
    with _work:
        if time.monotonic() - _pulled.get(workspace_id, -PULL_GAP) < PULL_GAP:
            return recent
    with finance_session() as session:
        board, _ = bills.current(session, workspace_id, wait=PULL_WAIT, force=True)
    if board is None:
        return "Книга не прочиталась - попробуйте через минуту."
    _ensure(workspace_id)
    with _work:
        if time.monotonic() - _pulled.get(workspace_id, -PULL_GAP) < PULL_GAP:
            return recent
        results: list[str] = []
        for kind in KINDS:
            post_id = _value(workspace_id, POSTS[kind])
            with _last_lock:
                last = _last.get((workspace_id, kind))
            post = render(board, now, kind, note=last.note if last is not None else "", extras_alone=True)
            # Совпадает с книгой и на месте (правка «не изменилось» - значит, есть) - не трогаем.
            if (
                post is not None
                and post_id
                and post.digest == _value(workspace_id, HASHES[kind])
                and _edit(api, chat, post_id, post.text)
            ):
                results.append("same")
                continue
            results.append(_publish(api, chat, workspace_id, board, now, kind, at=now, silent=True, extras_alone=True))
        _pulled[workspace_id] = time.monotonic()
    if all(result == "empty" for result in results):
        return "На сегодня все счета выставлены."
    if all(result in ("same", "empty") for result in results):
        return f"Новых счетов нет, списки выше актуальны: {_counts(board, now)}."
    return f"Списки обновлены: {_counts(board, now)}."


# ── Кнопка в канале ──────────────────────────────────────────────────────────


def panel_text() -> str:
    hours = ", ".join(f"{hour:02d}:00" for hour in reminder_hours())
    return (
        "<b>Счета к выставлению</b>\n"
        f"Абонентская плата и аренда - в {hours} и сразу, когда наступает день счёта."
    )


def panel_keyboard() -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": PULL_BUTTON, "callback_data": PULL}]]}


def bind(api: Bot, chat: str, workspace_id: uuid.UUID) -> int:
    """Закреплённое сообщение с кнопкой - заново; прежнее открепить и удалить."""
    old = _value(workspace_id, PIN)
    message_id = _send(api, chat, panel_text(), silent=True, markup=panel_keyboard())
    _set(workspace_id, PIN, message_id)
    try:
        api.call("pinChatMessage", {"chat_id": chat, "message_id": message_id, "disable_notification": True})
    except TelegramError as exc:
        log.warning("счета: кнопку не закрепить (%s) - боту нужно право править публикации", exc)
    if old > 0:
        with contextlib.suppress(TelegramError):
            api.call("unpinChatMessage", {"chat_id": chat, "message_id": old})
        with contextlib.suppress(TelegramError):
            _delete(api, chat, old)
    return message_id


def ensure_panel(api: Bot, chat: str) -> bool:
    """При запуске: кнопки в канале нет или её удалили - поставить. `False` - книги ещё нет, позже.

    Есть - правится (часы в тексте могли смениться); «сообщения нет» - ставится
    заново. Два процесса на выкладке не ставят две: `bills.pin` 0 → -1 берёт один.
    """
    workspace_id = target_workspace()
    if workspace_id is None:
        return False
    _ensure(workspace_id)
    with _work:
        current = _value(workspace_id, PIN)
        if current > 0 and _edit(api, chat, current, panel_text(), markup=panel_keyboard()):
            return True
        if current < 0 or not _swap(workspace_id, PIN, current, -1):
            return True  # ставит соседний процесс
        try:
            bind(api, chat, workspace_id)
        except TelegramError:
            _swap(workspace_id, PIN, -1, 0)
            raise
    return True


def _same_chat(where: dict[str, Any], chat: str) -> bool:
    if str(where.get("id")) == chat:
        return True
    username = str(where.get("username") or "")
    return bool(username) and chat.lower() == f"@{username}".lower()


def _later(seconds: float, action: Any) -> None:
    """Сделать через `seconds` секунд, не держа поток. Тесты подменяют - делают сразу."""
    timer = threading.Timer(seconds, action)
    timer.daemon = True
    timer.start()


def note(api: Bot, chat: str, text: str) -> None:
    """Короткое сообщение в канал без звука - и удалить его через `NOTE_SECONDS`.

    Ответ на кнопку: всплывающую надпись Telegram видит только нажавший, и
    человек в канале оставался с ощущением, что кнопка сломана (22.10.2026).
    """
    try:
        sent = api.call(
            "sendMessage",
            {"chat_id": chat, "text": text, "disable_notification": True, "link_preview_options": {"is_disabled": True}},
        )
    except TelegramError as exc:
        log.warning("счета: ответ на кнопку в канал не ушёл (%s)", exc)
        return
    message_id = int(sent["message_id"])

    def drop() -> None:
        try:
            _delete(api, chat, message_id)
        except TelegramError as exc:
            log.info("счета: ответ на кнопку не удалился (%s)", exc)

    _later(NOTE_SECONDS, drop)


def _stale(exc: TelegramError) -> bool:
    """Telegram отказал отвечать на нажатие: оно старое (бот не слышал его вовремя)."""
    reason = str(exc).lower()
    return exc.refused and ("too old" in reason or "query id is invalid" in reason)


def _on_button(api: Bot, chat: str, query: dict[str, Any]) -> None:
    """«Прислать сейчас»: сразу «Проверяю книгу…» нажавшему, итог - на миг в канале.

    Нажатие, на которое Telegram уже не даёт ответить, - старое: его сделали,
    пока бот не слышал канал. Такое не отрабатывается - иначе бот, вернувшись,
    вывалил бы в канал посты на давние нажатия.
    """

    def answer(text: str = "") -> bool:
        try:
            api.call("answerCallbackQuery", {"callback_query_id": query.get("id"), "text": text or None})
        except TelegramError as exc:
            log.info("счета: ответ на кнопку не ушёл (%s)", exc)
            return not _stale(exc)
        return True

    if query.get("data") != PULL:
        answer()
        return
    if not _same_chat((query.get("message") or {}).get("chat") or {}, chat):
        answer("Кнопка работает только в канале счетов")
        return
    if not answer("Проверяю книгу…"):
        log.info("счета: старое нажатие «Прислать сейчас» - не отрабатываю")
        return
    try:
        text = pull(api, chat)
    except TelegramError as exc:
        log.warning("счета: пост по кнопке не ушёл (%s)", exc)
        text = "Telegram не принял пост - попробуйте через минуту."
    note(api, chat, text)


def _on_channel_post(api: Bot, chat: str, post: dict[str, Any]) -> None:
    """`/bind` - поставить кнопку заново; служебное «закреплено» о своей кнопке - убрать."""
    if not _same_chat(post.get("chat") or {}, chat):
        return
    workspace_id = target_workspace()
    if workspace_id is None:
        return
    pinned = post.get("pinned_message")
    if pinned:
        if int(pinned.get("message_id") or 0) == _value(workspace_id, PIN):
            with contextlib.suppress(TelegramError):
                api.call("deleteMessage", {"chat_id": chat, "message_id": post.get("message_id")})
        return
    words = str(post.get("text") or "").split()
    if not words or words[0].split("@", 1)[0].lower() != "/bind":
        return
    with contextlib.suppress(TelegramError):
        api.call("deleteMessage", {"chat_id": chat, "message_id": post.get("message_id")})
    _ensure(workspace_id)
    with _work:
        bind(api, chat, workspace_id)


def handle(api: Bot, chat: str, update: dict[str, Any]) -> None:
    if update.get("callback_query"):
        _on_button(api, chat, update["callback_query"])
    elif update.get("channel_post"):
        _on_channel_post(api, chat, update["channel_post"])


# ── Канал для «Счетов» ───────────────────────────────────────────────────────

_channel: dict[str, Any] = {}
_channel_lock = threading.Lock()


def _remember_channel(api: Bot, chat: str) -> None:
    try:
        info = api.call("getChat", {"chat_id": chat}) or {}
    except TelegramError as exc:
        log.warning("счета: канал %s не прочитан (%s)", chat, exc)
        return
    username = info.get("username") or ""
    with _channel_lock:
        _channel.update(
            title=str(info.get("title") or ""),
            url=f"https://t.me/{username}" if username else str(info.get("invite_link") or ""),
            at=time.monotonic(),
        )


def channel_out(workspace_id: uuid.UUID) -> dict[str, Any] | None:
    """Канал бота - для строки в «Счетах», если бот читает книгу этой компании."""
    if not finance_settings.bills_ready:
        return None
    try:
        if target_workspace() != workspace_id:
            return None
    except Exception:  # noqa: BLE001 - строка о канале не роняет раздел
        return None
    with _channel_lock:
        title, url = _channel.get("title", ""), _channel.get("url", "")
    chat = _clean(finance_settings.bills_chat_id)
    if not url and chat.startswith("@"):
        url = f"https://t.me/{chat[1:]}"
    return {"title": title or chat, "url": url, "hours": reminder_hours()}


# ── Фоновый цикл ─────────────────────────────────────────────────────────────


async def _poll(api: Bot, chat: str) -> None:
    """Нажатия кнопки и посты канала (`/bind`). 409 - события читает другой экземпляр (выкладка)."""
    offset: int | None = None
    pause = 1.0
    while True:
        try:
            updates = await asyncio.to_thread(
                api.call,
                "getUpdates",
                {"offset": offset, "timeout": 50, "allowed_updates": ["callback_query", "channel_post"]},
                timeout=65,
            )
            pause = 1.0
        except TelegramError as exc:
            log.warning("счета: Telegram не отдал события (%s)", exc)
            await asyncio.sleep(pause)
            pause = min(pause * 2, 60.0)
            continue
        for update in updates or []:
            update_id = int(update.get("update_id") or 0)
            try:
                await asyncio.to_thread(handle, api, chat, update)
            except Exception:  # noqa: BLE001 - одно событие не останавливает опрос
                log.exception("счета: событие %s не разобрано", update_id)
            offset = update_id + 1


async def run_forever() -> None:
    """Фоновая задача API: посты по часам и новому, правка, кнопка - пока приложение живо."""
    if not finance_settings.bills_ready:
        return
    api = Bot(_clean(finance_settings.bills_bot_token))
    chat = _clean(finance_settings.bills_chat_id)
    # getUpdates не работает, пока у бота выставлен вебхук.
    with contextlib.suppress(TelegramError):
        await asyncio.to_thread(api.call, "deleteWebhook", {"drop_pending_updates": False})
    log.info("счета: бот запущен, канал %s", chat)
    polling = asyncio.create_task(_poll(api, chat))
    try:
        await asyncio.sleep(30)
        panel = False
        while True:
            try:
                with _channel_lock:
                    stale = time.monotonic() - _channel.get("at", -CHANNEL_TTL) >= CHANNEL_TTL
                if stale:
                    await asyncio.to_thread(_remember_channel, api, chat)
                if not panel:
                    panel = await asyncio.to_thread(ensure_panel, api, chat)
                done = await asyncio.to_thread(tick, api, chat)
                if done != "wait" and not all(part.endswith("-same") for part in done.split("+")):
                    log.info("счета: %s", done)
            except asyncio.CancelledError:
                raise
            except TelegramError as exc:
                log.warning("счета: Telegram не принял (%s)", exc)
            except Exception:  # noqa: BLE001 - фоновый проход не роняет приложение
                log.exception("счета: проход упал")
            await asyncio.sleep(TICK)
    finally:
        polling.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await polling


__all__ = [
    "KINDS",
    "LIMIT",
    "Plan",
    "Post",
    "bill_link",
    "bind",
    "channel_out",
    "claim",
    "claim_new",
    "ensure_panel",
    "handle",
    "kind_of",
    "plan",
    "pull",
    "release",
    "remember",
    "render",
    "run_forever",
    "sections",
    "since_line",
    "slot_of",
    "target_workspace",
    "tick",
]
