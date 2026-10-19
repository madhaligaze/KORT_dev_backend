"""Поддержка KORT (`/finance/support/*`): переписка учётки с командой продукта.

Своё, а не данные компании: права раздела не нужны (`current_member`) - в
поддержку пишет любой, кто вошёл, включая сотрудника без единого открытого
раздела. В журнал действий компании переписка не пишется: это разговор
человека с разработчиком, а не действие в учёте, и администратору компании
её видеть незачем. След остаётся в канале поддержки.

Отправка - формой (`multipart/form-data`): текст, файлы и голосовое одним
запросом. В канал сообщение уходит после ответа - фоновой задачей, а не
ожиданием Telegram: человек видит своё сообщение сразу, а повторы при
молчании Telegram - забота прохода доставки (`app/finance/support_bot.py`).
"""
from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel

from app.api.routes.finance import _guard, current_member
from app.finance import support, support_bot
from app.finance.auth import Member
from app.finance.db import finance_session

router = APIRouter(prefix="/finance/support", tags=["finance-support"])

LIMITS = {
    "max_body": support.MAX_BODY,
    "max_files": support.MAX_FILES,
    "max_file_bytes": support.MAX_FILE_BYTES,
    "max_total_bytes": support.MAX_TOTAL_BYTES,
    "max_voice_seconds": support.MAX_VOICE_SECONDS,
}


@router.get("")
def get_thread(member: Member = Depends(current_member)) -> dict[str, Any]:
    """Переписка целиком (последние сообщения) и состояние значка."""
    _guard()
    with finance_session() as session:
        return {
            "items": support.thread(session, member.user_id),
            "state": support.state(session, member.user_id),
            "limits": LIMITS,
        }


def _read(upload: UploadFile) -> bytes:
    # Больше потолка не читаем целиком: отказ скажет `add_message`, а лишние
    # десятки мегабайт в памяти потока ни к чему.
    return upload.file.read(support.MAX_FILE_BYTES + 1)


@router.post("/messages", status_code=201)
def post_message(
    background: BackgroundTasks,
    body: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    voice: UploadFile | None = File(default=None),
    voice_duration: float = Form(0.0),
    member: Member = Depends(current_member),
) -> dict[str, Any]:
    _guard()
    # Число и общий объём - до чтения: сотня файлов по 20 МБ не должна
    # успеть лечь в память потока, прежде чем ей откажут.
    if len(files) + (voice is not None) > support.MAX_FILES:
        raise HTTPException(status_code=400, detail=f"За один раз - не больше {support.MAX_FILES} вложений")
    uploads: list[support.Upload] = []
    total = 0
    for item in [*files, *([voice] if voice is not None else [])]:
        data = _read(item)
        total += len(data)
        if total > support.MAX_TOTAL_BYTES:
            raise HTTPException(status_code=413, detail="Вложения вместе больше 40 МБ - отправьте их в двух сообщениях")
        uploads.append(
            support.Upload(
                name=item.filename or ("голосовое" if item is voice else ""),
                mime=item.content_type or "",
                data=data,
                voice=item is voice,
                duration=(voice_duration or None) if item is voice else None,
            )
        )
    with finance_session() as session:
        try:
            message = support.add_message(
                session, user_id=member.user_id, workspace_id=member.workspace_id, body=body, uploads=uploads
            )
        except support.SupportError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        out = support.message_dict(message, support.files_of(session, [message.id]).get(message.id, []))
        message_id = message.id
    background.add_task(support_bot.deliver_soon, message_id)
    return out


@router.post("/read")
def post_read(member: Member = Depends(current_member)) -> dict[str, Any]:
    """Переписка открыта - ответы поддержки прочитаны."""
    _guard()
    with finance_session() as session:
        support.mark_read(session, member.user_id)
        return support.state(session, member.user_id)


class NoticeIn(BaseModel):
    #: День по часам человека: «новый день» - его, а не сервера.
    seen_on: date | None = None
    off: bool | None = None


@router.post("/notice")
def post_notice(body: NoticeIn, member: Member = Depends(current_member)) -> dict[str, Any]:
    """Объявление о поддержке показано сегодня; «больше не показывать» - для учётки навсегда."""
    _guard()
    with finance_session() as session:
        support.set_notice(session, member.user_id, seen_on=body.seen_on, off=body.off)
        return support.state(session, member.user_id)


#: Что можно показать прямо в странице. Остальное - только скачать: файл,
#: отданный с адреса KORT как страница (HTML, SVG), выполнился бы от его имени.
_INLINE = support.IMAGE_TYPES | {"audio/ogg", "audio/webm", "audio/mp4", "audio/mpeg", "audio/aac", "audio/x-m4a"}


@router.get("/files/{file_id}")
def get_file(file_id: UUID, member: Member = Depends(current_member)) -> Response:
    _guard()
    with finance_session() as session:
        stored = support.file_of(session, member.user_id, file_id)
        if stored is None:
            raise HTTPException(status_code=404, detail="Файла нет")
        data, mime, name = stored.data, stored.mime, stored.name
    inline = mime in _INLINE
    disposition = "inline" if inline else "attachment"
    return Response(
        content=data,
        media_type=mime if inline else "application/octet-stream",
        headers={
            "Content-Disposition": f"{disposition}; filename=\"file\"; filename*=UTF-8''{quote(name)}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Cache-Control": "private, max-age=86400",
        },
    )
