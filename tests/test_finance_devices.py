"""Подтверждение устройства для слабых, но уже заданных паролей.

Новые пароли политика слабыми быть не даёт (`test_finance_auth` и ниже), а
заданные до неё (в том числе у владельца) со слабым паролем пускают только с
подтверждённого устройства: с нового вход ждёт одобрения с того, где человек
уже вошёл.
"""
from __future__ import annotations

import uuid

import pytest

from app.finance import auth, passwords
from app.finance.auth import Member, NeedsDeviceApproval
from app.finance.accounts_model import FinanceUser
from app.finance.db import finance_session


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    from app.finance.config import finance_settings

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    # Механизм по умолчанию выключен (в разработке он мешает) - здесь включаем,
    # чтобы проверить его работу.
    monkeypatch.setattr(finance_settings, "device_approval", True)
    yield engine


def _owner_with_weak_password(weak: str = "12345678"):
    """Владелец, которому пароль задали до политики - слабый и уже в базе."""
    with finance_session() as session:
        member, _token = auth.register(
            session, email="owner@example.com", password="strong-pass-1", company="ТОО Ромашка"
        )
        user = session.get(FinanceUser, member.user_id)
        user.password_hash = auth.hash_password(weak)  # как будто задан давно
        session.flush()
        return member.user_id, member.workspace_id


def _anchor(user_id, workspace_id) -> Member:
    """Снимок уже вошедшего - с него подтверждают новое устройство."""
    return Member(
        user_id=user_id, email="owner@example.com", full_name="Владелец",
        workspace_id=workspace_id, workspace_title="ТОО Ромашка", role="owner",
        must_change_password=False, session_id=uuid.uuid4(),
    )


def test_politika_otvergaet_slaboe_i_propuskaet_normalnoe() -> None:
    assert passwords.problem("12345678") is not None
    assert passwords.problem("password") is not None
    assert passwords.problem("qwerty123") is not None
    assert passwords.problem("1234") is not None  # короче восьми
    assert passwords.problem("pass-12345") is None
    assert passwords.problem("secret-123") is None
    assert passwords.problem("heir-pass-1") is None


def test_vyklyuchennyy_mehanizm_puskaet_slabyy_parol_srazu(finance_db, monkeypatch) -> None:
    """По умолчанию (в разработке) подтверждения нет: слабый пароль входит сразу."""
    from app.finance.config import finance_settings

    monkeypatch.setattr(finance_settings, "device_approval", False)
    _owner_with_weak_password()
    with finance_session() as session:
        member, token = auth.login(session, email="owner@example.com", password="12345678", device_token=None)
    assert member.role == "owner" and token


def test_silnyy_parol_s_novogo_ustroystva_puskaet_srazu(finance_db) -> None:
    """Сильный пароль устройства не спрашивает."""
    with finance_session() as session:
        auth.register(session, email="a@b.kz", password="strong-pass-1", company="ТОО")
    with finance_session() as session:
        member, token = auth.login(session, email="a@b.kz", password="strong-pass-1", device_token=None)
    assert member.role == "owner" and token


def test_slabyy_parol_s_novogo_ustroystva_zhdet_podtverzhdeniya(finance_db) -> None:
    user_id, workspace_id = _owner_with_weak_password()
    # Новое устройство (без метки) - сеанс не открывается, появляется ожидание.
    with finance_session() as session:
        with pytest.raises(NeedsDeviceApproval) as caught:
            auth.login(session, email="owner@example.com", password="12345678", device_token=None)
    device_token = caught.value.set_token
    assert device_token  # метку выдали, чтобы ждущий экран ею забрал сеанс

    with finance_session() as session:
        pend = auth.pending_devices(session, user_id)
    assert len(pend) == 1

    # Пока не подтвердили - забрать нечего.
    with finance_session() as session:
        assert auth.claim_device(session, device_token) == "pending"

    # Подтверждаем с того, где уже вошли.
    with finance_session() as session:
        auth.decide_device(session, _anchor(user_id, workspace_id), uuid.UUID(pend[0]["id"]), trust=True)

    # Ждущий экран забирает сеанс - один раз.
    with finance_session() as session:
        result = auth.claim_device(session, device_token)
    assert isinstance(result, tuple)
    member, token = result
    assert member.user_id == user_id and token
    with finance_session() as session:
        assert auth.claim_device(session, device_token) == "none"  # второй раз нечего


def test_podtverzhdennoe_ustroystvo_puskaet_pryamo(finance_db) -> None:
    user_id, workspace_id = _owner_with_weak_password()
    with finance_session() as session:
        with pytest.raises(NeedsDeviceApproval) as caught:
            auth.login(session, email="owner@example.com", password="12345678", device_token=None)
    device_token = caught.value.set_token
    with finance_session() as session:
        pend = auth.pending_devices(session, user_id)
        auth.decide_device(session, _anchor(user_id, workspace_id), uuid.UUID(pend[0]["id"]), trust=True)
    # Та же метка, тот же слабый пароль - теперь сразу сеанс, без ожидания.
    with finance_session() as session:
        member, token = auth.login(
            session, email="owner@example.com", password="12345678", device_token=device_token
        )
    assert member.user_id == user_id and token


def test_otklonennoe_ustroystvo_ne_puskaet(finance_db) -> None:
    user_id, workspace_id = _owner_with_weak_password()
    with finance_session() as session:
        with pytest.raises(NeedsDeviceApproval) as caught:
            auth.login(session, email="owner@example.com", password="12345678", device_token=None)
    device_token = caught.value.set_token
    with finance_session() as session:
        pend = auth.pending_devices(session, user_id)
        auth.decide_device(session, _anchor(user_id, workspace_id), uuid.UUID(pend[0]["id"]), trust=False)
    with finance_session() as session:
        assert auth.claim_device(session, device_token) == "denied"


def test_chuzhoe_ustroystvo_ne_podtverdit(finance_db) -> None:
    """Подтвердить можно только своё устройство."""
    user_id, workspace_id = _owner_with_weak_password()
    with finance_session() as session:
        with pytest.raises(NeedsDeviceApproval):
            auth.login(session, email="owner@example.com", password="12345678", device_token=None)
    with finance_session() as session:
        pend = auth.pending_devices(session, user_id)
        stranger = _anchor(uuid.uuid4(), workspace_id)
        with pytest.raises(auth.AuthError):
            auth.decide_device(session, stranger, uuid.UUID(pend[0]["id"]), trust=True)
