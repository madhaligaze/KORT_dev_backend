"""Учётки «Финансов»: регистрация компании, вход, роли, границы компаний.

Главная проверка набора — предпоследняя: **данные одной компании не видны
другой**. Всё остальное (пароли, роли, переключатель) существует ради неё.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.finance import auth, service
from app.finance.auth import AuthError
from app.finance.db import finance_session


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    yield engine


def register(email="owner@example.com", company="ТОО Ромашка", password="pass-12345"):
    with finance_session() as session:
        member, token = auth.register(
            session, email=email, password=password, company=company
        )
        return member.user_id, member.workspace_id, token


def test_registratsiya_sozdaet_kompaniyu_so_schetami(finance_db) -> None:
    """Пустая компания без счетов не даёт сделать ни одного действия."""
    _user_id, workspace_id, _token = register()
    with finance_session() as session:
        accounts = [a.name for a in service.list_accounts(session, workspace_id)]
        categories = [c.name for c in service.list_categories(session, workspace_id)]
    assert accounts == ["Банковский счёт", "Касса"]
    assert "Выручка" in categories and "Аренда" in categories


def test_vhod_po_pochte_bez_ogladki_na_registr(finance_db) -> None:
    register(email="Owner@Example.COM")
    with finance_session() as session:
        member, _token = auth.login(session, email="  owner@example.com ", password="pass-12345")
    assert member.role == "owner"
    assert member.can("accounts") and member.can("people")


def test_neverniy_parol_ne_govorit_est_li_takaya_pochta(finance_db) -> None:
    """Разные тексты на «нет почты» и «неверный пароль» выдают перебором,
    кто зарегистрирован."""
    register(email="owner@example.com")
    with finance_session() as session:
        with pytest.raises(AuthError) as wrong_password:
            auth.login(session, email="owner@example.com", password="не тот")
        with pytest.raises(AuthError) as no_user:
            auth.login(session, email="never@example.com", password="pass-12345")
    assert str(wrong_password.value) == str(no_user.value)


def test_korotkiy_parol_ne_prinimaetsya(finance_db) -> None:
    with finance_session() as session:
        with pytest.raises(AuthError) as exc:
            auth.register(session, email="a@b.kz", password="1234", company="ТОО")
    assert "короче" in str(exc.value)


def test_pochta_zanyata(finance_db) -> None:
    register(email="owner@example.com")
    with finance_session() as session:
        with pytest.raises(AuthError) as exc:
            auth.register(session, email="owner@example.com", password="pass-12345", company="Вторая")
    assert "уже зарегистрирована" in str(exc.value)


def test_sessiya_zhivet_do_vyhoda(finance_db) -> None:
    _user_id, _workspace_id, token = register()
    with finance_session() as session:
        assert auth.resolve(session, token) is not None
        auth.logout(session, token)
    with finance_session() as session:
        assert auth.resolve(session, token) is None


def test_prigłashennyy_buhgalter_ne_zavodit_scheta(finance_db) -> None:
    """Роль «бухгалтер» ведёт учёт, но счета и людей не трогает."""
    _user_id, _workspace_id, token = register()
    with finance_session() as session:
        owner = auth.resolve(session, token)
        auth.invite(
            session, owner, email="buh@example.com", role="accountant", password="pass-12345"
        )
    with finance_session() as session:
        member, _token = auth.login(session, email="buh@example.com", password="pass-12345")
    assert member.can("write") is True
    assert member.can("accounts") is False
    assert member.can("people") is False
    assert member.must_change_password is True, "временный пароль обязан требовать смены"


def test_smotryashchiy_tolko_smotrit(finance_db) -> None:
    _user_id, _workspace_id, token = register()
    with finance_session() as session:
        owner = auth.resolve(session, token)
        auth.invite(session, owner, email="view@example.com", role="viewer", password="pass-12345")
        member, _t = auth.login(session, email="view@example.com", password="pass-12345")
    assert member.can("read") is True
    assert member.can("write") is False


def test_vladeltsa_nelzya_ponizit_ili_ubrat(finance_db) -> None:
    _user_id, _workspace_id, token = register()
    with finance_session() as session:
        owner = auth.resolve(session, token)
        auth.invite(session, owner, email="admin@example.com", role="admin", password="pass-12345")
        admin, _t = auth.login(session, email="admin@example.com", password="pass-12345")
        with pytest.raises(AuthError):
            auth.change_role(session, admin, user_id=owner.user_id, role="viewer")
        with pytest.raises(AuthError):
            auth.remove_member(session, admin, user_id=owner.user_id)


def test_dve_kompanii_odnogo_cheloveka_ne_smeshivayutsya(finance_db) -> None:
    """Самая важная проверка набора: деньги одной компании не видны другой."""
    _user_id, first_id, token = register(company="Первая")
    with finance_session() as session:
        member = auth.resolve(session, token)
        created = auth.add_company(session, member, title="Вторая")
        second_id = created["id"]

    # В первой компании — поступление, во второй ничего.
    with finance_session() as session:
        workspace = service.get_workspace(session, first_id)
        cash = next(a for a in service.list_accounts(session, first_id) if a.name == "Касса")
        service.create_operation(
            session,
            workspace,
            service.OperationInput(
                kind="income",
                paid_at=date(2026, 9, 10),
                amount=Decimal("500000"),
                account_to_id=cash.id,
                comment="деньги первой компании",
            ),
        )

    import uuid as _uuid

    with finance_session() as session:
        first_ops, first_total = service.list_operations(session, first_id)
        second_ops, second_total = service.list_operations(session, _uuid.UUID(second_id))
        # Значения снимаем внутри сессии: после выхода объект от неё отвязан.
        first_comments = [op.comment for op in first_ops]
        second_comments = [op.comment for op in second_ops]
    assert first_total == 1 and second_total == 0
    assert first_comments == ["деньги первой компании"]
    assert second_comments == []


def test_pereklyuchatel_menyaet_kompaniyu_toy_zhe_sessii(finance_db) -> None:
    import uuid as _uuid

    _user_id, first_id, token = register(company="Первая")
    with finance_session() as session:
        member = auth.resolve(session, token)
        created = auth.add_company(session, member, title="Вторая")
        switched = auth.switch_company(session, token, _uuid.UUID(created["id"]))
    assert switched.workspace_title == "Вторая"
    assert switched.workspace_id != first_id

    with finance_session() as session:
        again = auth.resolve(session, token)
    assert again is not None and again.workspace_title == "Вторая", "выбор компании обязан пережить перечитывание сессии"


def test_chuzhuyu_kompaniyu_ne_vybrat(finance_db) -> None:
    import uuid as _uuid

    _first_user, _first_ws, first_token = register(email="one@example.com", company="Первая")
    _second_user, second_ws, _second_token = register(email="two@example.com", company="Чужая")
    with finance_session() as session:
        with pytest.raises(AuthError) as exc:
            auth.switch_company(session, first_token, second_ws)
    assert "не открыта" in str(exc.value)


def test_smena_parolya_trebuet_starogo(finance_db) -> None:
    _user_id, _workspace_id, token = register()
    with finance_session() as session:
        member = auth.resolve(session, token)
        with pytest.raises(AuthError):
            auth.set_password(session, member, old="не тот", new="new-password-1")
        auth.set_password(session, member, old="pass-12345", new="new-password-1")
    with finance_session() as session:
        assert auth.login(session, email="owner@example.com", password="new-password-1")
