"""Исходная точка этапа 2: что нынешний KORT делает с источниками полигона.

Пустая база SQLite, файлы полигона - через те входы, что в KORT уже есть:
загрузка реестра (`contracts/importer.py`), подключение 1С (`onec/sync.py` +
`onec/match.py`), разбор выписок (`importing.analyze`). Остальные файлы
(ОСВ, журналы 1С файлами, таблицы аренды и учебного центра) KORT принять
не может - входа для них нет, и это тоже число.

Решения, которые человек принимает при загрузке, пробник принимает самым
простым способом: колонки без поля - пропустить, наши юрлица - как
предложил разбор. Это не проверка этапа, а снимок «до».

`uv run python -m polygon.baseline` из `backend/tests`.
"""
from __future__ import annotations

import json
import sys
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import sqlalchemy as sa


def _isolated_db(folder: Path) -> None:
    from app.finance import db as finance_db_module

    engine = sa.create_engine(f"sqlite:///{folder / 'baseline.db'}", future=True)
    finance_db_module.get_engine = lambda: engine  # type: ignore[assignment]
    finance_db_module._session_factory = None
    finance_db_module._initialized = False


def run() -> dict[str, Any]:
    from app.finance import importing, secretbox
    from app.finance import service as finance_service
    from app.finance.config import finance_settings
    from app.finance.contracts import importer, service
    from app.finance.contracts.fields import number_key
    from app.finance.contracts.models import Contract, GroupEntity
    from app.finance.db import finance_session
    from app.finance.models import Counterparty
    from app.finance.onec import match as onec_match
    from app.finance.onec import sync as onec_sync
    from app.finance.onec.models import OnecBase, OnecDocument
    from polygon import sources
    from polygon.world import build

    world = build()
    files = sources.render(world)
    out: dict[str, Any] = {}
    access = service.Access(view=True, edit=True, setup=True)
    actor = service.Actor(None, "baseline@polygon")

    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)

        # ── 1. Реестр договоров ──
        name = "Excel/Реестр договоров 2026.xlsx"
        batch = importer.start(session, workspace, actor, files[name], "Реестр договоров 2026.xlsx")
        sections = {section["key"]: section for section in batch.report["sections"]}
        blocks = sections.get("blocks", {}).get("items", [])
        asked = [c for b in sections.get("columns", {}).get("items", []) for c in b["columns"] if c["decision"].get("action") == "ask"]
        mapped = Counter(c["decision"].get("field") or c["decision"].get("action") for b in sections.get("columns", {}).get("items", []) for c in b["columns"])
        own = [item["key"] for item in sections.get("entities", {}).get("items", []) if item["own"]]
        registry_out: dict[str, Any] = {
            "blocks": len(blocks),
            "block_titles": [b.get("title") or b["id"] for b in blocks],
            "blocking": batch.report.get("blocking"),
            "columns_asked": len(asked),
            "columns": dict(mapped),
            "own_proposed": [item["names"] for item in sections.get("entities", {}).get("items", []) if item["own"]],
        }
        # Человек на экране загрузки: «Наше ТОО» - исполнитель, сумма - сумма, колонка
        # без шапки под «Договор» - дата договора; остальное без поля - пропустить.
        columns: dict[str, Any] = {}
        for block in sections.get("columns", {}).get("items", []):
            for column in block["columns"]:
                header = str(column.get("header") or "").lower()
                if "наше тоо" in header:
                    columns[column["id"]] = {"action": "field", "field": "executor"}
                elif "в месяц" in header or "стоимость" in header:
                    columns[column["id"]] = {"action": "field", "field": "amount"}
                elif not header and column["index"] == 4:
                    columns[column["id"]] = {"action": "field", "field": "signed_at"}
                elif column["decision"].get("action") == "ask":
                    columns[column["id"]] = {"action": "skip"}
        registry_out["columns_by_hand"] = len(columns)
        batch = importer.decide(session, workspace, batch.id, {"columns": columns})
        sections = {section["key"]: section for section in batch.report["sections"]}
        own = [item["key"] for item in sections.get("entities", {}).get("items", []) if item["own"]]
        registry_out["own_proposed"] = [item["names"] for item in sections.get("entities", {}).get("items", []) if item["own"]]
        rules = {item["block"]: {"action": "accept"} for item in sections.get("rules", {}).get("items", [])}
        batch = importer.decide(session, workspace, batch.id, {"entities": {"confirmed": True, "own": own}, "rules": rules})
        registry_out["blocking_after"] = batch.report.get("blocking")
        try:
            result = importer.apply(session, workspace, access, actor, batch.id)
            registry_out["created"] = result.get("created")
            registry_out["failed"] = len(result.get("failed") or [])
        except Exception as exc:  # noqa: BLE001 - пробник записывает отказ как есть
            registry_out["apply_error"] = str(exc)
        # Договор KORT ↔ договор правды: номер и клиент (номер «А-01» есть у двух юрлиц, Д2).
        truth_numbers: dict[str, list[str]] = {}
        for k, c in world.contracts.items():
            if c.in_registry:
                truth_numbers.setdefault(number_key(c.registry_number or c.number), []).append(k)
        made = session.execute(
            sa.select(Contract.id, Contract.number, Counterparty.name).outerjoin(Counterparty, Counterparty.id == Contract.customer_id)
            .where(Contract.workspace_id == workspace.id)
        ).all()

        def truth_of(number: str, customer: str | None) -> str | None:
            keys = truth_numbers.get(number_key(number), [])
            if len(keys) > 1:
                keys = [k for k in keys if (world.parties.get(world.contracts[k].party) or world.entities.get(world.contracts[k].party)).name.lower()
                        .replace('"', "") in str(customer or "").lower().replace('"', "")
                        or str(customer or "").lower().replace('"', "") in str(getattr(world.parties.get(world.contracts[k].party), "registry", "")).lower().replace('"', "")]
            return keys[0] if len(keys) == 1 else None

        kort_to_truth = {row.id: truth_of(row.number, row.name) for row in made}
        registry_out["truth_in_registry"] = sum(1 for c in world.contracts.values() if c.in_registry)
        registry_out["found"] = sum(1 for key in kort_to_truth.values() if key)
        registry_out["phantom_created"] = any(number_key(row.number) == number_key("ТС-28/2026") for row in made)
        found_keys = set(kort_to_truth.values())
        registry_out["not_found"] = sorted(
            f"{c.department or c.side}: {c.registry_number or c.number}" for k, c in world.contracts.items() if c.in_registry and k not in found_keys
        )
        with_executor = session.scalar(sa.select(sa.func.count()).select_from(Contract).where(
            Contract.workspace_id == workspace.id, Contract.executor_id.is_not(None)))
        with_amount = session.scalar(sa.select(sa.func.count()).select_from(Contract).where(
            Contract.workspace_id == workspace.id, Contract.amount.is_not(None)))
        registry_out["with_executor"], registry_out["with_amount"] = with_executor, with_amount
        out["registry"] = registry_out

        # ── 2. 1С «Тумар Сервис» ──
        odata = files["1С Тумар Сервис/odata.json"]
        org = odata["Catalog_Организации"][0]["Ref_Key"]
        # Имя - в Python: ILIKE в SQLite не сводит регистр кириллицы.
        own_rows = session.execute(
            sa.select(GroupEntity.counterparty_id, Counterparty.name).join(Counterparty, Counterparty.id == GroupEntity.counterparty_id)
            .where(GroupEntity.workspace_id == workspace.id)
        ).all()
        registry_out["own_entities"] = [row.name for row in own_rows]
        entity_id = next((row.counterparty_id for row in own_rows if "сервис" in row.name.lower()), None)

        class Fake:
            def rows(self, entity: str, **_: Any):
                yield from odata.get(entity, [])

        if not finance_settings.secrets_key:
            finance_settings.secrets_key = "polygon-baseline"
        base = OnecBase(workspace_id=workspace.id, title="1С Тумар Сервис", url="https://1cfresh.kz/a/ea201/1", login="odata",
                        secret=secretbox.seal("x"), group_entity_id=entity_id, organization_ref=org, sync_from=date(2026, 1, 1))
        session.add(base)
        session.flush()
        snapshot = onec_sync.read(Fake(), organization_ref=org, since=date(2026, 1, 1))
        onec_sync.apply(session, base, snapshot, since=base.sync_from)
        index = onec_match.build_index(session, workspace.id)
        ref_to_truth: dict[str, str] = {}
        for doc in world.documents:
            for kind in ("invoice", "sale"):
                ref_to_truth[sources.ref(kind, doc.key)] = doc.contract
        for payment in world.payments:
            ref_to_truth[sources.ref("payment", payment.key)] = payment.alloc[0][0] if payment.alloc else ""
        states: Counter[str] = Counter()
        right = wrong = 0
        for doc in session.scalars(sa.select(OnecDocument).where(OnecDocument.base_id == base.id)):
            if doc.kind not in ("invoice", "sale", "payment"):
                continue
            found = onec_match.match_own(doc, entity_id, index)
            states[found.state] += 1
            if found.state == "ok":
                truth_key = ref_to_truth.get(doc.ref.split(":")[0])
                if kort_to_truth.get(found.contract_id) == truth_key:
                    right += 1
                else:
                    wrong += 1
        out["onec"] = {
            "entity_found": entity_id is not None,
            "documents": snapshot.counts(),
            "states": dict(states),
            "ok_right": right,
            "ok_wrong": wrong,
            "parties_with_bin_in_kort": session.scalar(
                sa.select(sa.func.count()).select_from(Counterparty).where(Counterparty.workspace_id == workspace.id, Counterparty.bin.is_not(None), Counterparty.bin != "")
            ),
        }

    # ── 3. Выписки ──
    statements: dict[str, Any] = {}
    for name, data in files.items():
        if not name.startswith("Банк/"):
            continue
        short = name.split("Kaspi ")[1].rsplit(" ", 1)[0]
        entity = next(e for e in world.entities.values() if e.short == short)
        preview = importing.analyze(data, Path(name).name, [f"Kaspi {short}"], account_numbers={entity.iban: f"Kaspi {short}"})
        states = Counter(row.state for row in preview.rows)
        statements[Path(name).stem] = {"question": bool(preview.question), **dict(states)}
    out["statements"] = statements
    out["statements_total"] = dict(sum((Counter({k: v for k, v in s.items() if k != "question"}) for s in statements.values()), Counter()))
    out["truth_payments"] = len(world.payments)

    # ── 4. Файлы без входа ──
    out["no_entry"] = [name for name in files if name.endswith(".xlsx") and not name.startswith("Банк/") and "Реестр" not in name]
    out["truth_owed"] = {k: str(v) for k, v in world.owed().items()}
    return out


def main() -> int:
    # Запуск из `backend/tests`: пакет `app` лежит уровнем выше.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    # SQLite держит файл до конца процесса - на Windows папку не удалить сразу.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        _isolated_db(Path(folder))
        result = run()
    json.dump(result, sys.stdout, ensure_ascii=False, indent=1, default=str)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
