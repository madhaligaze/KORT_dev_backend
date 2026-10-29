"""Кто спрашивает: сессия, компания, права реестра и видов, автор записи.

Маршрут собирает это из вошедшего (`of_member`), бот - из системы
(`system`): вызовы модели одни и те же, разница только в правах.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.finance.contracts.service import Access, Actor, access_of
from app.finance.core import views as views_module
from app.finance.models import Workspace


@dataclass
class Ctx:
    session: Session
    workspace: Workspace
    #: Права реестра: видимые договоры, скрытые и закрытые поля, правка.
    access: Access
    #: Права видов: чьи виды видны и кто их правит.
    viewer: views_module.Viewer
    #: Автор записи - для журнала и точек восстановления; `None` - только чтение.
    actor: Actor | None = None
    #: Уровень по каждому разделу (`access.RESOURCES`): `none` / `view` / `edit`.
    levels: Mapping[str, str] = field(default_factory=dict)

    def level(self, resource: str) -> str:
        if self.viewer.admin:
            return "edit"
        return self.levels.get(resource, "none")

    def can(self, resource: str, level: str = "view") -> bool:
        rank = {"none": 0, "view": 1, "edit": 2}
        return rank[self.level(resource)] >= rank[level]

    @property
    def login(self) -> str:
        return self.actor.email if self.actor is not None else ""


def of_member(session: Session, workspace: Workspace, member: Any) -> Ctx:
    from app.finance.access import RESOURCES

    rights = getattr(member, "rights", None)
    levels = {item.key: rights.level(item.key) for item in RESOURCES} if rights is not None else {}
    login = getattr(member, "login", "") or getattr(member, "email", "")
    if getattr(member, "key", ""):
        # Записанное ключом API подписано ключом (`auth.resolve_key`).
        login = f"{login} · ключ «{member.key_title}»"
    return Ctx(
        session=session,
        workspace=workspace,
        access=access_of(member),
        viewer=views_module.viewer_of(member),
        actor=Actor(member.user_id, login),
        levels=levels,
    )


def system(session: Session, workspace: Workspace, *, user_id: uuid.UUID | None = None) -> Ctx:
    """Система (бот, фоновая задача): видит всё, пишет «система»."""
    return Ctx(
        session=session,
        workspace=workspace,
        access=Access(view=True, edit=True, setup=True, admin=True, full_contracts=True, user_id=user_id),
        viewer=views_module.Viewer(user_id=user_id, admin=True),
        actor=Actor(user_id, "система"),
    )


__all__ = ["Ctx", "of_member", "system"]
