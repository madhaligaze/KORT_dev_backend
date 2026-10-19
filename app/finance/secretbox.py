"""Секреты подключений, которыми KORT ходит в чужие системы (пароль REST-сервиса 1С).

Почему не хеш, как у токена приёма
──────────────────────────────────
Токен приёма (`integrations.py`) KORT только проверяет: хеш сравнивают с
присланным, и знать сам токен базе незачем. Пароль 1С наоборот: им KORT
входит в базу клиента, и без открытого текста не войти. Поэтому он хранится
зашифрованным ключом из окружения (`FINANCE_SECRETS_KEY`): копия базы KORT
без ключа пароля не даёт, а ключ лежит там же, где остальные секреты сервиса.

Ключа нет - пароль не сохраняется вовсе, с понятным отказом, а не кладётся
открытым «пока не настроят»: такие «пока» не убирает никто.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.finance.config import finance_settings


class SecretError(Exception):
    """Секрет нельзя зашифровать или прочитать - с текстом для человека."""


def ready() -> bool:
    return bool((finance_settings.secrets_key or "").strip())


def _fernet() -> Fernet:
    raw = (finance_settings.secrets_key or "").strip()
    if not raw:
        raise SecretError(
            "На сервере не задан ключ FINANCE_SECRETS_KEY - пароль подключения сохранить нечем"
        )
    # Ключ окружения - любая строка; Fernet нужен ровно 32 байта в base64.
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest()))


def seal(text: str) -> str:
    return _fernet().encrypt(text.encode("utf-8")).decode("ascii")


def unseal(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise SecretError(
            "Пароль подключения не расшифровывается: ключ FINANCE_SECRETS_KEY сменили - впишите пароль заново"
        ) from exc


__all__ = ["SecretError", "ready", "seal", "unseal"]
