"""Соглашения: «Разобрать» текст из файла и подтвердить по куску.

В реестре BBC соглашения — свободный текст двух колонок: «№ 1 от 23.07.2024\\n
№2 10.12.2025» и «1) внесен изменение в п. 1.2. а\\n2) увелечение цены на
250 000». В одной ячейке бывает несколько соглашений, включая замену лиц.

Разбор здесь **только предлагает**: режет текст на куски с границами (`start`,
`end` — по ним карточка подчёркивает кусок в тексте), подсказывает номер, дату
и что соглашение меняет. В базу пишется лишь подтверждённый человеком кусок,
и только он влияет на значение договора на прошлый месяц. Сумму из «увеличение
цены на 250 000» разбор не вычисляет: «на» — это прибавка или новая цена, из
текста не следует, и цифра, посчитанная наугад, выглядела бы как цифра.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history
from app.finance.contracts.models import AMENDMENT_EFFECTS, Contract, ContractAmendment
from app.finance.contracts.service import (
    Access,
    Actor,
    NotFound,
    Registry,
    _apply_amendment,
    _derive,
    _finish,
    _label,
    _next_amendment_position,
    _plain,
    ensure_writable,
    get_contract,
    read_date,
    read_money,
    today,
    value_of,
)
from app.finance.models import Workspace
from app.finance.service import FinanceError

_DATE = r"\d{1,2}[./]\d{1,2}[./]\d{2,4}"
#: Одно соглашение в колонке номеров: «№ 1 от 23.07.2024», «№2 10.12.2025»,
#: «Соглашение о замене лиц от 01.01.2026».
_PIECE = re.compile(
    rf"(?P<head>(?:№\s*[\w\-/]+|соглашени[еяи]\s+о\s+замене\s+лиц|доп\.?\s*соглашени[еяи]\s*№?\s*[\w\-/]*))"
    rf"\s*(?:от\s*)?(?P<date>{_DATE})?",
    re.IGNORECASE,
)
#: Граница пункта в колонке предметов: «1)», «2)» или «№ 1 …» с новой
#: строки или после запятой.
_SUMMARY_SPLIT = re.compile(r"(?:^|\n|,\s*)(?=\s*(?:\d+\)|№\s*\d+))", re.IGNORECASE)
_REPLACE = re.compile(r"замен\w*\s+лиц", re.IGNORECASE)
_AMOUNT = re.compile(r"(увелич|уменьш|цен|сумм|стоимост)", re.IGNORECASE)
_TO_PARTY = re.compile(r"замен\w*\s+лиц\w*\s+на\s+([^,;\n]+?)(?:\s+и\s+|[,;\n]|$)", re.IGNORECASE)


@dataclass
class Piece:
    index: int
    text: str
    start: int
    end: int
    number: str
    signed_at: str | None
    summary: str
    summary_start: int | None
    summary_end: int | None
    effect: str
    effective_from: str | None
    #: Подсказка значения: «BBC» для замены лиц; для суммы — текст, не число.
    value_hint: str
    #: Какие изменения этого куска уже подтверждены.
    confirmed: list[str] | None = None


def _pieces_of(text: str) -> list[tuple[int, int, str, str | None]]:
    out: list[tuple[int, int, str, str | None]] = []
    for match in _PIECE.finditer(text or ""):
        head = match.group("head").strip()
        if head.startswith("№") and not re.search(r"\d", head):
            continue
        out.append((match.start(), match.end(), head, match.group("date")))
    return out


def _summaries_of(text: str) -> list[tuple[int, int, str]]:
    if not (text or "").strip():
        return []
    cuts = sorted({m.start() for m in _SUMMARY_SPLIT.finditer(text)} | {0})
    out: list[tuple[int, int, str]] = []
    for index, start in enumerate(cuts):
        end = cuts[index + 1] if index + 1 < len(cuts) else len(text)
        chunk = text[start:end]
        stripped = chunk.strip(" ,\n")
        if stripped:
            offset = start + chunk.find(stripped)
            out.append((offset, offset + len(stripped), stripped))
    return out


def _effect_of(head: str, summary: str) -> tuple[str, str]:
    """Что соглашение меняет — подсказка, а не решение."""
    whole = f"{head} {summary}"
    if _REPLACE.search(whole):
        found = _TO_PARTY.search(whole)
        return "executor", (found.group(1).strip() if found else "")
    if _AMOUNT.search(summary):
        figure = re.search(r"\d[\d\s ]*\d", summary)
        return "amount", (figure.group(0).strip() if figure else "")
    return "none", ""


def _iso(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        value = read_date(raw, field="Дата соглашения")
    except FinanceError:
        return None
    return value.isoformat() if value else None


def parse(session: Session, workspace: Workspace, contract_id: uuid.UUID) -> list[dict[str, Any]]:
    """Куски текста соглашений с подсказками. Ничего не пишет."""
    contract = get_contract(session, workspace, contract_id)
    heads = _pieces_of(contract.amendments_text or "")
    summaries = _summaries_of(contract.amendments_summary_text or "")
    # Пункты сопоставляются по порядку, только если их столько же, сколько
    # номеров. Иначе пара «номер ↔ пункт» — догадка; предмет остаётся общим.
    paired = len(heads) == len(summaries)
    confirmed: dict[str, list[str]] = {}
    for row in session.scalars(
        sa.select(ContractAmendment).where(
            ContractAmendment.contract_id == contract.id, ContractAmendment.origin == "parsed"
        )
    ):
        confirmed.setdefault(row.piece, []).append(row.effect)
    out: list[Piece] = []
    if not heads and (contract.amendments_text or "").strip():
        text = contract.amendments_text.strip()
        heads = [(0, len(text), text, None)]
    for index, (start, end, head, raw_date) in enumerate(heads):
        summary, s_start, s_end = "", None, None
        if paired:
            s_start, s_end, summary = summaries[index]
        elif len(heads) == 1:
            summary = (contract.amendments_summary_text or "").strip()
            s_start, s_end = (0, len(contract.amendments_summary_text or "")) if summary else (None, None)
        effect, hint = _effect_of(head, summary)
        number = head if head.startswith("№") else ""
        signed = _iso(raw_date)
        text = (contract.amendments_text or "")[start:end]
        out.append(
            Piece(
                index=index,
                text=text,
                start=start,
                end=end,
                number=number,
                signed_at=signed,
                summary=summary,
                summary_start=s_start,
                summary_end=s_end,
                effect=effect,
                effective_from=signed,
                value_hint=hint,
                confirmed=confirmed.get(text, []),
            )
        )
    return [asdict(item) for item in out]


def confirm(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    contract_id: uuid.UUID,
    piece: dict[str, Any],
) -> Contract:
    """Подтвердить кусок: он становится соглашением договора.

    Если соглашение меняет сторону или сумму и указано новое значение с датой,
    оно действует так же, как «изменение с даты»: дата прошла — значение
    договора становится новым, дата впереди — ждёт своего дня.
    """
    if not access.edit:
        raise PermissionError("Подтверждать соглашения вам не открыто")
    registry = Registry(session, workspace)
    contract = get_contract(session, workspace, contract_id, for_update=True)
    ensure_writable(session, registry, access, contract)
    effect = str(piece.get("effect") or "none")
    if effect not in AMENDMENT_EFFECTS:
        raise FinanceError("Такого изменения у соглашения нет")
    text = str(piece.get("text") or "").strip()
    if not text:
        raise FinanceError("Пустой кусок текста")
    # Один кусок может нести два изменения («замена лиц на BBC и увеличение
    # суммы») — подтверждается каждое отдельно, но одно и то же дважды нельзя.
    exists = session.scalar(
        sa.select(ContractAmendment.id).where(
            ContractAmendment.contract_id == contract.id,
            ContractAmendment.origin == "parsed",
            ContractAmendment.piece == text,
            ContractAmendment.effect == effect,
        )
    )
    if exists is not None:
        raise FinanceError("Этот кусок с таким изменением уже подтверждён")
    effective_from = read_date(piece.get("effective_from"), field="С какой даты")
    signed_at = read_date(piece.get("signed_at"), field="Дата соглашения")
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    value = piece.get("value")
    if effect in ("executor", "customer", "amount") and value not in (None, ""):
        if effective_from is None:
            raise FinanceError("Укажите, с какой даты действует изменение")
        before = {effect: value_of(contract, effect)}
        if effect == "amount":
            amount, terms = read_money(value, field="Сумма")
            after = {"amount": _plain(amount), "amount_terms": terms}
        elif registry.fill(effect) == "own" and not registry.is_own(
            contract.customer_id if effect == "executor" else contract.executor_id
        ):
            party = registry.pick_own(value, title=registry.title(effect))
            after = {effect: str(party.id) if party else None}
        else:
            resolved = registry.resolve_party(value, slot=effect)
            if resolved.ambiguous or resolved.party is None:
                raise FinanceError("Сторона не найдена однозначно — выберите в карточке")
            after = {effect: str(resolved.party.id)}
    amendment = ContractAmendment(
        workspace_id=workspace.id,
        contract_id=contract.id,
        number=str(piece.get("number") or "").strip(),
        signed_at=signed_at,
        summary=str(piece.get("summary") or "").strip(),
        effect=effect,
        effective_from=effective_from,
        before=before,
        after=after,
        origin="parsed",
        piece=text,
        position=_next_amendment_position(session, contract.id),
        created_by=actor.user_id,
    )
    session.add(amendment)
    changed: list[str] = []
    if after and effective_from is not None and effective_from <= today():
        _apply_amendment(contract, amendment, registry)
        changed.append(effect)
        _derive(contract, registry, {effect})
    elif not after:
        amendment.applied_at = None
    session.flush()
    _finish(session, registry, contract, actor, changed)
    history.write(
        session,
        workspace,
        kind="contract.amendment",
        entity="contract",
        entity_id=contract.id,
        title=(
            f"договор {contract.number or ''} · подтверждено соглашение «{text[:60]}»"
            + (
                f": {_label(registry, effect, before.get(effect))} → {_label(registry, effect, after.get(effect))}"
                if after
                else ""
            )
        ),
        before=before,
        after={**after, "piece": text},
        actor=actor.email,
    )
    return contract


def listing(session: Session, workspace: Workspace, contract_id: uuid.UUID) -> list[dict[str, Any]]:
    """Лента соглашений договора: подтверждённые, с «впереди» у будущих."""
    contract = get_contract(session, workspace, contract_id)
    registry = Registry(session, workspace)
    rows = session.scalars(
        sa.select(ContractAmendment)
        .where(ContractAmendment.contract_id == contract.id)
        .order_by(ContractAmendment.effective_from.nulls_last(), ContractAmendment.position)
    )
    out = []
    for row in rows:
        key = row.effect if row.effect in ("executor", "customer", "amount", "end_date") else None
        out.append(
            {
                "id": str(row.id),
                "number": row.number,
                "signed_at": _plain(row.signed_at),
                "summary": row.summary,
                "effect": row.effect,
                "effective_from": _plain(row.effective_from),
                "before": row.before or {},
                "after": row.after or {},
                "before_label": _label(registry, key, (row.before or {}).get(key)) if key else "",
                "after_label": _label(registry, key, (row.after or {}).get(key)) if key else "",
                "origin": row.origin,
                "piece": row.piece,
                "ahead": bool(row.after) and row.applied_at is None,
                "applied_at": _plain(row.applied_at),
            }
        )
    return out


def remove_amendment(
    session: Session, workspace: Workspace, access: Access, actor: Actor, contract_id: uuid.UUID, amendment_id: uuid.UUID
) -> None:
    """Убрать соглашение. Применённое значение договора не откатывается —
    откат делается правкой «опечатка», и это видно в истории."""
    if not access.edit:
        raise PermissionError("Убирать соглашения вам не открыто")
    contract = get_contract(session, workspace, contract_id, for_update=True)
    registry = Registry(session, workspace)
    ensure_writable(session, registry, access, contract)
    row = session.get(ContractAmendment, amendment_id)
    if row is None or row.contract_id != contract.id:
        raise NotFound("Соглашение не найдено")
    session.delete(row)
    _finish(session, registry, contract, actor, [])
    history.write(
        session,
        workspace,
        kind="contract.amendment.remove",
        entity="contract",
        entity_id=contract.id,
        title=f"договор {contract.number or ''} · соглашение убрано {row.number or row.piece[:40]}".strip(),
        before={"number": row.number, "effect": row.effect, "effective_from": _plain(row.effective_from)},
        actor=actor.email,
    )


__all__ = ["confirm", "listing", "parse", "remove_amendment"]
