"""Поддержка KORT - модель: переписка учётки с командой продукта.

Переписка - одна на человека (учётку), а не на компанию: человек пишет о
продукте, и ответ должен найти его, в какой бы из своих компаний он ни был.
Компания, из которой написано, стоит у каждого сообщения (`workspace_id`):
поддержке нужно знать, в чьих данных смотреть.

Сообщения хранятся здесь целиком, с файлами. В канал поддержки (Telegram) они
уходят отдельным проходом и с повторами (`support_bot.py`): обращение не
теряется, если Telegram не ответил или бот ещё не настроен, а история
переписки в KORT не зависит от Telegram.

Ответ поддержки приходит из Telegram: сотрудник нажимает «Отправить ответ»
под обращением в канале и пишет боту в личку (`SupportReplySession` -
кому он сейчас отвечает). Каждое событие Telegram разбирается один раз
(`SupportUpdate`): после перезапуска или при двух экземплярах API на время
выкладки ответ не задвоится.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.finance.db import FinanceBase
from app.finance.models import _in

#: Кто написал: человек из KORT или поддержка из Telegram.
SUPPORT_AUTHORS = ("user", "support")
#: Вложение: картинка (показывается в переписке), голосовое, любой файл.
SUPPORT_FILE_KINDS = ("image", "voice", "file")


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class SupportThread(FinanceBase):
    """Переписка человека с поддержкой: одна на учётку."""

    __tablename__ = "support_threads"

    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    #: «Больше не показывать» под объявлением о поддержке - навсегда, для учётки.
    notice_off: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    #: День (по часам человека), когда объявление уже показано: раз в день.
    notice_seen_on: Mapped[date | None] = mapped_column(sa.Date)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())
    #: Последний пост переписки в канале: новое сообщение встаёт ответом на
    #: него, и переписка в канале читается цепочкой.
    tg_last_post_id: Mapped[int | None] = mapped_column(sa.BigInteger)


class SupportMessage(FinanceBase):
    """Сообщение переписки - человека или поддержки."""

    __tablename__ = "support_messages"
    __table_args__ = (
        sa.CheckConstraint(_in("author", SUPPORT_AUTHORS), name="support_message_author"),
        sa.Index("ix_support_messages_user_created", "user_id", "created_at"),
        # Непрочитанные ответы - число у значка поддержки, его спрашивает каждый опрос `me`.
        sa.Index("ix_support_messages_user_unread", "user_id", "author", "read_at"),
        # Недоставленные в канал - их берёт проход доставки.
        sa.Index("ix_support_messages_undelivered", "author", "delivered_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    #: Чья переписка.
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE"))
    #: Из какой компании написано. Компанию удалили - сообщение остаётся.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="SET NULL")
    )
    author: Mapped[str] = mapped_column(sa.Text)
    body: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())
    #: Ответ поддержки увиден человеком. Пусто - непрочитанный.
    read_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    # ── Доставка сообщения человека в канал ──
    delivered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    #: Проход доставки взял сообщение. Держится несколько минут: второй
    #: экземпляр API на время выкладки не отправит его второй раз.
    sending_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    #: Не раньше этого - следующая попытка: Telegram не ответил, повтор с паузой.
    next_try_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    #: Пост с шапкой обращения в канале - под ним кнопка «Отправить ответ».
    tg_message_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    #: Сколько продолжений длинного текста уже в канале (шапка вмещает ~3300 знаков).
    tg_parts: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))

    # ── Ответ поддержки ──
    #: На какое сообщение человека ответили.
    reply_to_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("support_messages.id", ondelete="SET NULL")
    )
    #: Кто ответил - имя в Telegram, для канала и журнала; человеку - «Поддержка KORT».
    support_name: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    #: Альбом Telegram приходит событием на каждую картинку - собираем в одно сообщение.
    tg_media_group: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))


class SupportFile(FinanceBase):
    """Вложение сообщения: картинка, голосовое или файл - байты в базе."""

    __tablename__ = "support_files"
    __table_args__ = (sa.CheckConstraint(_in("kind", SUPPORT_FILE_KINDS), name="support_file_kind"),)

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    message_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("support_messages.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(sa.Text)
    name: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    mime: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    size: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    data: Mapped[bytes] = mapped_column(sa.LargeBinary)
    #: Длительность голосового, секунды.
    duration: Mapped[float | None] = mapped_column(sa.Float)
    position: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    #: Пост этого вложения в канале: при повторе доставки уже отправленное не шлётся заново.
    tg_message_id: Mapped[int | None] = mapped_column(sa.BigInteger)


class SupportReplySession(FinanceBase):
    """Кому сотрудник поддержки сейчас отвечает в личке бота.

    Заводится нажатием «Отправить ответ» под обращением в канале - кнопку
    видят только участники закрытого канала, поэтому чужой человек, нашедший
    бота, ответить за поддержку не может.
    """

    __tablename__ = "support_reply_sessions"

    tg_user_id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=False)
    message_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("support_messages.id", ondelete="CASCADE")
    )
    tg_name: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())
    touched_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())


class SupportUpdate(FinanceBase):
    """Событие Telegram, уже разобранное, - чтобы не разобрать его дважды."""

    __tablename__ = "support_updates"

    update_id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=False)
    seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())


__all__ = [
    "SUPPORT_AUTHORS",
    "SUPPORT_FILE_KINDS",
    "SupportFile",
    "SupportMessage",
    "SupportReplySession",
    "SupportThread",
    "SupportUpdate",
]
