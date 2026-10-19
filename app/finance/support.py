"""Поддержка KORT: переписка учётки с командой продукта - правила и чтение.

Что здесь решается
──────────────────
* **что можно отправить**: текст до 8000 знаков, до 10 вложений, файл до
  20 МБ, всё вместе до 40 МБ, голосовое до 10 минут; пустое - отказ. 20 МБ -
  потолок, который бот Telegram умеет скачать: ответ поддержки с файлом
  больше не дошёл бы обратно, и правило одно в обе стороны;
* **как часто**: не больше 20 сообщений за 10 минут - иначе случайный повтор
  (зажатый Enter, повисшая сеть) завалил бы канал поддержки;
* **что человек видит**: свою переписку - и только свою; файл отдаётся только
  хозяину переписки;
* **что непрочитано**: ответ поддержки, который человек ещё не открыл. Число
  едет в `me` - значок поддержки в шапке узнаёт о нём с опросом раз в 20 с.

Доставка в Telegram и разбор ответов - `support_bot.py`.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.support_model import SupportFile, SupportMessage, SupportThread

MAX_BODY = 8000
MAX_FILES = 10
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 40 * 1024 * 1024
MAX_VOICE_SECONDS = 600
#: Сколько сообщений человек отправит за `RATE_WINDOW` - дальше просим подождать.
RATE_LIMIT = 20
RATE_WINDOW = timedelta(minutes=10)
#: Сколько последних сообщений отдаёт переписка: дальше история не нужна на экране.
THREAD_LIMIT = 300
#: Через сколько недоставленное сообщение человек видит «ещё не дошло».
LATE = timedelta(minutes=2)

#: Картинки, которые переписка показывает. SVG - нет: он может нести скрипт,
#: и отданный с нашего адреса выполнился бы от имени KORT. SVG уходит файлом.
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
#: Голосовое - то, что пишет браузер: WebM (Chrome, Edge), Ogg (Firefox), MP4 (Safari).
VOICE_TYPES = {"audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/aac", "audio/x-m4a"}


class SupportError(ValueError):
    """Отказ словами: что не так с сообщением."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Upload:
    """Вложение, как оно пришло из формы."""

    name: str
    mime: str
    data: bytes
    voice: bool = False
    #: Длительность голосового по часам браузера, секунды.
    duration: float | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _base_type(mime: str) -> str:
    return (mime or "").split(";", 1)[0].strip().lower()


def _looks_like_image(data: bytes) -> bool:
    """Подпись файла, а не слово браузера: тип из формы присылает кто угодно."""
    return (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        or data.startswith(b"\xff\xd8\xff")
        or data[:6] in (b"GIF87a", b"GIF89a")
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    )


def _plural(count: int, one: str, few: str, many: str) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return many
    last = count % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _clean_name(name: str, fallback: str) -> str:
    """Имя файла без пути и управляющих символов - его покажут в канале и в переписке."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch >= " " and ch != "\x7f").strip()
    return base[:180] or fallback


def kind_of(upload: Upload) -> str:
    if upload.voice:
        return "voice"
    if _base_type(upload.mime) in IMAGE_TYPES and _looks_like_image(upload.data):
        return "image"
    return "file"


def ensure_thread(session: Session, user_id: uuid.UUID) -> SupportThread:
    thread = session.get(SupportThread, user_id)
    if thread is None:
        thread = SupportThread(user_id=user_id, notice_off=False)
        session.add(thread)
        session.flush()
    return thread


def add_message(
    session: Session,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    body: str,
    uploads: list[Upload],
    now: datetime | None = None,
) -> SupportMessage:
    """Сообщение человека в поддержку. Отказ - `SupportError` с текстом."""
    now = now or _now()
    text = (body or "").replace("\r\n", "\n").strip()
    if len(text) > MAX_BODY:
        raise SupportError(f"Сообщение длиннее {MAX_BODY} знаков - разбейте его на два или приложите файлом")
    if not text and not uploads:
        raise SupportError("Напишите, что случилось, или приложите файл или голосовое")
    if len(uploads) > MAX_FILES:
        raise SupportError(f"За один раз - не больше {MAX_FILES} вложений")
    total = 0
    for upload in uploads:
        if not upload.data:
            raise SupportError(f"Файл «{_clean_name(upload.name, 'без имени')}» пустой")
        if len(upload.data) > MAX_FILE_BYTES:
            raise SupportError(
                f"Файл «{_clean_name(upload.name, 'без имени')}» больше 20 МБ - сожмите его или пришлите ссылкой",
                status=413,
            )
        total += len(upload.data)
        if upload.voice:
            if _base_type(upload.mime) not in VOICE_TYPES:
                raise SupportError("Голосовое записалось в формате, которого мы не знаем - запишите ещё раз")
            if upload.duration is not None and upload.duration > MAX_VOICE_SECONDS + 5:
                raise SupportError("Голосовое длиннее 10 минут - разбейте рассказ на два")
    if total > MAX_TOTAL_BYTES:
        raise SupportError("Вложения вместе больше 40 МБ - отправьте их в двух сообщениях", status=413)
    recent = session.scalar(
        sa.select(sa.func.count())
        .select_from(SupportMessage)
        .where(
            SupportMessage.user_id == user_id,
            SupportMessage.author == "user",
            SupportMessage.created_at >= now - RATE_WINDOW,
        )
    )
    if (recent or 0) >= RATE_LIMIT:
        raise SupportError("Слишком много сообщений подряд - подождите несколько минут", status=429)

    ensure_thread(session, user_id)
    message = SupportMessage(
        user_id=user_id,
        workspace_id=workspace_id,
        author="user",
        body=text,
        created_at=now,
        attempts=0,
    )
    session.add(message)
    session.flush()
    for position, upload in enumerate(uploads):
        kind = kind_of(upload)
        mime = _base_type(upload.mime) or "application/octet-stream"
        fallback = "голосовое" if kind == "voice" else ("картинка" if kind == "image" else "файл")
        session.add(
            SupportFile(
                message_id=message.id,
                kind=kind,
                name=_clean_name(upload.name, fallback),
                mime=mime,
                size=len(upload.data),
                data=upload.data,
                duration=round(float(upload.duration), 2) if upload.duration else None,
                position=position,
            )
        )
    session.flush()
    return message


def files_of(session: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, list[SupportFile]]:
    out: dict[uuid.UUID, list[SupportFile]] = {}
    if not ids:
        return out
    # Без байтов: список переписки их не отдаёт, файл читается своим запросом.
    rows = session.execute(
        sa.select(
            SupportFile.id,
            SupportFile.message_id,
            SupportFile.kind,
            SupportFile.name,
            SupportFile.mime,
            SupportFile.size,
            SupportFile.duration,
            SupportFile.position,
        )
        .where(SupportFile.message_id.in_(ids))
        .order_by(SupportFile.position)
    ).all()
    for row in rows:
        out.setdefault(row.message_id, []).append(row)  # type: ignore[arg-type]
    return out


def _file_dict(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "name": row.name,
        "mime": row.mime,
        "size": row.size,
        "duration": row.duration,
    }


def message_dict(message: SupportMessage, files: list[Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    created = _aware(message.created_at) or now
    out: dict[str, Any] = {
        "id": str(message.id),
        "author": message.author,
        "body": message.body or "",
        "created_at": created.isoformat(),
        "files": [_file_dict(row) for row in files],
    }
    if message.author == "support":
        out["read"] = message.read_at is not None
    else:
        delivered = message.delivered_at is not None
        out["delivered"] = delivered
        # «Ещё не дошло» - только когда это уже заметно: обычно канал
        # получает сообщение за секунды, и строка мигала бы на каждом.
        out["late"] = not delivered and now - created > LATE
    return out


def thread(session: Session, user_id: uuid.UUID, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Переписка человека - старое сверху, последние `THREAD_LIMIT` сообщений."""
    now = now or _now()
    rows = list(
        session.scalars(
            sa.select(SupportMessage)
            .where(SupportMessage.user_id == user_id)
            .order_by(SupportMessage.created_at.desc(), SupportMessage.id.desc())
            .limit(THREAD_LIMIT)
        )
    )
    rows.reverse()
    files = files_of(session, [row.id for row in rows])
    return [message_dict(row, files.get(row.id, []), now) for row in rows]


def preview(message: SupportMessage, files: list[Any]) -> str:
    """Строка ответа для напоминания: начало текста, а без текста - что приложено."""
    text = " ".join((message.body or "").split())
    if text:
        return text if len(text) <= 160 else text[:157].rstrip() + "…"
    kinds = [row.kind for row in files]
    if "voice" in kinds:
        return "Голосовое сообщение"
    if kinds and all(kind == "image" for kind in kinds):
        return "Фото" if len(kinds) == 1 else f"{len(kinds)} фото"
    if kinds:
        return "Файл" if len(kinds) == 1 else f"{len(kinds)} {_plural(len(kinds), 'файл', 'файла', 'файлов')}"
    return "Сообщение"


def state(session: Session, user_id: uuid.UUID) -> dict[str, Any]:
    """Поддержка в `me`: непрочитанные ответы и объявление дня.

    Два запроса по индексу: опрос `me` раз в 20 с у каждого человека.
    """
    stored = session.get(SupportThread, user_id)
    unread_only = (
        SupportMessage.user_id == user_id,
        SupportMessage.author == "support",
        SupportMessage.read_at.is_(None),
    )
    unread = session.scalar(sa.select(sa.func.count()).select_from(SupportMessage).where(*unread_only)) or 0
    last = None
    if unread:
        newest = session.scalar(
            sa.select(SupportMessage).where(*unread_only).order_by(SupportMessage.created_at.desc()).limit(1)
        )
        if newest is not None:
            files = files_of(session, [newest.id]).get(newest.id, [])
            last = {
                "id": str(newest.id),
                "preview": preview(newest, files),
                "at": (_aware(newest.created_at) or _now()).isoformat(),
            }
    return {
        "unread": int(unread),
        "last_reply": last,
        "notice_off": bool(stored.notice_off) if stored is not None else False,
        "notice_seen_on": stored.notice_seen_on.isoformat() if stored and stored.notice_seen_on else None,
    }


def mark_read(session: Session, user_id: uuid.UUID, *, now: datetime | None = None) -> int:
    """Человек открыл переписку - ответы поддержки прочитаны."""
    result = session.execute(
        sa.update(SupportMessage)
        .where(
            SupportMessage.user_id == user_id,
            SupportMessage.author == "support",
            SupportMessage.read_at.is_(None),
        )
        .values(read_at=now or _now())
    )
    return int(result.rowcount or 0)


def set_notice(
    session: Session, user_id: uuid.UUID, *, seen_on: date | None = None, off: bool | None = None
) -> SupportThread:
    """Объявление о поддержке показано сегодня; «больше не показывать» - навсегда."""
    stored = ensure_thread(session, user_id)
    if seen_on is not None:
        stored.notice_seen_on = seen_on
    if off is not None:
        stored.notice_off = off
    return stored


def file_of(session: Session, user_id: uuid.UUID, file_id: uuid.UUID) -> SupportFile | None:
    """Файл переписки - только её хозяину."""
    return session.scalar(
        sa.select(SupportFile)
        .join(SupportMessage, SupportMessage.id == SupportFile.message_id)
        .where(SupportFile.id == file_id, SupportMessage.user_id == user_id)
    )


__all__ = [
    "IMAGE_TYPES",
    "MAX_BODY",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_BYTES",
    "MAX_VOICE_SECONDS",
    "SupportError",
    "Upload",
    "add_message",
    "ensure_thread",
    "file_of",
    "files_of",
    "kind_of",
    "mark_read",
    "message_dict",
    "preview",
    "set_notice",
    "state",
    "thread",
]
