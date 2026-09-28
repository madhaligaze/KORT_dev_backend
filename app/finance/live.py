"""Живой режим журнала: номер изменения у каждой записанной операции.

Лист «Таблица» опрашивает сервер так же, как реестр договоров: «что
изменилось после номера N» (`GET /finance/grid/changes?since=`). Номер —
счётчик компании «operations» (`finance.counters`), тот же механизм, что у
договоров: `UPDATE … RETURNING` держит блокировку строки до коммита, поэтому
номера видны строго по порядку и опрос ничего не пропускает.

Кто ставит номер
────────────────
Операцию пишут журнал, лист, карточка, загрузка выписок, автоправила,
повторения, счета, откат из журнала действий. Ставить номер в каждом из этих
мест — значит однажды забыть одно, и правка из него не дойдёт до открытых
листов. Поэтому номер ставят события сессии «Финансов» (`register`), а не
вызывающие:

* после каждого сброса в базу (`after_flush`) запоминается, какие операции
  заведены, изменены, удалены — и чьё разнесение по проектам поменялось;
* перед коммитом (`before_commit`) — один сдвиг счётчика на компанию и один
  `UPDATE` номера всем тронутым операциям. Не раньше: загрузка выписки на
  тысячи строк сбрасывается в базу много раз, и сдвиг в начале держал бы
  блокировку счётчика всю загрузку — ровно так «Завести» реестра держало
  правки коллег (стресс-прогон 24.09).

Удалённые мягко (`deleted_at`) приходят в опрос сами; удалённые `DELETE`
(план из счёта и из повторения) оставляют запись в `operation_removals`.
"""
from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from app.finance.models import Operation, OperationProject, OperationRemoval, Workspace

OPERATIONS = "operations"
_TOUCHED = "finance.live.touched"
_REMOVED = "finance.live.removed"
_CHUNK = 1000


def _after_flush(session: Session, _context: Any) -> None:
    # В after_flush списки new/dirty/deleted ещё показывают то, что сбрасывалось.
    touched: dict[uuid.UUID, uuid.UUID | None] = session.info.setdefault(_TOUCHED, {})
    removed: set[tuple[uuid.UUID, uuid.UUID]] = session.info.setdefault(_REMOVED, set())
    for obj in session.new:
        if isinstance(obj, Operation):
            touched[obj.id] = obj.workspace_id
        elif isinstance(obj, OperationProject):
            touched.setdefault(obj.operation_id, None)
    for obj in session.dirty:
        if isinstance(obj, Operation):
            if session.is_modified(obj, include_collections=False):
                touched[obj.id] = obj.workspace_id
        elif isinstance(obj, OperationProject):
            touched.setdefault(obj.operation_id, None)
    for obj in session.deleted:
        if isinstance(obj, Operation):
            removed.add((obj.workspace_id, obj.id))
            touched.pop(obj.id, None)
        elif isinstance(obj, OperationProject):
            touched.setdefault(obj.operation_id, None)


def _before_commit(session: Session) -> None:
    pending = bool(session.new or session.dirty or session.deleted)
    if not pending and not session.info.get(_TOUCHED) and not session.info.get(_REMOVED):
        return
    # Несброшенное попадёт в after_flush — сбрасываем сами, до расстановки.
    session.flush()
    touched: dict[uuid.UUID, uuid.UUID | None] = session.info.pop(_TOUCHED, {})
    removed: set[tuple[uuid.UUID, uuid.UUID]] = session.info.pop(_REMOVED, set())
    if not touched and not removed:
        return
    from app.finance.contracts.fields import bump

    unknown = [oid for oid, ws in touched.items() if ws is None]
    if unknown:
        for oid, ws in session.execute(
            sa.select(Operation.id, Operation.workspace_id).where(Operation.id.in_(unknown))
        ):
            touched[oid] = ws
    by_workspace: dict[uuid.UUID, list[uuid.UUID]] = {}
    for oid, ws in touched.items():
        if ws is not None:
            by_workspace.setdefault(ws, []).append(oid)
    for ws, _oid in removed:
        by_workspace.setdefault(ws, [])
    for ws, ids in by_workspace.items():
        seq = bump(session, ws, OPERATIONS)
        for start in range(0, len(ids), _CHUNK):
            session.execute(
                sa.update(Operation)
                .where(Operation.id.in_(ids[start : start + _CHUNK]))
                .values(seq=seq)
                .execution_options(synchronize_session=False)
            )
        for removed_ws, oid in removed:
            if removed_ws == ws:
                session.add(OperationRemoval(workspace_id=ws, operation_id=oid, seq=seq))
    session.flush()
    # Записи удалений сброшены — их сброс не касается операций, но after_flush
    # всё равно завёл пустые наборы; убираем, чтобы не тащить их дальше.
    session.info.pop(_TOUCHED, None)
    session.info.pop(_REMOVED, None)


def _after_rollback(session: Session) -> None:
    session.info.pop(_TOUCHED, None)
    session.info.pop(_REMOVED, None)


def register(factory: sessionmaker[Session]) -> None:
    """Повесить номера изменений на фабрику сессий «Финансов» (один раз на фабрику)."""
    if getattr(factory, "_finance_live", False):
        return
    event.listen(factory, "after_flush", _after_flush)
    event.listen(factory, "before_commit", _before_commit)
    event.listen(factory, "after_rollback", _after_rollback)
    factory._finance_live = True  # type: ignore[attr-defined]


def current_seq(session: Session, workspace_id: uuid.UUID) -> int:
    from app.finance.contracts.fields import current

    return current(session, workspace_id, OPERATIONS)


def changes(session: Session, workspace: Workspace, since: int) -> dict[str, Any]:
    """Строки листа, изменённые после `since`, и снятые с листа.

    Номер — до выборки строк: правка, закоммиченная между ними, иначе
    попала бы под курсор и не пришла бы ни здесь, ни следующим опросом.
    """
    from app.finance import grid as grid_module

    seq_now = current_seq(session, workspace.id)
    if seq_now <= since:
        return {"rows": [], "removed": [], "seq": seq_now}
    changed = list(
        session.scalars(
            sa.select(Operation)
            .where(Operation.workspace_id == workspace.id, Operation.seq > since)
            .order_by(Operation.seq)
        )
    )
    live = [item for item in changed if item.deleted_at is None]
    removed = [str(item.id) for item in changed if item.deleted_at is not None]
    removed.extend(
        str(operation_id)
        for operation_id in session.scalars(
            sa.select(OperationRemoval.operation_id).where(
                OperationRemoval.workspace_id == workspace.id, OperationRemoval.seq > since
            )
        )
    )
    rows = grid_module.build_grid(session, workspace, live)["rows"] if live else []
    return {"rows": rows, "removed": removed, "seq": seq_now}


__all__ = ["OPERATIONS", "changes", "current_seq", "register"]
