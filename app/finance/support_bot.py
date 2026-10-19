"""Бот поддержки KORT: обращения - в закрытый канал Telegram, ответы - обратно в KORT.

Туда
────
Сообщение человека сначала ложится в базу (`support.add_message`), и только
потом проход доставки несёт его в канал: шапка - кто, из какой компании, когда,
текст - с кнопкой «Отправить ответ», вложения - ответами на неё: картинки
альбомом, голосовое - голосовым (WebM из Chrome перекладывается в Ogg,
`opus_ogg.py`), файлы - файлами. Не дошло (Telegram не ответил, бот не
настроен) - повтор с растущей паузой; каждый отправленный пост помечается
сразу, и повтор не шлёт его второй раз.

Обратно
───────
Сотрудник поддержки нажимает «Отправить ответ» под обращением - бот
запоминает, кому тот отвечает (`SupportReplySession`), и открывает ему личку с
собой. Всё, что сотрудник пришлёт туда - текст, голосовое, фото, файл, -
уходит в KORT ответом этому человеку, а в канал - копией под обращением:
команда видит, что ответ дан и какой. Ответ прямо в канале («Ответить» на
пост) человеку не уходит намеренно: там команда переговаривается между собой,
и заметка «это баг, передал разработчику» не должна оказаться у клиента.

События
───────
Бот читает Telegram долгим опросом (`getUpdates`), а не вебхуком: API не
нужно знать свой публичный адрес, и наружу не торчит лишний вход. Каждое
событие разбирается один раз (`SupportUpdate`): перезапуск или два экземпляра
API на время выкладки не задвоят ответ.

Токен бота не попадает ни в журнал, ни в текст ошибки: исключения `requests`
несут адрес запроса, а в нём токен, - поэтому наружу идёт только имя
исключения.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.finance import support as support_module
from app.finance.accounts_model import FinanceMembership, FinanceUser
from app.finance.config import finance_settings
from app.finance.contracts.models import Department, Employee
from app.finance.db import finance_session
from app.finance.models import Workspace
from app.finance.opus_ogg import RemuxError, webm_to_ogg
from app.finance.support_model import (
    SupportFile,
    SupportMessage,
    SupportReplySession,
    SupportThread,
    SupportUpdate,
)

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
#: Время в канале - по Казахстану: с 1 марта 2024 вся страна в UTC+5.
TZ = timezone(timedelta(hours=5))
TZ_LABEL = "UTC+5"
#: Сколько проход доставки держит сообщение за собой.
CLAIM = timedelta(minutes=5)
#: Пауза перед повтором: 30 с, 1 мин, 2 мин… не больше 30 минут.
RETRY_FIRST = 30
RETRY_MAX = 1800
#: Ответ сотрудника без дела закрывается сам: следующее сообщение в личку
#: не должно уйти человеку, которому отвечали вчера.
SESSION_TTL = timedelta(hours=12)
#: Бот Telegram скачивает файлы не больше этого.
DOWNLOAD_LIMIT = 20 * 1024 * 1024
#: Текст обращения в посте - кусками, чтобы шапка с куском уложилась в 4096 знаков Telegram.
CHUNK = 3300
ROLE_TITLES = {"owner": "владелец", "admin": "администратор", "employee": "сотрудник"}
REPLY_BUTTON = "Отправить ответ"
DONE_BUTTON = "Готово"


class TelegramError(RuntimeError):
    """Отказ или молчание Telegram. `code` - код ответа; `None` - не достучались."""

    def __init__(self, description: str, *, code: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(description)
        self.code = code
        self.retry_after = retry_after

    @property
    def refused(self) -> bool:
        """Telegram прочитал запрос и отказал по содержимому (400) - повтор не поможет."""
        return self.code == 400


def _clean(value: str) -> str:
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def _form(params: dict[str, Any] | None) -> dict[str, Any]:
    """Поля запроса: вложенное - JSON, флаги - `true`/`false`, пустое - не шлём."""
    data: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            data[key] = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            data[key] = json.dumps(value, ensure_ascii=False)
        else:
            data[key] = value
    return data


class Bot:
    """Bot API. Свой `requests.Session` на поток: опрос, доставка и фоновые задачи идут параллельно."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._local = threading.local()
        self._username: str | None = None

    def _http(self) -> requests.Session:
        http = getattr(self._local, "http", None)
        if http is None:
            http = requests.Session()
            self._local.http = http
        return http

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        timeout: float = 40,
    ) -> Any:
        try:
            response = self._http().post(
                f"{API}/bot{self._token}/{method}", data=_form(params), files=files, timeout=timeout
            )
        except requests.RequestException as exc:
            raise TelegramError(f"нет связи с Telegram ({type(exc).__name__})") from None
        try:
            payload = response.json()
        except ValueError:
            raise TelegramError(f"Telegram ответил {response.status_code}", code=response.status_code) from None
        if not payload.get("ok"):
            extra = payload.get("parameters") or {}
            raise TelegramError(
                str(payload.get("description") or "отказ Telegram"),
                code=payload.get("error_code"),
                retry_after=extra.get("retry_after"),
            )
        return payload.get("result")

    def download(self, file_id: str) -> bytes:
        info = self.call("getFile", {"file_id": file_id}) or {}
        path = info.get("file_path")
        if (info.get("file_size") or 0) > DOWNLOAD_LIMIT:
            raise TelegramError("файл больше 20 МБ", code=400)
        if not path:
            raise TelegramError("Telegram не отдал файл", code=400)
        try:
            response = self._http().get(f"{API}/file/bot{self._token}/{path}", timeout=90)
        except requests.RequestException as exc:
            raise TelegramError(f"файл не скачался ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise TelegramError(f"файл не скачался ({response.status_code})", code=response.status_code)
        return response.content

    def username(self) -> str:
        if self._username is None:
            self._username = str((self.call("getMe") or {}).get("username") or "")
        return self._username


_BOT: Bot | None = None
_BOT_LOCK = threading.Lock()


def bot() -> Bot | None:
    """Бот поддержки, если он настроен (`FINANCE_SUPPORT_*`)."""
    global _BOT
    if not finance_settings.support_ready:
        return None
    with _BOT_LOCK:
        if _BOT is None:
            _BOT = Bot(_clean(finance_settings.support_bot_token))
        return _BOT


def chat_id() -> str:
    return _clean(finance_settings.support_chat_id)


# ── Как выглядит обращение в канале ──────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return _now()
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def when(value: datetime | None) -> str:
    return _aware(value).astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def when_short(value: datetime | None) -> str:
    return _aware(value).astimezone(TZ).strftime("%d.%m %H:%M")


def phone_text(phone: str | None) -> str:
    """`+77011234567` → `+7 701 123 45 67`; иное - как есть."""
    raw = phone or ""
    if len(raw) == 12 and raw.startswith("+7") and raw[1:].isdigit():
        return f"+7 {raw[2:5]} {raw[5:8]} {raw[8:10]} {raw[10:12]}"
    return raw


def duration_text(seconds: float | None) -> str:
    total = int(round(seconds or 0))
    return f"{total // 60}:{total % 60:02d}"


def tag(user_id: uuid.UUID) -> str:
    """Метка человека в канале: по ней поиск находит всю его переписку."""
    return f"#u{user_id.hex[:8]}"


def chunks(text: str, limit: int = CHUNK) -> list[str]:
    """Текст кусками не длиннее `limit` знаков после экранирования - по строкам и словам."""
    rest = (text or "").strip()
    out: list[str] = []
    while rest:
        if len(_esc(rest)) <= limit:
            out.append(rest)
            break
        cut = limit
        while cut > 1 and len(_esc(rest[:cut])) > limit:
            cut -= max(1, cut // 20)
        piece = rest[:cut]
        for mark in ("\n\n", "\n", ". ", " "):
            at = piece.rfind(mark)
            if at > cut // 2:
                piece = piece[: at + len(mark)]
                break
        out.append(piece.strip())
        rest = rest[len(piece) :].strip()
    return out


def attachments_line(files: list[Any]) -> str:
    images = [row for row in files if row.kind == "image"]
    voices = [row for row in files if row.kind == "voice"]
    others = [row for row in files if row.kind == "file"]
    parts: list[str] = []
    if images:
        parts.append("фото" if len(images) == 1 else f"{len(images)} фото")
    for row in voices:
        parts.append(f"голосовое {duration_text(row.duration)}")
    if len(others) == 1:
        parts.append(f"файл «{_esc(others[0].name)}»")
    elif others:
        parts.append(f"файлов: {len(others)}")
    return ("Вложения: " + ", ".join(parts)) if parts else ""


@dataclass
class Who:
    name: str
    login: str
    phone: str
    company: str
    role: str
    department: str
    job: str


def who(session: Any, message: SupportMessage) -> Who:
    user = session.get(FinanceUser, message.user_id)
    company = role = department = job = ""
    if message.workspace_id is not None:
        workspace = session.get(Workspace, message.workspace_id)
        company = workspace.title if workspace is not None else ""
        role = session.scalar(
            sa.select(FinanceMembership.role).where(
                FinanceMembership.workspace_id == message.workspace_id,
                FinanceMembership.user_id == message.user_id,
            )
        ) or ""
        row = session.execute(
            sa.select(Employee.job_title, Department.code)
            .outerjoin(Department, Department.id == Employee.department_id)
            .where(Employee.workspace_id == message.workspace_id, Employee.user_id == message.user_id)
        ).first()
        if row is not None:
            job, department = row.job_title or "", row.code or ""
    email = (user.email or "") if user is not None else ""
    phone = (user.phone or "") if user is not None else ""
    name = ((user.full_name or "").strip() if user is not None else "") or email or phone or "Без имени"
    return Who(
        name=name,
        login=email,
        phone=phone,
        company=company,
        role=ROLE_TITLES.get(role, role),
        department=department,
        job=job,
    )


def compose(session: Any, message: SupportMessage, files: list[Any], *, answered: str = "") -> tuple[str, list[str]]:
    """Шапка обращения с первым куском текста - и остальные куски по порядку."""
    person = who(session, message)
    earlier = session.scalar(
        sa.select(SupportMessage)
        .where(SupportMessage.user_id == message.user_id, SupportMessage.created_at < message.created_at)
        .order_by(SupportMessage.created_at.desc())
        .limit(1)
    )
    first = not session.scalar(
        sa.select(sa.func.count())
        .select_from(SupportMessage)
        .where(
            SupportMessage.user_id == message.user_id,
            SupportMessage.author == "user",
            SupportMessage.created_at < message.created_at,
        )
    )
    lines = [f"<b>{'Новое обращение' if first else 'Продолжение переписки'}</b>", ""]
    lines.append(f"<b>{_esc(person.name)}</b>" + (f" · {_esc(person.role)}" if person.role else ""))
    place = " · ".join(part for part in (person.company, person.department, person.job) if part)
    if place:
        lines.append(_esc(place))
    contacts = " · ".join(part for part in (phone_text(person.phone), person.login) if part)
    if contacts:
        lines.append(_esc(contacts))
    lines.append(f"{when(message.created_at)} ({TZ_LABEL}) · {tag(message.user_id)}")
    if earlier is not None and earlier.author == "support":
        earlier_files = support_module.files_of(session, [earlier.id]).get(earlier.id, [])
        lines.append(f"<i>в ответ на: «{_esc(support_module.preview(earlier, earlier_files))}»</i>")
    pieces = chunks(message.body or "")
    if pieces:
        lines.extend(["", _esc(pieces[0])])
    attached = attachments_line(files)
    if attached:
        lines.extend(["", attached])
    if len(pieces) > 1:
        lines.append(f"<i>Текст длинный - продолжение ниже ({len(pieces) - 1})</i>")
    if answered:
        lines.extend(["", f"<i>{_esc(answered)}</i>"])
    return "\n".join(lines), [_esc(piece) for piece in pieces[1:]]


def reply_keyboard(message_id: uuid.UUID) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": REPLY_BUTTON, "callback_data": f"r:{message_id.hex}"}]]}


def done_keyboard() -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": DONE_BUTTON, "callback_data": "done"}]]}


def _reply_to(message_id: int | None) -> dict[str, Any] | None:
    return {"message_id": message_id, "allow_sending_without_reply": True} if message_id else None


# ── Доставка ─────────────────────────────────────────────────────────────────


def due(now: datetime | None = None, limit: int = 20) -> list[uuid.UUID]:
    now = now or _now()
    with finance_session() as session:
        return list(
            session.scalars(
                sa.select(SupportMessage.id)
                .where(
                    SupportMessage.author == "user",
                    SupportMessage.delivered_at.is_(None),
                    sa.or_(SupportMessage.next_try_at.is_(None), SupportMessage.next_try_at <= now),
                    sa.or_(SupportMessage.sending_at.is_(None), SupportMessage.sending_at < now - CLAIM),
                )
                .order_by(SupportMessage.created_at)
                .limit(limit)
            )
        )


def _claim(message_id: uuid.UUID, now: datetime) -> bool:
    with finance_session() as session:
        result = session.execute(
            sa.update(SupportMessage)
            .where(
                SupportMessage.id == message_id,
                SupportMessage.author == "user",
                SupportMessage.delivered_at.is_(None),
                sa.or_(SupportMessage.sending_at.is_(None), SupportMessage.sending_at < now - CLAIM),
            )
            .values(sending_at=now)
        )
        return (result.rowcount or 0) == 1


def _save(message_id: uuid.UUID, **values: Any) -> None:
    with finance_session() as session:
        session.execute(sa.update(SupportMessage).where(SupportMessage.id == message_id).values(**values))


def _save_file(file_id: uuid.UUID, post_id: int) -> None:
    with finance_session() as session:
        session.execute(sa.update(SupportFile).where(SupportFile.id == file_id).values(tg_message_id=post_id))


@dataclass
class _Pending:
    id: uuid.UUID
    kind: str
    name: str
    mime: str
    data: bytes
    duration: float | None


def _send_one(api: Bot, chat: str, header_id: int, item: _Pending) -> int:
    """Одно вложение ответом на шапку. Отказ формата - тем же файлом документом."""
    reply = _reply_to(header_id)
    if item.kind == "voice":
        data, name, mime, seconds = item.data, item.name, item.mime, item.duration
        if mime == "audio/webm":
            try:
                voice = webm_to_ogg(item.data)
                data, name, mime, seconds = voice.data, "голосовое.ogg", "audio/ogg", voice.duration or seconds
            except RemuxError as exc:
                log.info("поддержка: голосовое не переложено в Ogg (%s) - уйдёт файлом", exc)
                data = b""
        if data:
            try:
                sent = api.call(
                    "sendVoice",
                    {"chat_id": chat, "duration": int(round(seconds)) if seconds else None, "reply_parameters": reply},
                    files={"voice": (name, data, mime)},
                )
                return int(sent["message_id"])
            except TelegramError as exc:
                if not exc.refused:
                    raise
                log.info("поддержка: Telegram не принял голосовое (%s) - уйдёт файлом", exc)
    if item.kind == "image":
        try:
            sent = api.call(
                "sendPhoto", {"chat_id": chat, "reply_parameters": reply}, files={"photo": (item.name, item.data, item.mime)}
            )
            return int(sent["message_id"])
        except TelegramError as exc:
            if not exc.refused:
                raise
    sent = api.call(
        "sendDocument",
        {"chat_id": chat, "reply_parameters": reply},
        files={"document": (item.name, item.data, item.mime or "application/octet-stream")},
        timeout=120,
    )
    return int(sent["message_id"])


def _send_album(api: Bot, chat: str, header_id: int, items: list[_Pending]) -> list[int]:
    media = []
    files: dict[str, tuple[str, bytes, str]] = {}
    for index, item in enumerate(items):
        key = f"f{index}"
        media.append({"type": "photo", "media": f"attach://{key}"})
        files[key] = (item.name, item.data, item.mime)
    sent = api.call(
        "sendMediaGroup", {"chat_id": chat, "media": media, "reply_parameters": _reply_to(header_id)}, files=files,
        timeout=120,
    )
    return [int(post["message_id"]) for post in sent]


def _deliver(api: Bot, chat: str, message_id: uuid.UUID) -> None:
    with finance_session() as session:
        message = session.get(SupportMessage, message_id)
        if message is None or message.delivered_at is not None:
            return
        files = list(
            session.scalars(
                sa.select(SupportFile).where(SupportFile.message_id == message_id).order_by(SupportFile.position)
            )
        )
        owner = message.user_id
        thread = support_module.ensure_thread(session, owner)
        header, rest = compose(session, message, files)
        chain = thread.tg_last_post_id
        header_id = message.tg_message_id
        parts_done = message.tg_parts or 0
        pending = [
            _Pending(row.id, row.kind, row.name, row.mime, row.data, row.duration)
            for row in files
            if row.tg_message_id is None
        ]
    if header_id is None:
        sent = api.call(
            "sendMessage",
            {
                "chat_id": chat,
                "text": header,
                "parse_mode": "HTML",
                "reply_markup": reply_keyboard(message_id),
                "reply_parameters": _reply_to(chain),
                "link_preview_options": {"is_disabled": True},
            },
        )
        header_id = int(sent["message_id"])
        _save(message_id, tg_message_id=header_id)
    for index in range(parts_done, len(rest)):
        api.call(
            "sendMessage",
            {
                "chat_id": chat,
                "text": rest[index],
                "parse_mode": "HTML",
                "reply_parameters": _reply_to(header_id),
                "link_preview_options": {"is_disabled": True},
            },
        )
        _save(message_id, tg_parts=index + 1)
    images = [item for item in pending if item.kind == "image"]
    sent_files: set[uuid.UUID] = set()
    if len(images) >= 2:
        for start in range(0, len(images), 10):
            group = images[start : start + 10]
            try:
                posts = _send_album(api, chat, header_id, group)
            except TelegramError as exc:
                if not exc.refused:
                    raise
                continue  # альбом не принят - картинки уйдут по одной ниже
            for item, post in zip(group, posts):
                _save_file(item.id, post)
                sent_files.add(item.id)
    for item in pending:
        if item.id not in sent_files:
            _save_file(item.id, _send_one(api, chat, header_id, item))
    with finance_session() as session:
        session.execute(
            sa.update(SupportMessage)
            .where(SupportMessage.id == message_id)
            .values(delivered_at=_now(), sending_at=None, next_try_at=None, last_error="")
        )
        stored = session.get(SupportThread, owner)
        if stored is not None:
            stored.tg_last_post_id = header_id


def _failed(message_id: uuid.UUID, exc: TelegramError) -> None:
    now = _now()
    with finance_session() as session:
        message = session.get(SupportMessage, message_id)
        if message is None:
            return
        message.attempts = (message.attempts or 0) + 1
        pause = exc.retry_after or min(RETRY_FIRST * 2 ** (message.attempts - 1), RETRY_MAX)
        message.next_try_at = now + timedelta(seconds=float(pause))
        message.sending_at = None
        message.last_error = str(exc)[:500]
    log.warning("поддержка: сообщение %s не доставлено в канал (%s), повтор через %s с", message_id, exc, pause)


def deliver(api: Bot, chat: str, message_id: uuid.UUID) -> bool:
    """Доставить сообщение в канал - если его не взял другой проход."""
    if not _claim(message_id, _now()):
        return False
    try:
        _deliver(api, chat, message_id)
        return True
    except TelegramError as exc:
        _failed(message_id, exc)
    except Exception as exc:  # noqa: BLE001 - доставка не роняет приложение, повтор - позже
        log.exception("поддержка: сбой доставки %s", message_id)
        _failed(message_id, TelegramError(type(exc).__name__))
    return False


def deliver_due(api: Bot, chat: str) -> int:
    return sum(1 for message_id in due() if deliver(api, chat, message_id))


def deliver_soon(message_id: uuid.UUID) -> None:
    """Сразу после записи сообщения - не ждать прохода доставки (он раз в 5 с)."""
    api = bot()
    if api is None:
        return
    try:
        deliver(api, chat_id(), message_id)
    except Exception:  # noqa: BLE001
        log.exception("поддержка: доставка %s не запустилась", message_id)


# ── Ответ поддержки из лички бота ────────────────────────────────────────────


def tg_name(person: dict[str, Any]) -> str:
    name = " ".join(part for part in (person.get("first_name"), person.get("last_name")) if part).strip()
    return name or (f"@{person['username']}" if person.get("username") else "Поддержка")


def _say(api: Bot, chat: int | str, text: str, **extra: Any) -> None:
    try:
        api.call("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML", **extra})
    except TelegramError as exc:
        log.info("поддержка: сообщение в Telegram не ушло (%s)", exc)


INTRO = (
    "Это бот поддержки KORT. Написать в поддержку можно в самом KORT - значок поддержки в шапке.\n\n"
    "Сотрудникам поддержки: ответ - кнопкой «Отправить ответ» под обращением в канале."
)
NO_TARGET = (
    "Сейчас вы никому не отвечаете. Нажмите «Отправить ответ» под обращением в канале поддержки - "
    "и пишите сюда."
)


def post_link(chat: str, post_id: int) -> str:
    """Ссылка на пост закрытого канала: `-1004474041636` → `t.me/c/4474041636/<пост>`."""
    short = chat[4:] if chat.startswith("-100") else chat.lstrip("-")
    return f"https://t.me/c/{short}/{post_id}"


def _waiting(session: Any) -> SupportMessage | None:
    """Самое свежее обращение в канале, на которое ещё нет ответа."""
    recent = session.scalars(
        sa.select(SupportMessage)
        .where(SupportMessage.author == "user", SupportMessage.tg_message_id.is_not(None))
        .order_by(SupportMessage.created_at.desc())
        .limit(20)
    )
    for message in recent:
        answered = session.scalar(
            sa.select(sa.func.count())
            .select_from(SupportMessage)
            .where(
                SupportMessage.user_id == message.user_id,
                SupportMessage.author == "support",
                SupportMessage.created_at > message.created_at,
            )
        )
        if not answered:
            return message
    return None


def _no_target(api: Bot, chat: str, tg_user: int) -> None:
    """Написали боту, не нажав «Отправить ответ»: куда идти - словами и ссылкой.

    Адресата бот не угадывает (16.10.2026: первое, что сделал владелец, - написал
    боту напрямую, и ответ никуда не ушёл). Ссылка ведёт к последнему
    обращению без ответа, а отвечать по-прежнему - кнопкой под ним.
    """
    with finance_session() as session:
        waiting = _waiting(session)
        line = ""
        link = ""
        if waiting is not None and waiting.tg_message_id is not None:
            line = f"\n\nБез ответа: <b>{_esc(who(session, waiting).name)}</b>, {when_short(waiting.created_at)}."
            link = post_link(chat, waiting.tg_message_id)
    extra: dict[str, Any] = {}
    if link:
        extra["reply_markup"] = {"inline_keyboard": [[{"text": "Открыть обращение", "url": link}]]}
    _say(api, tg_user, NO_TARGET + line, **extra)


def _prompt(session: Any, message: SupportMessage, *, short: bool = False) -> str:
    person = who(session, message)
    head = f"Ответ для: <b>{_esc(person.name)}</b>"
    if short:
        return f"{head}\nПишите - всё, что пришлёте, уйдёт в KORT. Закончили - «{DONE_BUTTON}»."
    files = support_module.files_of(session, [message.id]).get(message.id, [])
    place = " · ".join(part for part in (person.company, f"сообщение от {when_short(message.created_at)}") if part)
    quote = support_module.preview(message, files)
    return (
        f"{head}\n{_esc(place)}\n\n«{_esc(quote)}»\n\n"
        "Напишите ответ сюда - текстом, голосовым, фото или файлом. Всё, что пришлёте, уйдёт в KORT "
        f"этому человеку, пока не нажмёте «{DONE_BUTTON}»."
    )


def _seen(update_id: int) -> bool:
    with finance_session() as session:
        return session.get(SupportUpdate, update_id) is not None


def _mark_seen(update_id: int) -> None:
    try:
        with finance_session() as session:
            if session.get(SupportUpdate, update_id) is None:
                session.add(SupportUpdate(update_id=update_id))
    except IntegrityError:
        pass


def _on_callback(api: Bot, chat: str, query: dict[str, Any]) -> None:
    data = str(query.get("data") or "")
    person = query.get("from") or {}
    tg_user = int(person.get("id") or 0)
    post = query.get("message") or {}

    def answer(text: str = "", url: str = "", alert: bool = False) -> bool:
        try:
            api.call(
                "answerCallbackQuery",
                {
                    "callback_query_id": query.get("id"),
                    "text": text or None,
                    "url": url or None,
                    "show_alert": True if alert else None,
                },
            )
            return True
        except TelegramError as exc:
            log.info("поддержка: ответ на кнопку не ушёл (%s)", exc)
            return False

    if data == "done":
        with finance_session() as session:
            row = session.get(SupportReplySession, tg_user)
            if row is not None:
                session.delete(row)
        answer("Ответ закрыт")
        _say(api, tg_user, "Ответ закрыт. Следующий - кнопкой «Отправить ответ» под обращением в канале.")
        return
    if not data.startswith("r:"):
        answer()
        return
    # Кнопка ответа работает только под постом закрытого канала: её видят его участники.
    if str((post.get("chat") or {}).get("id")) != str(chat):
        answer("Эта кнопка работает только в канале поддержки")
        return
    try:
        target = uuid.UUID(hex=data[2:])
    except ValueError:
        answer("Обращение не найдено")
        return
    with finance_session() as session:
        message = session.get(SupportMessage, target)
        if message is None:
            prompt = ""
        else:
            row = session.get(SupportReplySession, tg_user)
            if row is None:
                row = SupportReplySession(tg_user_id=tg_user)
                session.add(row)
            now = _now()
            row.message_id = target
            row.tg_name = tg_name(person)
            row.started_at = now
            row.touched_at = now
            prompt = _prompt(session, message)
    if not prompt:
        answer("Обращение не найдено - учётку могли удалить")
        return
    # Бот не может написать первым тому, кто его ещё не запускал, - тогда
    # подсказка придёт по «Start» в личке, куда ведёт ссылка ниже.
    _say(api, tg_user, prompt, reply_markup=done_keyboard())
    username = ""
    try:
        username = api.username()
    except TelegramError as exc:
        log.info("поддержка: имя бота не получено (%s)", exc)
    if username and answer(url=f"https://t.me/{username}?start=reply"):
        return
    # Ссылку Telegram не принял - сказать словами, куда идти, а не оставить кнопку крутиться.
    where = f"@{username}" if username else "бота поддержки"
    answer(f"Откройте {where} и нажмите «Start» - ответ пишется там, в личке бота.", alert=True)


@dataclass
class _Incoming:
    kind: str
    file_id: str
    name: str
    mime: str
    size: int
    duration: float | None


def _incoming(message: dict[str, Any]) -> _Incoming | None | str:
    """Вложение из лички: картинка, голосовое, файл. Строка - отказ словами."""
    if message.get("photo"):
        sizes = [size for size in message["photo"] if (size.get("file_size") or 0) <= DOWNLOAD_LIMIT]
        if not sizes:
            return "Фото больше 20 МБ - бот Telegram такое не скачает. Пришлите поменьше."
        best = sizes[-1]
        return _Incoming("image", best["file_id"], "фото.jpg", "image/jpeg", best.get("file_size") or 0, None)
    if message.get("voice"):
        voice = message["voice"]
        return _Incoming(
            "voice", voice["file_id"], "голосовое.ogg", voice.get("mime_type") or "audio/ogg",
            voice.get("file_size") or 0, float(voice.get("duration") or 0) or None,
        )
    for key, fallback, mime in (
        ("document", "файл", "application/octet-stream"),
        ("audio", "аудио.mp3", "audio/mpeg"),
        ("video", "видео.mp4", "video/mp4"),
        ("video_note", "видео.mp4", "video/mp4"),
        ("animation", "анимация.mp4", "video/mp4"),
    ):
        item = message.get(key)
        if item:
            return _Incoming(
                "file", item["file_id"], item.get("file_name") or fallback, item.get("mime_type") or mime,
                item.get("file_size") or 0, float(item.get("duration") or 0) or None,
            )
    if any(message.get(key) for key in ("sticker", "location", "contact", "poll", "venue", "dice")):
        return "Такое в KORT не уйдёт - пришлите текстом, голосовым, фото или файлом."
    return None


def _log_reply(api: Bot, chat: str, message: dict[str, Any], header_id: int | None, name: str) -> None:
    """Копия ответа в канал - под обращением: команда видит, что и кто ответил."""
    title = f"<b>Ответ поддержки</b> · {_esc(name)}"
    try:
        if message.get("text"):
            text = f"{title}\n\n{_esc(message['text'])}"
            if len(text) > 4000:
                text = text[:3990] + "…"
            api.call(
                "sendMessage",
                {
                    "chat_id": chat,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_parameters": _reply_to(header_id),
                    "link_preview_options": {"is_disabled": True},
                },
            )
        else:
            caption = title + (f"\n\n{_esc(message['caption'])}" if message.get("caption") else "")
            api.call(
                "copyMessage",
                {
                    "chat_id": chat,
                    "from_chat_id": (message.get("chat") or {}).get("id"),
                    "message_id": message.get("message_id"),
                    "caption": caption[:1020],
                    "parse_mode": "HTML",
                    "reply_parameters": _reply_to(header_id),
                },
            )
    except TelegramError as exc:
        log.info("поддержка: копия ответа в канал не ушла (%s)", exc)


def _mark_answered(api: Bot, chat: str, message_id: uuid.UUID, name: str, at: datetime) -> None:
    """Шапка обращения в канале - с пометкой «Отвечено»: видно, что ещё ждёт ответа."""
    with finance_session() as session:
        message = session.get(SupportMessage, message_id)
        if message is None or message.tg_message_id is None:
            return
        files = list(
            session.scalars(sa.select(SupportFile).where(SupportFile.message_id == message_id).order_by(SupportFile.position))
        )
        header, _ = compose(session, message, files, answered=f"Отвечено · {name} · {when_short(at)}")
        post_id = message.tg_message_id
    try:
        api.call(
            "editMessageText",
            {
                "chat_id": chat,
                "message_id": post_id,
                "text": header,
                "parse_mode": "HTML",
                "reply_markup": reply_keyboard(message_id),
                "link_preview_options": {"is_disabled": True},
            },
        )
    except TelegramError as exc:
        log.info("поддержка: пометка «Отвечено» не встала (%s)", exc)


def _on_private(api: Bot, chat: str, message: dict[str, Any], update_id: int) -> None:
    where = message.get("chat") or {}
    person = message.get("from") or {}
    if where.get("type") != "private" or person.get("is_bot"):
        _mark_seen(update_id)
        return
    tg_user = int(person.get("id") or 0)
    text = str(message.get("text") or "")
    now = _now()
    target_id: uuid.UUID | None = None
    target_user: uuid.UUID | None = None
    target_workspace: uuid.UUID | None = None
    header_id: int | None = None
    with finance_session() as session:
        row = session.get(SupportReplySession, tg_user)
        live = row is not None and _aware(row.touched_at) >= now - SESSION_TTL
        target = session.get(SupportMessage, row.message_id) if live and row is not None else None
        short = _prompt(session, target, short=True) if target is not None else ""
        name = row.tg_name if row is not None and row.tg_name else tg_name(person)
        if target is not None:
            target_id, target_user, target_workspace, header_id = (
                target.id, target.user_id, target.workspace_id, target.tg_message_id,
            )
    if text.startswith("/start"):
        _mark_seen(update_id)
        if target is not None:
            _say(api, tg_user, short, reply_markup=done_keyboard())
        else:
            _say(api, tg_user, INTRO)
        return
    if text.split("@", 1)[0] in ("/done", "/stop", "/cancel"):
        _mark_seen(update_id)
        with finance_session() as session:
            stale = session.get(SupportReplySession, tg_user)
            if stale is not None:
                session.delete(stale)
        _say(api, tg_user, "Ответ закрыт. Следующий - кнопкой «Отправить ответ» под обращением в канале.")
        return
    if target is None:
        _mark_seen(update_id)
        _no_target(api, chat, tg_user)
        return
    incoming = _incoming(message)
    if isinstance(incoming, str):
        _mark_seen(update_id)
        _say(api, tg_user, incoming)
        return
    body = str(message.get("text") or message.get("caption") or "").strip()
    if incoming is None and not body:
        _mark_seen(update_id)
        return
    data = b""
    if incoming is not None:
        if incoming.size > DOWNLOAD_LIMIT:
            _mark_seen(update_id)
            _say(api, tg_user, "Файл больше 20 МБ - бот Telegram такой не скачает. Пришлите поменьше или ссылкой.")
            return
        try:
            data = api.download(incoming.file_id)
        except TelegramError as exc:
            if not exc.refused:
                raise  # сеть - событие разберём снова со следующим опросом
            _mark_seen(update_id)
            _say(api, tg_user, f"Файл не скачался из Telegram ({_esc(str(exc))}) - пришлите ещё раз.")
            return
    group = str(message.get("media_group_id") or "")
    try:
        with finance_session() as session:
            session.add(SupportUpdate(update_id=update_id))
            session.flush()
            reply = None
            if group:
                reply = session.scalar(
                    sa.select(SupportMessage).where(
                        SupportMessage.author == "support",
                        SupportMessage.user_id == target_user,
                        SupportMessage.tg_media_group == group,
                    )
                )
            joined = reply is not None
            if reply is None:
                reply = SupportMessage(
                    user_id=target_user,
                    workspace_id=target_workspace,
                    author="support",
                    body=body,
                    created_at=now,
                    reply_to_id=target_id,
                    support_name=name,
                    tg_media_group=group,
                    attempts=0,
                )
                session.add(reply)
                session.flush()
            elif body and not reply.body:
                reply.body = body
            if incoming is not None:
                upload = support_module.Upload(
                    name=incoming.name, mime=incoming.mime, data=data, voice=incoming.kind == "voice",
                    duration=incoming.duration,
                )
                count = session.scalar(
                    sa.select(sa.func.count()).select_from(SupportFile).where(SupportFile.message_id == reply.id)
                ) or 0
                session.add(
                    SupportFile(
                        message_id=reply.id,
                        kind=support_module.kind_of(upload),
                        name=incoming.name,
                        mime=incoming.mime.split(";", 1)[0].strip().lower(),
                        size=len(data),
                        data=data,
                        duration=incoming.duration,
                        position=int(count),
                    )
                )
            stored = session.get(SupportReplySession, tg_user)
            if stored is not None:
                stored.touched_at = now
    except IntegrityError:
        log.info("поддержка: событие %s уже разобрано", update_id)
        return
    _log_reply(api, chat, message, header_id, name)
    if joined:
        return  # часть альбома: подтверждение и пометка - на первой картинке
    _say(
        api,
        tg_user,
        f"Доставлено в KORT. Можно дописать ещё - уйдёт туда же. Закончили - «{DONE_BUTTON}».",
        reply_markup=done_keyboard(),
        reply_parameters=_reply_to(message.get("message_id")),
    )
    _mark_answered(api, chat, target_id, name, now)


def handle(api: Bot, chat: str, update: dict[str, Any]) -> None:
    """Одно событие Telegram - один раз."""
    update_id = int(update.get("update_id") or 0)
    if _seen(update_id):
        return
    if update.get("callback_query"):
        _on_callback(api, chat, update["callback_query"])
        _mark_seen(update_id)
    elif update.get("message"):
        _on_private(api, chat, update["message"], update_id)
    else:
        _mark_seen(update_id)


def cleanup(now: datetime | None = None) -> None:
    """Разобранные события старше двух недель и забытые ответы - прочь."""
    now = now or _now()
    with finance_session() as session:
        session.execute(sa.delete(SupportUpdate).where(SupportUpdate.seen_at < now - timedelta(days=14)))
        session.execute(sa.delete(SupportReplySession).where(SupportReplySession.touched_at < now - timedelta(days=2)))


# ── Фоновый цикл ─────────────────────────────────────────────────────────────


async def _poll(api: Bot, chat: str) -> None:
    offset: int | None = None
    pause = 1.0
    while True:
        try:
            updates = await asyncio.to_thread(
                api.call,
                "getUpdates",
                {"offset": offset, "timeout": 50, "allowed_updates": ["message", "callback_query"]},
                timeout=65,
            )
            pause = 1.0
        except TelegramError as exc:
            # 409 - события читает другой экземпляр (выкладка): подождать и снова.
            log.warning("поддержка: Telegram не отдал события (%s)", exc)
            await asyncio.sleep(pause)
            pause = min(pause * 2, 60.0)
            continue
        for update in updates or []:
            update_id = int(update.get("update_id") or 0)
            try:
                await asyncio.to_thread(handle, api, chat, update)
            except TelegramError as exc:
                if exc.code is None:
                    # Сеть упала посреди разбора - не подтверждаем, событие придёт снова.
                    log.warning("поддержка: событие %s отложено (%s)", update_id, exc)
                    break
                log.warning("поддержка: событие %s - отказ Telegram (%s)", update_id, exc)
            except Exception:  # noqa: BLE001
                log.exception("поддержка: событие %s не разобрано", update_id)
            offset = update_id + 1


async def _deliver_loop(api: Bot, chat: str) -> None:
    cleaned = 0.0
    while True:
        try:
            await asyncio.to_thread(deliver_due, api, chat)
            if time.monotonic() - cleaned > 3600:
                await asyncio.to_thread(cleanup)
                cleaned = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - фоновый проход не роняет приложение
            log.exception("поддержка: проход доставки упал")
        await asyncio.sleep(5)


async def run_forever() -> None:
    """Фоновая задача API: доставка обращений и приём ответов, пока приложение живо."""
    api = bot()
    if api is None:
        return
    chat = chat_id()
    # getUpdates не работает, пока у бота выставлен вебхук.
    with contextlib.suppress(TelegramError):
        await asyncio.to_thread(api.call, "deleteWebhook", {"drop_pending_updates": False})
    log.info("поддержка: бот запущен, канал %s", chat)
    delivering = asyncio.create_task(_deliver_loop(api, chat))
    try:
        await _poll(api, chat)
    finally:
        delivering.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await delivering


__all__ = [
    "Bot",
    "TelegramError",
    "bot",
    "chat_id",
    "chunks",
    "compose",
    "deliver",
    "deliver_due",
    "deliver_soon",
    "handle",
    "run_forever",
]
