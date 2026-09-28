"""Вернуть системе память после тяжёлой операции.

CPython отдаёт освобождённые объекты своему распределителю, тот — glibc, а
glibc держит освободившиеся арены у себя. После разбора реестра на 20 000
строк процесс так и остаётся на пике: стресс-прогон 24.09 — 197 → 824 → 746
МБ и обратно не вернулся. На Railway за эту память платят, а следующий
пик ляжет поверх.

`malloc_trim(0)` возвращает системе пустые страницы. Зовётся после
разбора файла, «Завести» и выгрузки — редких и тяжёлых операций; на опросе
и обычной правке не нужен. Работает только на Linux (glibc); на Windows и
macOS — ничего не делает.
"""
from __future__ import annotations

import ctypes
import gc
import logging
import sys

log = logging.getLogger(__name__)

_libc: ctypes.CDLL | None = None
_broken = False


def trim() -> None:
    global _libc, _broken
    if _broken or not sys.platform.startswith("linux"):
        return
    try:
        if _libc is None:
            _libc = ctypes.CDLL("libc.so.6")
        gc.collect()
        _libc.malloc_trim(0)
    except (OSError, AttributeError) as exc:  # musl, нет libc.so.6
        _broken = True
        log.info("malloc_trim недоступен: %s", exc)


__all__ = ["trim"]
