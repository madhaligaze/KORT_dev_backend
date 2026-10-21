"""Бот «Не выставлены счета»: книга-сводка → канал Telegram, круглосуточно.

Решения владельца 22.10.2026 (ночь), по порядку, как они складывались.

Что и когда
───────────
* **Окно - текущий и прошлый месяц.** Поимённо бот ведёт счета месяца строки
  не раньше прошлого (22.10 - сентябрь и октябрь, с 01.11 - октябрь и
  ноябрь). Всё старше - «старый хвост» одной строкой с числом и суммой: 84
  просроченных разом в каждом посте - шум, а хвост - это уборка книги, его
  список в KORT. Окно сдвигается само.
* **Напоминание каждые `FINANCE_BILLS_EVERY` часов днём** - с
  `FINANCE_BILLS_HOUR` до 20:00 по Астане (9, 12, 15, 18), каждый день.
  Новое напоминание звенит, а прошлое бот удаляет: в канале одно актуальное,
  копий нет. Повторяется только невыставленное, и строка под заголовком
  говорит, что изменилось с прошлого («С 13:00: выставлено 3, добавилось 1»).
  Между напоминаниями бот **правит текущее**: поставили галочку «Счет» -
  строка уходит.
* **Новое - сразу.** В любое время суток и в выходные, со звуком и ночью:
  счёт из окна попал в «В ближайшие дни» («подошёл срок») или в
  «Просрочено» («срок прошёл») - короткое сообщение именно о нём. О каждом
  счёте в каждой группе - один раз (`BillNotice`); названное напоминанием
  отдельным сообщением не повторяется.
* **Выставлено - сообщения нет.** Галочка в книге убирает счёт из
  сообщения; не осталось ни одного - сообщение (и напоминание) удаляется.
  Сняли галочку - бот напишет о счёте заново. Отметка одна - в книге: KORT
  её показывает, но не ставит.

Список - тот же, что в «Счета → Не выставлены» (`contracts/bills.py`): одна
книга, одни слова, одно чтение. В KORT - весь, с хвостом.

Не дважды
─────────
«Напоминание за этот час ушло» помнит база (`counters`: `bills.day` - номер
часа `ГГГГММДДЧЧ`, `bills.post`, `bills.hash`), «об этом счёте уже написали»
- тоже (`bill_notices`), а не память процесса: перезапуск или два экземпляра
API на время выкладки не повторят ни напоминание, ни сообщение. Telegram не
принял - час и счета освобождаются, и следующий проход (через минуту)
пробует снова. Строка «что изменилось» помнится в процессе: после
перезапуска одно напоминание выходит без неё.

Первый запуск запоминает текущий список молча (`bills.seeded`): иначе в канал
разом ушли бы все сотня строк, которые и так придут напоминанием.

Пустое напоминание не выкладывается: «всё выставлено» каждые три часа - шум.
Хвост без счетов из окна напоминается только первым напоминанием дня.

В пост влезает не больше `LIMIT` знаков (Telegram режет на 4096). Не влезло
- хвост договоров заменяется строкой «и ещё N - в KORT»: полный список - по
кнопке «Открыть в KORT».
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
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

#: Сколько знаков поста занимает список. Telegram принимает 4096; запас - на
#: шапку и подвал, которые дописываются после.
LIMIT = 3700
#: Напоминания - до этого часа (не включая): ночью звенит только «новое - сразу».
LAST_HOUR = 20
#: Как часто проверять, не пора ли выложить или поправить пост.
TICK = 60
#: Как часто перечитывать сведения о канале (название, ссылка) для «Счетов».
CHANNEL_TTL = 3600.0

DAY = "bills.day"
POST = "bills.post"
HASH = "bills.hash"
SEEDED = "bills.seeded"
#: О чём пишется «сразу»: подошёл срок и срок прошёл. «Еще рано» и «Без срока» - нет.
NOTIFY = (bills.SOON, bills.OVERDUE)

WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MONTHS = {
    "январь": "янв",
    "февраль": "фев",
    "март": "мар",
    "апрель": "апр",
    "май": "май",
    "июнь": "июн",
    "июль": "июл",
    "август": "авг",
    "сентябрь": "сен",
    "октябрь": "окт",
    "ноябрь": "ноя",
    "декабрь": "дек",
}
MONTH_NUMBERS = {name: index for index, name in enumerate(MONTHS, start=1)}
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


def money(value: Decimal) -> str:
    """«2 345 000»; копейки - только если они есть."""
    whole = value.quantize(Decimal("1"))
    if whole == value:
        return f"{int(whole):,}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ")


def app_url() -> str:
    """Адрес KORT для кнопки: `FINANCE_APP_URL`, иначе первый https из ALLOWED_ORIGINS."""
    raw = _clean(finance_settings.app_url).rstrip("/")
    if raw:
        return raw
    from app.core.config import settings

    return next((origin.rstrip("/") for origin in settings.cors_origins if origin.startswith("https://")), "")


def section_link() -> str:
    base = app_url()
    return f"{base}/?s=invoices" if base.startswith("https://") else ""


# ── Пост ─────────────────────────────────────────────────────────────────────


def month_label(bill: bills.Bill) -> str:
    """«ОКТЯБРЬ 2026» → «окт»; «Старые (до Май/Июня 2026)» → «старые»."""
    words = bill.month.lower().split()
    if not words:
        return bill.period_from.strftime("%m.%Y") if bill.period_from else "?"
    return MONTHS.get(words[0], words[0])


def month_of(bill: bills.Bill) -> tuple[int, int] | None:
    """Месяц строки (год, месяц): из «ОКТЯБРЬ 2026», иначе - из «Периода с».

    «Старые (до Май/Июня 2026)» - месяц по периоду: в окно такая строка не
    попадает. Ни того, ни другого - `None`, и строка идёт в хвост.
    """
    words = bill.month.lower().split()
    if len(words) == 2 and words[0] in MONTH_NUMBERS and words[1].isdigit():
        return int(words[1]), MONTH_NUMBERS[words[0]]
    if bill.period_from is not None:
        return bill.period_from.year, bill.period_from.month
    return None


def window_start(now: datetime) -> tuple[int, int]:
    """Первый месяц окна - прошлый месяц по Астане."""
    local = now.astimezone(TZ)
    return (local.year, local.month - 1) if local.month > 1 else (local.year - 1, 12)


def in_window(bill: bills.Bill, now: datetime) -> bool:
    """Счёт ведётся поимённо: его месяц - прошлый, текущий или позже."""
    month = month_of(bill)
    return month is not None and month >= window_start(now)


def months_text(items: list[bills.Bill]) -> str:
    """Месяцы договора через запятую; три и больше подряд - «июн-окт»."""
    ordered = sorted(items, key=lambda bill: (bill.month_no or 0, bill.period_from or date.min))
    parts: list[str] = []
    run: list[bills.Bill] = []

    def flush() -> None:
        if len(run) >= 3:
            parts.append(f"{month_label(run[0])}-{month_label(run[-1])}")
        else:
            parts.extend(month_label(bill) for bill in run)
        run.clear()

    for bill in ordered:
        regular = month_label(bill) in MONTHS.values() and bill.month_no is not None
        if run and regular and bill.month_no == (run[-1].month_no or 0) + 1:
            run.append(bill)
            continue
        flush()
        if regular:
            run.append(bill)
        else:
            parts.append(month_label(bill))
    flush()
    return ", ".join(dict.fromkeys(parts))


def person(staff: str) -> str:
    """Сотрудник из книги - если это человек: у аренды там стоит юрлицо («ТОО "…"»)."""
    text = staff.strip()
    if not text or '"' in text or text.upper().startswith(("ТОО", "TOO", "ИП ")):
        return ""
    return text


@dataclass(frozen=True)
class Contract:
    own: str
    customer: str
    number: str
    items: tuple[bills.Bill, ...]

    @property
    def total(self) -> Decimal:
        return sum((bill.amount or Decimal("0") for bill in self.items), Decimal("0"))

    def line(self) -> str:
        parts = [f"<b>{_esc(self.customer)}</b>"]
        if self.number:
            parts.append(_esc(self.number))
        parts.append(_esc(months_text(list(self.items))))
        if self.total:
            parts.append(money(self.total))
        staff = next((person(bill.staff) for bill in self.items if person(bill.staff)), "")
        if staff:
            parts.append(_esc(staff))
        return "• " + " · ".join(parts)


def contracts(items: Iterable[bills.Bill]) -> list[Contract]:
    """Строки книги - договорами: один договор за пять месяцев - одна строка поста."""
    grouped: dict[tuple[str, str, str], list[bills.Bill]] = {}
    for bill in items:
        grouped.setdefault((bill.own, bill.customer, bill.number), []).append(bill)
    return [Contract(own, customer, number, tuple(rows)) for (own, customer, number), rows in grouped.items()]


@dataclass(frozen=True)
class Post:
    body: str
    text: str

    @property
    def digest(self) -> int:
        """Отпечаток списка - чтобы править пост, только когда список изменился."""
        return int(hashlib.sha1(self.body.encode()).hexdigest()[:15], 16)


def plural(count: int, one: str, few: str, many: str) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(count % 10, many)


def _soon_line(bill: bills.Bill) -> str:
    """Срок - словами книги («3 дня до выставления» → «через 3 дня»), а не датой периода.

    Книги юрлиц считают срок по-своему (у одних от «Периода с», у других от
    «Числа выставления»), и дата периода в посте спорила бы с книгой на день.
    """
    text = bill.text.strip()
    marker = text.lower().find(" до выставления")
    when = f"через {text[:marker]}" if marker > 0 else text
    parts = (_esc(when), f"<b>{_esc(bill.customer)}</b>", _esc(bill.number), _esc(bill.own))
    return "• " + " · ".join(part for part in parts if part)


def split(board: bills.Board, now: datetime) -> tuple[list[bills.Bill], list[bills.Bill], list[bills.Bill]]:
    """Просрочено и «в ближайшие дни» из окна - и старый хвост (то же, но месяцем раньше окна)."""
    overdue = [bill for bill in board.of(bills.OVERDUE) if in_window(bill, now)]
    soon = [bill for bill in board.of(bills.SOON) if in_window(bill, now)]
    tail = [bill for bill in board.bills if bill.group in NOTIFY and not in_window(bill, now)]
    return overdue, soon, tail


def since_line(previous: frozenset[str], previous_at: datetime, board: bills.Board, now: datetime) -> str:
    """«С 13:00: выставлено 3, добавилось 1» - чем это напоминание отличается от прошлого."""
    overdue, soon, _ = split(board, now)
    current = {bill.key for bill in overdue + soon}
    issued = len(previous & board.issued)
    added = len(current - previous)
    before, local = previous_at.astimezone(TZ), now.astimezone(TZ)
    at = f"С {before:%H:%M}" if before.date() == local.date() else f"С {before:%d.%m %H:%M}"
    parts = []
    if issued:
        parts.append(f"выставлено {issued}")
    if added:
        parts.append(f"добавилось {added}")
    return f"{at}: {', '.join(parts)}" if parts else f"{at} без изменений"


def render(
    board: bills.Board,
    now: datetime,
    *,
    at: datetime | None = None,
    edited: bool = False,
    note: str = "",
    tail_alone: bool = False,
) -> Post | None:
    """Напоминание или `None`, если напоминать не о чем.

    `at` - час напоминания в заголовке (при правке - тот же, что был);
    `note` - строка «что изменилось»; `tail_alone` - выложить и тогда, когда
    в окне пусто, а хвост есть (первое напоминание дня).
    """
    overdue, soon, tail = split(board, now)
    if not overdue and not soon and not (tail and tail_alone):
        return None
    lines: list[str] = []

    if overdue:
        count = len(overdue)
        total = sum((bill.amount or Decimal("0") for bill in overdue), Decimal("0"))
        lines.append(f"<b>Просрочено: {count} {plural(count, 'счёт', 'счёта', 'счетов')} на {money(total)}</b>")
        by_own: dict[str, list[Contract]] = {}
        for contract in contracts(overdue):
            by_own.setdefault(contract.own, []).append(contract)
        # Юрлицо, где просрочено больше, - выше: с него и начинать.
        firms = sorted(by_own.items(), key=lambda pair: (-sum(len(c.items) for c in pair[1]), pair[0]))
        for firm, items in firms:
            lines.append("")
            lines.append(f"<i>{_esc(firm or 'Без юрлица')}</i>")
            lines.extend(contract.line() for contract in items)
    if soon:
        lines.append("")
        lines.append(f"<b>В ближайшие дни: {len(soon)}</b>")
        lines.extend(_soon_line(bill) for bill in soon)

    # Не влезло - режем хвост просрочки, а не ближайшие: их день наступает
    # сейчас, а просрочка целиком есть в KORT.
    hidden = 0
    while len("\n".join(lines)) > LIMIT:
        soon_at = next((i for i, line in enumerate(lines) if line.startswith("<b>В ближайшие дни")), len(lines))
        cut = next((i for i in range(soon_at - 1, -1, -1) if lines[i].startswith("• ")), None)
        if cut is None:
            break
        del lines[cut]
        hidden += 1
        if cut >= 2 and lines[cut - 1].startswith("<i>") and (cut >= len(lines) or not lines[cut].startswith("• ")):
            del lines[cut - 2 : cut]  # юрлицо осталось без строк - снять и его заголовок
    if hidden:
        lines.append("")
        lines.append(f"И ещё {hidden} {plural(hidden, 'договор', 'договора', 'договоров')} - полный список в KORT")
    if tail:
        start = window_start(now)
        total = sum((bill.amount or Decimal("0") for bill in tail), Decimal("0"))
        lines.append("")
        lines.append(
            f"Старый хвост, раньше {MONTHS_GENITIVE[start[1] - 1]}: {len(tail)} "
            f"{plural(len(tail), 'счёт', 'счёта', 'счетов')} на {money(total)} - список в KORT"
        )
    undated = len(board.of(bills.UNDATED))
    if undated:
        lines.append("")
        lines.append(f"Без срока в книге: {undated} - в KORT, «Счета → Не выставлены»")
    body = "\n".join(lines).strip("\n")

    local = now.astimezone(TZ)
    when = (at or now).astimezone(TZ)
    head = f"<b>Не выставлены счета</b> · {WEEKDAYS[when.weekday()]}, {when:%d.%m %H:%M}"
    if note:
        head += f"\n<i>{_esc(note)}</i>"
    source = _esc(board.title or "книга-сводка")
    foot = f"<i>По книге «{source}»" + (f" · обновлено {local:%H:%M}" if edited else "") + "</i>"
    return Post(body=body, text=f"{head}\n\n{body}\n\n{foot}")


def keyboard() -> dict[str, Any] | None:
    link = section_link()
    if not link:
        return None
    return {"inline_keyboard": [[{"text": "Открыть в KORT", "url": link}]]}


# ── Не дважды ────────────────────────────────────────────────────────────────


def _ensure(workspace_id: uuid.UUID) -> None:
    for name in (DAY, POST, HASH, SEEDED):
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


def slot_of(now: datetime) -> int | None:
    """Напоминание, которое пора выложить, - `ГГГГММДДЧЧ` его часа; `None` - не время.

    Часы - с `FINANCE_BILLS_HOUR` через `FINANCE_BILLS_EVERY` до `LAST_HOUR`:
    9, 12, 15, 18. API, поднявшийся в 10:30, выкладывает напоминание 9:00
    сразу, а не ждёт 13:00.
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
    """Часы напоминаний по Астане: [9, 12, 15, 18]; пусто - бот выключен неверной настройкой."""
    hour, every = finance_settings.bills_hour_value, finance_settings.bills_every_value
    if hour is None or every is None:
        return []
    return list(range(hour, LAST_HOUR, every))


def slot_time(slot: int) -> datetime:
    day, hour = divmod(slot, 100)
    year, rest = divmod(day, 10000)
    month, mday = divmod(rest, 100)
    return datetime(year, month, mday, hour, tzinfo=TZ)


def claim(workspace_id: uuid.UUID, day: int) -> int | None:
    """Напоминание этого часа - этому процессу. Вернёт прежний час или `None`, если его уже взяли.

    `UPDATE … WHERE value < day` под блокировкой строки: второй процесс дождётся
    первого и увидит, что час уже не меньше, - и ничего не выложит.
    """
    _ensure(workspace_id)
    with finance_session() as session:
        previous = session.scalar(
            sa.select(Counter.value).where(Counter.workspace_id == workspace_id, Counter.name == DAY)
        )
        result = session.execute(
            sa.update(Counter)
            .where(Counter.workspace_id == workspace_id, Counter.name == DAY, Counter.value < day)
            .values(value=day)
        )
        return int(previous or 0) if (result.rowcount or 0) == 1 else None


def release(workspace_id: uuid.UUID, day: int, previous: int) -> None:
    """Telegram не принял - час снова свободен, следующий проход повторит."""
    with finance_session() as session:
        session.execute(
            sa.update(Counter)
            .where(Counter.workspace_id == workspace_id, Counter.name == DAY, Counter.value == day)
            .values(value=previous)
        )


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


def _send(api: Bot, chat: str, text: str) -> int:
    sent = api.call(
        "sendMessage",
        {
            "chat_id": chat,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": keyboard(),
            "link_preview_options": {"is_disabled": True},
        },
    )
    return int(sent["message_id"])


def _edit(api: Bot, chat: str, message_id: int, text: str) -> None:
    """Поправить своё сообщение. «Не изменилось» и «сообщения больше нет» - не ошибка."""
    try:
        api.call(
            "editMessageText",
            {
                "chat_id": chat,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": keyboard(),
                "link_preview_options": {"is_disabled": True},
            },
        )
    except TelegramError as exc:
        reason = str(exc).lower()
        if "not modified" in reason:
            return
        if "not found" in reason:
            log.info("счета: сообщение %s удалили из канала - править нечего", message_id)
            return
        raise


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


# ── Новое - сразу ────────────────────────────────────────────────────────────

#: Сколько дней бот правит свои сообщения «новое»: позже их уже никто не читает.
NOTICE_DAYS = 7
NOTICE_TITLES = {bills.OVERDUE: "Срок прошёл", bills.SOON: "Подошёл срок"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def notice_line(bill: bills.Bill) -> str:
    """Счёт строкой сообщения «новое»: строки не сводятся в договоры - каждая уходит по своей галочке."""
    if bill.group == bills.SOON:
        return _soon_line(bill)
    parts = [f"<b>{_esc(bill.customer)}</b>", _esc(bill.number), _esc(month_label(bill))]
    if bill.amount:
        parts.append(money(bill.amount))
    parts.extend((_esc(bill.own), _esc(person(bill.staff))))
    return "• " + " · ".join(part for part in parts if part)


@dataclass(frozen=True)
class NoticeRow:
    group: str
    line: str
    done: bool


def notice_text(rows: list[NoticeRow], at: datetime, *, edited_at: datetime | None = None) -> str:
    """Сообщение «новое»: просрочка выше; выставленное из него уходит, итог - внизу.

    «Всё выставлено» в тексте бывает, только если Telegram не дал удалить
    сообщение (старше 48 часов): обычно оно удаляется целиком.
    """
    local = (at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)).astimezone(TZ)
    lines = [f"<b>Счета: новое</b> · {local:%d.%m %H:%M}"]
    pending = [row for row in rows if not row.done]
    for group in (bills.OVERDUE, bills.SOON):
        part = [row for row in pending if row.group == group]
        if part:
            lines.extend(("", f"<b>{NOTICE_TITLES[group]}: {len(part)}</b>"))
            lines.extend(row.line for row in part)
    done = len(rows) - len(pending)
    if done and edited_at is not None:
        tail = f"Выставлено {done} из {len(rows)}" if pending else "Всё выставлено"
        lines.extend(("", f"<i>{tail} · обновлено {edited_at.astimezone(TZ):%H:%M}</i>"))
    return "\n".join(lines)


def _insert(session: Any, rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """Записать, чего ещё нет, и вернуть записанное именно этим вызовом.

    `ON CONFLICT DO NOTHING … RETURNING`: второй процесс, взявшийся за те же
    счета, получит пустой ответ и не напишет о них второй раз.
    """
    if not rows:
        return set()
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement = (
        insert(BillNotice).values(rows).on_conflict_do_nothing().returning(BillNotice.key, BillNotice.bill_group)
    )
    return {(key, group) for key, group in session.execute(statement)}


def _unique(items: Iterable[bills.Bill]) -> list[bills.Bill]:
    seen: dict[tuple[str, str], bills.Bill] = {}
    for bill in items:
        if bill.group in NOTIFY:
            seen.setdefault((bill.key, bill.group), bill)
    return list(seen.values())


def remember(workspace_id: uuid.UUID, board: bills.Board) -> None:
    """Всё, что сейчас «подошёл срок» и «срок прошёл», - сказано (сводкой или первым запуском)."""
    stamp = _now()
    rows = [
        {"workspace_id": workspace_id, "key": bill.key, "bill_group": bill.group, "notified_at": stamp, "line": ""}
        for bill in _unique(board.bills)
    ]
    with finance_session() as session:
        _insert(session, rows)
    _set(workspace_id, SEEDED, 1)


def _chunks(items: list[bills.Bill], at: datetime) -> list[list[bills.Bill]]:
    """Счета - сообщениями не длиннее `LIMIT`: ночью после смены дня их бывает много."""
    out: list[list[bills.Bill]] = [[]]
    for bill in items:
        trial = out[-1] + [bill]
        text = notice_text([NoticeRow(item.group, notice_line(item), False) for item in trial], at)
        if out[-1] and len(text) > LIMIT:
            out.append([bill])
        else:
            out[-1] = trial
    return [chunk for chunk in out if chunk]


def notify_new(api: Bot, chat: str, workspace_id: uuid.UUID, board: bills.Board, now: datetime) -> int:
    """Счета, впервые попавшие в «подошёл срок» или «срок прошёл», - сообщением сразу.

    Возвращает, о скольких счетах написано. Telegram не принял - эти счета
    снова «не сказаны», и следующий проход пишет о них заново.
    """
    # Только окно: старый хвост «новым» не бывает - о нём строка в напоминании.
    fresh = [bill for bill in _unique(board.bills) if in_window(bill, now)]
    if not fresh:
        return 0
    with finance_session() as session:
        known = {
            (key, group)
            for key, group in session.execute(
                sa.select(BillNotice.key, BillNotice.bill_group).where(BillNotice.workspace_id == workspace_id)
            )
        }
    new = [bill for bill in fresh if (bill.key, bill.group) not in known]
    if not new:
        return 0
    stamp = _now()
    # Порядок строк сообщения живёт в `notified_at` (+1 мкс на строку): по нему
    # правка собирает сообщение так же, как оно ушло.
    rows = [
        {
            "workspace_id": workspace_id,
            "key": bill.key,
            "bill_group": bill.group,
            "notified_at": stamp + timedelta(microseconds=index),
            "line": notice_line(bill),
        }
        for index, bill in enumerate(new)
    ]
    with finance_session() as session:
        claimed = _insert(session, rows)
    mine = [bill for bill in new if (bill.key, bill.group) in claimed]
    chunks = _chunks(mine, now)
    for index, chunk in enumerate(chunks):
        text = notice_text([NoticeRow(bill.group, notice_line(bill), False) for bill in chunk], now)
        try:
            message_id = _send(api, chat, text)
        except TelegramError:
            unsent = [bill for rest in chunks[index:] for bill in rest]
            with finance_session() as session:
                for bill in unsent:
                    session.execute(
                        sa.delete(BillNotice).where(
                            BillNotice.workspace_id == workspace_id,
                            BillNotice.key == bill.key,
                            BillNotice.bill_group == bill.group,
                        )
                    )
            raise
        with finance_session() as session:
            for bill in chunk:
                session.execute(
                    sa.update(BillNotice)
                    .where(
                        BillNotice.workspace_id == workspace_id,
                        BillNotice.key == bill.key,
                        BillNotice.bill_group == bill.group,
                    )
                    .values(message_id=message_id)
                )
    return len(mine)


def _forget(workspace_id: uuid.UUID, items: Iterable[Any]) -> None:
    with finance_session() as session:
        for row in items:
            session.execute(
                sa.delete(BillNotice).where(
                    BillNotice.workspace_id == workspace_id,
                    BillNotice.key == row.key,
                    BillNotice.bill_group == row.bill_group,
                )
            )


def refresh_notices(
    api: Bot, chat: str, workspace_id: uuid.UUID, board: bills.Board, now: datetime
) -> tuple[int, int]:
    """Галочка в книге - счёт уходит из сообщения «новое»; выставлено всё - сообщение удаляется.

    Решение владельца 22.10.2026: выставленное не висит в канале. Сообщение
    удаляется вместе с памятью о нём - снимут галочку, и бот напишет о счёте
    заново. Telegram не даёт удалить сообщение старше 48 часов - тогда оно
    правится на «Всё выставлено».

    Пропавшая из книги строка не считается выставленной: её могла на минуту
    потерять формула сводки. Возвращает (поправлено, удалено) сообщений.
    """
    open_keys = {bill.key for bill in board.bills}
    with finance_session() as session:
        rows = list(
            session.execute(
                sa.select(BillNotice.key, BillNotice.bill_group, BillNotice.line, BillNotice.message_id,
                          BillNotice.notified_at, BillNotice.done_at)
                .where(
                    BillNotice.workspace_id == workspace_id,
                    BillNotice.message_id.is_not(None),
                    BillNotice.notified_at >= _now() - timedelta(days=NOTICE_DAYS),
                )
                .order_by(BillNotice.notified_at)
            )
        )
    messages: dict[int, list[Any]] = {}
    for row in rows:
        messages.setdefault(int(row.message_id), []).append(row)
    edited = deleted = 0
    for message_id, items in messages.items():
        states = []
        for row in items:
            was = row.done_at is not None
            if row.key in board.issued:
                states.append(True)
            elif row.key in open_keys:
                states.append(False)
            else:
                states.append(was)
        if states == [row.done_at is not None for row in items]:
            continue
        if all(states) and _delete(api, chat, message_id):
            _forget(workspace_id, items)
            deleted += 1
            continue
        text = notice_text(
            [NoticeRow(row.bill_group, row.line, done) for row, done in zip(items, states)],
            items[0].notified_at,
            edited_at=now,
        )
        _edit(api, chat, message_id, text)
        with finance_session() as session:
            for row, done in zip(items, states):
                session.execute(
                    sa.update(BillNotice)
                    .where(
                        BillNotice.workspace_id == workspace_id,
                        BillNotice.key == row.key,
                        BillNotice.bill_group == row.bill_group,
                    )
                    .values(done_at=(row.done_at or now) if done else None)
                )
        edited += 1
    return edited, deleted


# ── Проход ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Last:
    """Прошлое напоминание - для строки «С 13:00: выставлено 3, добавилось 1»."""

    at: datetime
    keys: frozenset[str]
    note: str


_last: dict[uuid.UUID, _Last] = {}
_last_lock = threading.Lock()


def _first_of_day(slot: int) -> bool:
    return slot % 100 == finance_settings.bills_hour_value


def _edit_reminder(api: Bot, chat: str, workspace_id: uuid.UUID, board: bills.Board, now: datetime) -> str:
    """Поправить текущее напоминание, если список изменился; выставлено всё - удалить его.

    `""` - напоминания нет (или его уже удалили).
    """
    post_id = _value(workspace_id, POST)
    if not post_id:
        return ""
    slot = _value(workspace_id, DAY)
    with _last_lock:
        last = _last.get(workspace_id)
    post = render(
        board,
        now,
        at=slot_time(slot) if slot >= 10**9 else None,
        edited=True,
        note=last.note if last is not None else "",
        tail_alone=slot >= 10**9 and _first_of_day(slot),
    )
    if post is None:
        if _delete(api, chat, post_id):
            _set(workspace_id, POST, 0)
            return "deleted"
        post = Post(body="", text=_cleared(board, now))
    if post.digest == _value(workspace_id, HASH):
        return "same"
    _edit(api, chat, post_id, post.text)
    _set(workspace_id, HASH, post.digest)
    return "edited"


def _post_reminder(
    api: Bot, chat: str, workspace_id: uuid.UUID, board: bills.Board, now: datetime, slot: int, previous: int
) -> str:
    """Новое напоминание - и прошлое прочь: в канале одно актуальное."""
    old = _value(workspace_id, POST)
    with _last_lock:
        last = _last.get(workspace_id)
    note = since_line(last.keys, last.at, board, now) if last is not None else ""
    at = slot_time(slot)
    post = render(board, now, at=at, note=note, tail_alone=_first_of_day(slot))
    result = "empty"
    if post is None:
        _set(workspace_id, POST, 0)
    else:
        try:
            message_id = _send(api, chat, post.text)
        except TelegramError:
            release(workspace_id, slot, previous)
            raise
        _set(workspace_id, POST, message_id)
        _set(workspace_id, HASH, post.digest)
        overdue, soon, _ = split(board, now)
        with _last_lock:
            _last[workspace_id] = _Last(at=at, keys=frozenset(bill.key for bill in overdue + soon), note=note)
        result = "posted"
    if old:
        _delete(api, chat, old)  # старше 48 часов не удалится - пусть висит, новое всё равно ниже
    # Названное напоминанием отдельным сообщением «новое» не повторяется.
    remember(workspace_id, board)
    return result


def tick(api: Bot, chat: str, now: datetime | None = None) -> str:
    """Один проход: напоминание, если пора; новое - сообщением; правка своих сообщений.

    Возвращает сделанное через «+» (`posted+notices-edited`), `wait` - ничего;
    `no-workspace` / `no-board` - читать нечего.
    """
    now = now or datetime.now(timezone.utc)
    workspace_id = target_workspace()
    if workspace_id is None:
        return "no-workspace"
    with finance_session() as session:
        slot = bills.get(session, workspace_id)
    board = slot.board if slot is not None else None
    if board is None:
        return "no-board"
    _ensure(workspace_id)
    done: list[str] = []
    slot = slot_of(now)
    if slot is not None:
        previous = claim(workspace_id, slot)
        if previous is not None:
            done.append(_post_reminder(api, chat, workspace_id, board, now, slot, previous))
    if not _value(workspace_id, SEEDED):
        remember(workspace_id, board)
        done.append("seeded")
    elif notify_new(api, chat, workspace_id, board, now):
        done.append("noticed")
    edited, deleted = refresh_notices(api, chat, workspace_id, board, now)
    if edited:
        done.append("notices-edited")
    if deleted:
        done.append("notices-deleted")
    if "posted" not in done and "empty" not in done:
        edited = _edit_reminder(api, chat, workspace_id, board, now)
        if edited:
            done.append(edited)
    return "+".join(done) or "wait"


def _cleared(board: bills.Board, now: datetime) -> str:
    """Всё из поста выставили за день - пост так и говорит, а не висит списком."""
    local = now.astimezone(TZ)
    return (
        f"<b>Не выставлены счета</b> · {WEEKDAYS[local.weekday()]}, {local:%d.%m.%Y}\n\n"
        "Всё из этого списка выставлено.\n\n"
        f"<i>По книге «{_esc(board.title or 'книга-сводка')}» · обновлено {local:%H:%M}</i>"
    )


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


async def run_forever() -> None:
    """Фоновая задача API: сводка дня и её правка, пока приложение живо."""
    if not finance_settings.bills_ready:
        return
    api = Bot(_clean(finance_settings.bills_bot_token))
    chat = _clean(finance_settings.bills_chat_id)
    log.info("счета: бот запущен, канал %s", chat)
    await asyncio.sleep(30)
    while True:
        try:
            with _channel_lock:
                stale = time.monotonic() - _channel.get("at", -CHANNEL_TTL) >= CHANNEL_TTL
            if stale:
                await asyncio.to_thread(_remember_channel, api, chat)
            done = await asyncio.to_thread(tick, api, chat)
            if done not in ("wait", "same"):
                log.info("счета: %s", done)
        except asyncio.CancelledError:
            raise
        except TelegramError as exc:
            log.warning("счета: Telegram не принял (%s)", exc)
        except Exception:  # noqa: BLE001 - фоновый проход не роняет приложение
            log.exception("счета: проход упал")
        await asyncio.sleep(TICK)


__all__ = [
    "LIMIT",
    "channel_out",
    "claim",
    "contracts",
    "in_window",
    "months_text",
    "notice_text",
    "notify_new",
    "refresh_notices",
    "release",
    "remember",
    "render",
    "run_forever",
    "since_line",
    "slot_of",
    "split",
    "target_workspace",
    "tick",
]
