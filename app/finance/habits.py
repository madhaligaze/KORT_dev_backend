"""Привычки учётки: каким видом раздела человек на деле пользуется.

29.09.2026: «один сотрудник часто открывает именно таблицу, но не особо
работает с карточками - при последующих заходах программа сразу должна
показать табличный вид». Раньше реестр открывался видом, выбранным в этом
браузере последним, а на новом компьютере или после входа - карточками.

Что считается
─────────────
Группа - раздел с несколькими видами («contracts»: «Карточки» и «Таблица»).
У каждого вида - очки: минуты настоящей работы в нём (вкладка на виду и
человек не бездействует - это меряет фронт) и явные переключения на него
(`PICK_WEIGHT` минут за щелчок). Открытие вида по умолчанию не считается
ничем: иначе вид, который открывается сам, набирал бы очки за то, что его
сразу закрывают.

Очки затухают вдвое за `HALF_LIFE_DAYS`: человек, перешедший с карточек на
таблицу, через пару недель получает таблицу по умолчанию, а не через полгода.

Предпочтение - только при явном перевесе (`MIN_TOTAL`, `MIN_SHARE`): при
равном пользовании оба вида одинаково хороши, и раздел открывается тем, что
выбрано последним.

Хранится за учёткой и компанией в личном хранилище экрана (`SheetLook`,
ключ `habits`) - рядом с личным видом листов: это настройка одного человека,
а не данные компании, и в журнал действий она не пишется.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

HALF_LIFE_DAYS = 14.0
#: Щелчок по виду весит, как столько минут работы в нём.
PICK_WEIGHT = 2.0
#: Меньше этого (в минутах) - привычки ещё нет.
MIN_TOTAL = 5.0
#: Доля лидера, при которой вид становится видом по умолчанию.
MIN_SHARE = 0.6
#: Больше этого за один отчёт фронта не бывает (он шлёт раз в минуту).
MAX_MINUTES = 30.0
KEY = "habits"
_NAME = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")


class HabitError(ValueError):
    pass


def _decay(score: float, since: datetime | None, now: datetime) -> float:
    if since is None:
        return score
    days = max(0.0, (now - since).total_seconds() / 86400)
    return score * 0.5 ** (days / HALF_LIFE_DAYS)


def _parse(stamp: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(stamp)) if stamp else None
    except ValueError:
        return None


def record(
    data: dict[str, Any], group: str, mode: str, *, minutes: float = 0.0, pick: bool = False, now: datetime | None = None
) -> dict[str, Any]:
    """Добавить отчёт фронта к привычкам. Возвращает новые данные (не меняя старые)."""
    if not _NAME.match(group or "") or not _NAME.match(mode or ""):
        raise HabitError("Такого раздела или вида нет")
    try:
        minutes = float(minutes)
    except (TypeError, ValueError) as exc:
        raise HabitError("Время - числом минут") from exc
    minutes = min(max(minutes, 0.0), MAX_MINUTES)
    now = now or datetime.now(timezone.utc)
    groups = dict((data or {}).get("groups") or {})
    item = dict(groups.get(group) or {})
    since = _parse(item.get("at"))
    modes = {key: _decay(float(value), since, now) for key, value in (item.get("modes") or {}).items()}
    modes[mode] = modes.get(mode, 0.0) + minutes + (PICK_WEIGHT if pick else 0.0)
    # Совсем выдохшиеся виды не держим: после полугода без дела там доли минуты.
    modes = {key: round(value, 3) for key, value in modes.items() if value >= 0.01}
    groups[group] = {"at": now.isoformat(), "modes": modes}
    return {**(data or {}), "groups": groups}


def prefer(data: dict[str, Any], *, now: datetime | None = None) -> dict[str, str]:
    """Вид по умолчанию для каждой группы - только где есть явный перевес."""
    now = now or datetime.now(timezone.utc)
    out: dict[str, str] = {}
    for group, item in ((data or {}).get("groups") or {}).items():
        since = _parse((item or {}).get("at"))
        modes = {key: _decay(float(value), since, now) for key, value in ((item or {}).get("modes") or {}).items()}
        total = sum(modes.values())
        if total < MIN_TOTAL:
            continue
        mode, score = max(modes.items(), key=lambda pair: pair[1])
        if score / total >= MIN_SHARE:
            out[group] = mode
    return out


__all__ = ["HabitError", "KEY", "prefer", "record"]
