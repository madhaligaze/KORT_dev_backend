"""Слои настройки (фаза 9, R15 и R17): шаблон → компания → отдел → человек.

Новая компания начинается с шаблона «Компания услуг по договорам»; всё, чем
она отличается от него, видно списком: переименованное, другой смысл, своё,
убранное. BBC - такой же слой компании на том же шаблоне.
"""
from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.finance import service as finance_service
from app.finance.contracts import service, setup
from app.finance.contracts.fields import ensure_registry
from app.finance.contracts.models import ListValue
from app.finance.core import layers
from app.finance.db import finance_session
from app.finance.layout import norm

FULL = service.Access(view=True, edit=True, setup=True, admin=True)


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    yield engine


def _value(session, field_key: str, title: str) -> ListValue:
    return session.scalar(sa.select(ListValue).where(ListValue.field_key == field_key, ListValue.normalized == norm(title)))


def test_kompaniya_sloem_na_shablone(finance_db):
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        ensure_registry(session, workspace)
        first = layers.layers(session, workspace, FULL)
        assert first["template"]["title"] == "Компания услуг по договорам"
        for item in first["company"]["lists"]:
            assert item["counts"]["template"] == first["template"]["lists"][item["key"]], item["key"]
            assert not (item["counts"]["renamed"] or item["counts"]["meaning"] or item["counts"]["added"] or item["counts"]["removed"])

        # Компания: статус под своим словом, у аренды - другой смысл начисления, свой статус без фазы.
        setup.update_value(session, workspace, _value(session, "status", "Действующий").id, {"value": "Активный"})
        setup.update_value(session, workspace, _value(session, "type", "Аренда").id, {"meaning": {"billing": "total"}})
        setup.add_value(session, workspace, "status", "нужно закрыть по бух")
        session.flush()
        after = {item["key"]: item for item in layers.layers(session, workspace, FULL)["company"]["lists"]}
        states = {(item["template"], item["value"]): item["state"] for item in after["status"]["items"]}
        assert states[("Действующий", "Активный")] == "renamed", "статус узнаётся по фазе, подпись - слово компании"
        assert states[(None, "нужно закрыть по бух")] == "added"
        rent = next(item for item in after["type"]["items"] if item["template"] == "Аренда")
        assert rent["state"] == "meaning" and rent["meaning"] == {"billing": "total"}
        assert rent["template_meaning"]["billing"] == "month", "смысл шаблона - чтобы вернуть его одной правкой"
        assert after["type"]["counts"]["meaning"] == 1 and after["status"]["counts"]["added"] == 1

        with pytest.raises(PermissionError):
            layers.layers(session, workspace, service.Access(view=True, edit=True))


def test_sloi_otdel_i_chelovek(finance_db):
    from app.finance.accounts_model import AccessGrant
    from app.finance.contracts.models import Department

    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        ensure_registry(session, workspace)
        department = Department(workspace_id=workspace.id, code="ЮО", normalized_name=norm("Юридический отдел"), title="Юридический отдел")
        session.add(department)
        session.flush()
        session.add(AccessGrant(workspace_id=workspace.id, subject_kind="department", subject_id=department.id, resource="contracts", level="edit"))
        session.add(AccessGrant(workspace_id=workspace.id, subject_kind="employee", subject_id=uuid.uuid4(), resource="journal", level="view"))
        session.flush()
        out = layers.layers(session, workspace, FULL)
        assert [(item["code"], item["grants"]) for item in out["departments"]] == [("ЮО", 1)]
        assert out["people"]["with_own_rights"] == 1
