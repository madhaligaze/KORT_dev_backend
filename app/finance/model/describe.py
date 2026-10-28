"""Описать - схема компании для фронта: что есть и что из этого можно.

Фронт рисует по этому ответу, а не по зашитому коду: поля договора с типами,
смыслами значений и правом правки; меры и источники видов с их
группировками и параметрами; виды, которые человек видит, - какие в колонке,
какие он может править; наши юрлица и отделы; команды «применить» и
доступны ли они. Курсор подписки - тот, с которого ответ верен.
"""
from __future__ import annotations

from typing import Any

from app.finance.core import views as views_module
from app.finance.model import VERSION
from app.finance.model.commands import COMMANDS, VIEW_COMMANDS
from app.finance.model.context import Ctx
from app.finance.model.stream import cursor_now

#: Параметры источников - что можно спросить у вида сверх отбора.
SOURCE_PARAMS = {
    "contracts": {},
    "debts": {
        "side": {"values": list(views_module.DEBT_SIDES), "default": "receivable", "title": "Сторона"},
        "basis": {"values": list(views_module.DEBT_BASES), "default": "accruals", "title": "Основание"},
        "group": {"values": list(views_module.DEBT_GROUPS), "default": "client", "title": "Группа"},
    },
    "unbilled": {
        "own": {"title": "Наше юрлицо (код)"},
        "department": {"title": "Отдел"},
    },
}


def describe(ctx: Ctx) -> dict[str, Any]:
    from app.finance.access import RESOURCES
    from app.finance.contracts import setup

    access = ctx.access
    registry: dict[str, Any] = {}
    if access.view:
        schema = setup.schema(ctx.session, ctx.workspace, access)
        registry = {
            "fields": schema["fields"],
            "lists": {key: [{k: v for k, v in item.items() if k != "similar"} for item in items] for key, items in schema["lists"].items()},
            "entities": schema["own_entities"],
            "departments": schema["departments"],
            "status_phases": schema["status_phases"],
            "billing_kinds": schema["billing_kinds"],
            "mode_fields": schema["mode_fields"],
            "schema_rev": schema["schema_rev"],
        }
    listing = views_module.listing(ctx.session, ctx.workspace, ctx.viewer)
    return {
        "version": VERSION,
        "company": {"id": str(ctx.workspace.id), "title": ctx.workspace.title or ""},
        "today": _today(),
        "me": {
            "user_id": str(ctx.viewer.user_id) if ctx.viewer.user_id else None,
            "admin": ctx.viewer.admin,
            "levels": {item.key: ctx.level(item.key) for item in RESOURCES},
            "contracts": {
                "view": access.view, "edit": access.edit, "setup": access.setup, "rows": access.rows,
                "hidden": sorted(access.hidden), "readonly": sorted(access.readonly),
            },
        },
        **registry,
        "measures": listing["measures"],
        "sources": {
            key: {**value, "resource": views_module.SOURCES[key].resource, "open": ctx.viewer.sees(views_module.SOURCES[key].resource), "params": SOURCE_PARAMS[key]}
            for key, value in listing["sources"].items()
        },
        "views": listing["views"],
        "templates": listing["templates"],
        "commands": [
            {
                "name": command.name,
                "title": command.title,
                "open": command.name in VIEW_COMMANDS or access.edit,
            }
            for command in COMMANDS.values()
        ],
        "cursor": cursor_now(ctx),
    }


def _today() -> str:
    from app.finance.contracts.service import today

    return today().isoformat()


__all__ = ["SOURCE_PARAMS", "describe"]
