"""Команды сервера для «Финансов».

    python -m app.finance.cli reset-password <почта или телефон> [--password ПАРОЛЬ]

Зачем команда, а не кнопка
──────────────────────────
Пароль владельца через API не сбрасывает никто: иначе администратор сбросил
бы владельца, сам задал бы новый пароль в окне ожидания и стал бы
владельцем. Остаётся тот, у кого есть доступ к серверу и базе, - он и так
может всё.

Без `--password` команда печатает временный пароль: при входе его попросят
сменить (`must_change_password`). Все сеансы учётки закрываются, в журнал
действий каждой её компании пишется событие «сброс пароля командой
сервера».
"""
from __future__ import annotations

import argparse
import secrets
import sys

import sqlalchemy as sa


def _find_user(session, login: str):
    from app.finance import auth
    from app.finance.accounts_model import FinanceUser

    login = (login or "").strip()
    if "@" in login:
        return session.scalar(
            sa.select(FinanceUser).where(FinanceUser.email_normalized == auth.normalize_email(login))
        )
    return session.scalar(sa.select(FinanceUser).where(FinanceUser.phone == auth.normalize_phone(login)))


def reset_password(login: str, password: str | None = None) -> str:
    """Сбросить пароль учётки. Возвращает пароль, с которым теперь входить."""
    from app.finance import auth, history
    from app.finance.accounts_model import FinanceMembership
    from app.finance.db import finance_session

    with finance_session() as session:
        user = _find_user(session, login)
        if user is None:
            raise SystemExit(f"Учётки «{login}» нет")
        temporary = password is None
        new_password = password or secrets.token_urlsafe(9)
        if len(new_password) < auth.MIN_PASSWORD_LENGTH:
            raise SystemExit(f"Пароль короче {auth.MIN_PASSWORD_LENGTH} символов")
        user.password_hash = auth.hash_password(new_password)
        user.status = "active"
        user.pending_until = None
        user.must_change_password = temporary
        closed = auth.end_sessions(session, user.id)
        for workspace_id in session.scalars(
            sa.select(FinanceMembership.workspace_id).where(FinanceMembership.user_id == user.id)
        ):
            history.write(
                session, None, workspace_id=workspace_id,  # type: ignore[arg-type]
                kind="people.reset_cli", entity="user", entity_id=user.id,
                title="сброс пароля командой сервера", actor="команда сервера",
                after={"sessions_closed": closed, "temporary": temporary},
            )
        if user.phone:
            auth._forget_failures(f"phone:{user.phone}")
        if user.email_normalized:
            auth._forget_failures(user.email_normalized)
        return new_password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.finance.cli", description="Команды сервера KORT")
    commands = parser.add_subparsers(dest="command", required=True)
    reset = commands.add_parser("reset-password", help="сбросить пароль учётки (в том числе владельца)")
    reset.add_argument("login", help="почта или телефон учётки")
    reset.add_argument("--password", help="новый пароль; без него - временный, со сменой при входе")
    args = parser.parse_args(argv)
    if args.command == "reset-password":
        new_password = reset_password(args.login, args.password)
        if args.password:
            print("Пароль задан. Все сеансы учётки закрыты.")
        else:
            print(f"Временный пароль: {new_password}")
            print("При входе его попросят сменить. Все сеансы учётки закрыты.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
