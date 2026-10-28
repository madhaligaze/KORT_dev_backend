"""Ссылка на число - что «объяснить» должно раскрыть.

Ссылка непрозрачна для клиента: он получает её рядом с числом в ответе
«спросить» или в слагаемом «объяснить» и отдаёт обратно как есть. Внутри -
короткий JSON в base64url: вид числа (`k`) и всё, чтобы посчитать его снова, в
том числе вопрос без сохранения (`q`), - сервер ничего не хранит. Подделанная
ссылка ничего не открывает: «объяснить» считает заново с правами того, кто
спрашивает.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from app.finance.service import FinanceError

#: Ссылка длиннее - уже не число, а попытка прислать в ней что-то другое.
LIMIT = 4096


def make(kind: str, **data: Any) -> str:
    payload = {"k": kind, **{key: value for key, value in data.items() if value is not None}}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def read(ref: str) -> dict[str, Any]:
    text = (ref or "").strip()
    if not text or len(text) > LIMIT:
        raise FinanceError("Ссылка на число пуста или слишком длинная")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise FinanceError("Ссылка на число записана неверно - возьмите её из ответа «спросить»") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("k"), str):
        raise FinanceError("Ссылка на число записана неверно - возьмите её из ответа «спросить»")
    return payload


__all__ = ["LIMIT", "make", "read"]
