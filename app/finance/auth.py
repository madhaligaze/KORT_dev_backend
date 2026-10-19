"""Регистрация, вход и сессии раздела «Финансы».

Своя авторизация, а не общая с дашбордом - почему, объяснено в
`accounts_model.py`. Здесь механика.

Что сделано так же, как в дашборде, и это осознанно
───────────────────────────────────────────────────
* пароли - argon2id, открытый текст в базу не попадает никогда;
* сессии серверные: в cookie уходит случайный токен, в базе лежит его SHA-256,
  поэтому дамп базы не даёт работающих сессий;
* логин (почта) нечувствителен к регистру и обрамляющим пробелам, пароль -
  чувствителен;
* ограничение перебора: пять неудачных попыток на почту подряд - минута
  ожидания. Счётчик в памяти процесса, не в базе: защита от подбора не должна
  писать в базу на каждый неверный пароль.

Что сделано иначе
─────────────────
**Регистрация открыта.** В дашборде первого админа заводят переменными
окружения, и это правильно для одной компании. Здесь компания регистрируется
сама: почта, пароль, название компании - и человек сразу владелец своей
компании со своими счетами.

**Компания выбирается в сессии.** Один человек ведёт несколько компаний;
переключатель меняет `sessions.workspace_id`, а не заводит вторую сессию.

Вход сотрудника по номеру (ревизия 0019)
────────────────────────────────────────
Два шага: номер → «пароль» или «задайте пароль». Незнакомый номер получает
«пароль», как и знакомый: вход не выдаёт, кто зарегистрирован. Задать пароль
можно только в окне ожидания (72 часа от заведения или сброса); кода и SMS
нет - это выбор пользователя, и смягчён он окном, уведомлением
администратору с устройством и адресом и сбросом, который обрывает всё.

Перебор режется по номеру (пять неудач - десять минут ожидания) и по адресу;
пятая неудача подряд уходит администраторам уведомлением «возможный
перебор». «Забыл пароль» отвечает одинаково всегда и просит не чаще раза в
десять минут на номер.

Одна проверка на все двери
──────────────────────────
Учётка, которая ждёт пароль или заблокирована, не открывает ни одного
маршрута: `resolve()` для неё возвращает `None`, как для чужого токена.
Проверка одна и стоит здесь, а не в каждом маршруте, - урок дашборда BBC,
где такой флаг проверяли по месту и однажды забыли.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy.orm import Session

from app.finance import access as access_module, passwords
from app.finance.access import Rights
from app.finance.accounts_model import FinanceMembership, FinanceSession, FinanceUser, LoginDevice
from app.finance.models import Workspace

log = logging.getLogger(__name__)

_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 8
SESSION_TTL = timedelta(days=30)
COOKIE_NAME = "fin_session"
#: Метка устройства для подтверждения слабых паролей. Живёт долго: устройство
#: подтверждают один раз. В cookie - случайный токен, в базе его SHA-256.
DEVICE_COOKIE = "fin_device"
DEVICE_TTL = timedelta(days=400)
#: Сколько «ждёт подтверждения» остаётся на виду и сколько после одобрения
#: можно забрать сеанс на ждущем устройстве.
APPROVAL_WINDOW = timedelta(minutes=30)
#: Как часто обновлять «был в сети» у сессии.
LAST_SEEN_STEP = timedelta(minutes=1)
#: Сколько ждёт учётка, пока человек задаст пароль по номеру.
PENDING_WINDOW = timedelta(hours=72)

#: Почта проверяется грубо и намеренно: сложные выражения отсекают живые
#: адреса, а подтверждение всё равно приходит письмом (когда оно появится).
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")
_NOT_DIGIT = re.compile(r"\D+")

#: Роли, которые можно выдать приглашением. `accountant`/`viewer` - прежние
#: названия: принимаются, чтобы старый экран «Команда» не сломался, и
#: превращаются в сотрудника с личными правами прежней роли.
INVITE_ROLES = ("admin", "employee", "accountant", "viewer")

#: Тексты, которые фронт показывает как есть (фронт-план, 6.8).
WRONG_PHONE_OR_PASSWORD = "Неверный номер или пароль"
TOO_MANY_PHONE = "Слишком много попыток. Попробуйте через 10 минут."
WINDOW_EXPIRED = "Время задать пароль прошло. Попросите администратора открыть его снова."
NO_PENDING = "Задать пароль по этому номеру нельзя. Войдите с паролем или попросите администратора открыть вход."
NEEDS_PASSWORD = "Пароль ещё не задан - придумайте его"


class AuthError(Exception):
    """Отказ со текстом, который можно показать человеку."""


class TooManyAttempts(AuthError):
    """Перебор: отвечаем 429, а не 401 - это не «неверный пароль»."""


class NeedsPassword(AuthError):
    """Учётка ждёт пароль, а человек на шаге «пароль»: экран должен перейти к
    «Придумайте пароль», а не отвечать «неверный пароль» на пароль, которого нет.

    Бывает, когда номер ввели раньше, чем администратор открыл вход, или после
    сброса - экран так и стоит на шаге «пароль». Тайны тут нет: то же самое
    `phone_start` говорит любому, кто введёт номер.
    """


class WindowExpired(AuthError):
    """Окно «задать пароль» прошло: отвечаем 410, а не 400 - экран по нему
    предлагает «Попросить администратора» (01.10.2026: раньше был только текст
    «попросите администратора», а просить было нечем)."""


class NeedsDeviceApproval(AuthError):
    """Слабый пароль с незнакомого устройства: пароль верен, но сеанс не
    открывается, пока вход не подтвердят с устройства, где человек уже вошёл.

    Это не отказ: пароль подошёл. Поэтому маршрут отвечает не ошибкой, а 202
    «ждём подтверждения» и ставит метку устройства, по которой ждущий экран
    потом заберёт сеанс (`device_claim`). `set_token` - новая метка, если её
    ещё не было; иначе `None` (cookie уже стоит).
    """

    def __init__(self, set_token: str | None = None) -> None:
        super().__init__("Вход с нового устройства ждёт подтверждения")
        self.set_token = set_token


#: Временный пароль - его диктуют или пересылают: без похожих знаков (0/о, 1/l).
_TEMPORARY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def temporary_password(length: int = 10) -> str:
    """Временный пароль для учётки, входящей по почте: при входе его сменят."""
    return "".join(secrets.choice(_TEMPORARY_ALPHABET) for _ in range(length))


@dataclass(frozen=True)
class Member:
    """Снимок вошедшего: кто, в какой компании и что ему можно.

    Снимок, а не живая строка ORM: объект переживает закрытие сессии базы и
    уходит в обработчик маршрута, где базы уже нет. Права (`rights`)
    посчитаны один раз на запрос в `resolve()`.
    """

    user_id: uuid.UUID
    email: str
    full_name: str
    workspace_id: uuid.UUID | None
    workspace_title: str
    role: str
    must_change_password: bool
    session_id: uuid.UUID
    phone: str = ""
    rights: Rights = field(default_factory=Rights)
    #: Откуда запрос - для журнала действий; ставит зависимость маршрута.
    ip: str = ""
    user_agent: str = ""

    def can(self, ability: str) -> bool:
        """Прежние способности (`read`, `write`, `accounts`, `people`, `company`)."""
        return ability in self.rights.abilities()

    @property
    def login(self) -> str:
        """Чем человек входит - почта или телефон. Подпись под записями."""
        return self.email or self.phone

    @property
    def display_name(self) -> str:
        return self.full_name.strip() or self.email or self.phone


# ── Ограничение перебора ─────────────────────────────────────────────────────

_ATTEMPTS: dict[str, list[float]] = {}
_ATTEMPTS_LOCK = threading.Lock()
_MAX_ATTEMPTS = 5
_WINDOW = 60.0
#: Вход по номеру ждёт дольше: «Попробуйте через 10 минут» - это обещание
#: экрана, и счётчик обязан его держать.
PHONE_WINDOW = 600.0
#: Неудач с одного адреса за окно - перебор многих номеров с одной машины.
IP_LIMIT = 30
#: Первых шагов входа по номеру с одного адреса за окно. Считается каждый
#: шаг, а не неудача: ответ «задайте пароль» сам по себе находка для того, кто
#: ищет учётки в окне ожидания. Офис за одним адресом входит раз в месяц на
#: человека, так что шестьдесят за десять минут живому входу не мешают.
START_LIMIT = 60
#: С какого размера словаря выбрасывать остывшие ключи. Перебор по списку
#: адресов - это новый ключ на каждую попытку, и назад его никто не спросит.
_SWEEP_AT = 1024


def _window_of(key: str) -> float:
    return PHONE_WINDOW if key.startswith(("phone:", "ip:", "start:")) else _WINDOW


def _too_many_attempts(key: str, limit: int = _MAX_ATTEMPTS) -> bool:
    now = time.monotonic()
    window = _window_of(key)
    with _ATTEMPTS_LOCK:
        hits = [stamp for stamp in _ATTEMPTS.get(key, []) if now - stamp < window]
        # Пустой список не храним: проверка идёт на каждом входе, и каждый
        # когда-либо введённый адрес оставался бы в словаре навсегда.
        if hits:
            _ATTEMPTS[key] = hits
        else:
            _ATTEMPTS.pop(key, None)
        return len(hits) >= limit


def _note_failure(key: str) -> int:
    """Записать неудачу. Возвращает число неудач в окне вместе с этой."""
    now = time.monotonic()
    window = _window_of(key)
    with _ATTEMPTS_LOCK:
        if len(_ATTEMPTS) >= _SWEEP_AT:
            cold = [
                k for k, hits in _ATTEMPTS.items() if all(now - s >= _window_of(k) for s in hits)
            ]
            for stale in cold:
                del _ATTEMPTS[stale]
        hits = [stamp for stamp in _ATTEMPTS.get(key, []) if now - stamp < window]
        hits.append(now)
        _ATTEMPTS[key] = hits
        return len(hits)


def _forget_failures(key: str) -> None:
    with _ATTEMPTS_LOCK:
        _ATTEMPTS.pop(key, None)


# ── Пароли, токены, логины ───────────────────────────────────────────────────


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(hashed: str | None, password: str) -> bool:
    if not hashed:
        return False
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def normalize_phone(raw: str | None) -> str:
    """`+7 (701) 123-45-67`, `87011234567`, `7011234567` → `+77011234567`.

    Казахстанский мобильный: `+7` и десять цифр, первая из них - 7. Не номер -
    отказ с текстом, который фронт покажет под полем.
    """
    digits = _NOT_DIGIT.sub("", raw or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    if len(digits) != 10:
        raise AuthError("Номер - десять цифр после +7")
    if digits[0] != "7":
        raise AuthError("Номер мобильного в Казахстане начинается с +7 7")
    return "+7" + digits


def _check_password(password: str) -> None:
    """Стойкость нового пароля (`passwords.problem`). Только при задании нового.

    Уже сохранённые пароли этой проверки не проходят и не обязаны: ломать вход
    действующим учёткам ради политики нельзя. Слабый, но уже заданный пароль
    прикрыт подтверждением устройства при входе, а не этой дверью.
    """
    trouble = passwords.problem(password or "")
    if trouble is not None:
        raise AuthError(trouble)


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _event(
    session: Session,
    workspace_id: uuid.UUID,
    kind: str,
    *,
    title: str,
    user_id: uuid.UUID | None,
    session_id: uuid.UUID | None = None,
    ip: str = "",
    user_agent: str = "",
    actor: str = "",
    entity: str = "user",
    entity_id: uuid.UUID | None = None,
    after: dict[str, Any] | None = None,
    before: dict[str, Any] | None = None,
) -> None:
    """Событие входа в журнал действий компании.

    Вход ещё не прошёл зависимость маршрута, поэтому автор, сеанс и адрес
    передаются явно, а не берутся из контекста запроса.
    """
    from app.finance import history

    history.write(
        session,
        None,  # type: ignore[arg-type] - компания задана идентификатором
        workspace_id=workspace_id,
        kind=kind,
        entity=entity,
        entity_id=entity_id,
        title=title,
        before=before,
        after=after,
        actor=actor,
        user_id=user_id,
        session_id=session_id,
        ip=ip,
        user_agent=user_agent,
    )


def _memberships(session: Session, user_id: uuid.UUID) -> list[FinanceMembership]:
    return list(
        session.scalars(
            sa.select(FinanceMembership)
            .where(FinanceMembership.user_id == user_id)
            .order_by(FinanceMembership.created_at)
        )
    )


# ── Регистрация и вход ───────────────────────────────────────────────────────


def register(
    session: Session,
    *,
    email: str,
    password: str,
    company: str,
    full_name: str = "",
    user_agent: str = "",
    ip: str = "",
) -> tuple[Member, str]:
    """Новая компания с её владельцем. Возвращает снимок и токен сессии.

    Начальные справочники компании заводит `service.create_workspace` - тот же
    код, что раньше создавал единственное пространство: пустая компания без
    счетов не даёт сделать ни одного действия.
    """
    from app.finance import people
    from app.finance import service  # локально: service импортирует auth не тут

    clean_email = normalize_email(email)
    if not _EMAIL.match(clean_email):
        raise AuthError("Это не похоже на адрес почты")
    _check_password(password)
    title = (company or "").strip()
    if len(title) < 2:
        raise AuthError("У компании должно быть название")

    exists = session.scalar(
        sa.select(FinanceUser).where(FinanceUser.email_normalized == clean_email)
    )
    if exists is not None:
        # Не «смените пароль»: своей смены пароля до входа нет, а «Забыли?»
        # у входа отправляет просьбу администраторам (01.10.2026).
        raise AuthError("Такая почта уже зарегистрирована - войдите с ней; пароль забыт - «Забыли?» у входа")

    user = FinanceUser(
        email=(email or "").strip(),
        email_normalized=clean_email,
        password_hash=hash_password(password),
        full_name=(full_name or "").strip(),
        status="active",
    )
    session.add(user)
    session.flush()

    workspace = service.create_workspace(session, title=title)
    session.add(
        FinanceMembership(workspace_id=workspace.id, user_id=user.id, role="owner")
    )
    session.flush()
    people.ensure_employee(session, workspace.id, user)

    token, record = _open_session(
        session, user=user, workspace_id=workspace.id, user_agent=user_agent, ip=ip
    )
    _event(
        session, workspace.id, "auth.register", title=f"компания «{title}» зарегистрирована",
        user_id=user.id, session_id=record.id, ip=ip, user_agent=user_agent, actor=user.email or "",
        entity_id=user.id,
    )
    log.info("finance: зарегистрирована компания «%s»", title)
    member = resolve(session, token)
    if member is None:  # pragma: no cover - сессия только что создана
        raise AuthError("Не удалось открыть сессию")
    return member, token


def _failure(
    session: Session,
    user: FinanceUser | None,
    *,
    key: str,
    ip: str,
    user_agent: str,
    login_text: str,
) -> None:
    """Неудачный вход: счётчики, событие в журнал и, на пятой, уведомление."""
    from app.finance import notifications

    count = _note_failure(key)
    if ip:
        _note_failure(f"ip:{ip}")
    if user is None:
        return
    for membership in _memberships(session, user.id):
        _event(
            session, membership.workspace_id, "auth.login_failed",
            title=f"неверный пароль · {login_text} · {count}-й подряд" if count > 1 else f"неверный пароль · {login_text}",
            # Автор неизвестен: пароль вводил кто угодно. Учётка - предмет записи.
            user_id=None, ip=ip, user_agent=user_agent, actor="", entity_id=user.id,
            after={"count": count},
        )
        if count == _MAX_ATTEMPTS:
            notifications.notify(
                session, membership.workspace_id, "login_locked",
                subject_user_id=user.id,
                payload={"count": count, "ip": ip, "user_agent": user_agent[:400], "login": login_text},
            )


def _pick_membership(session: Session, user: FinanceUser) -> FinanceMembership | None:
    """Компания для новой сессии: первая, где вход не закрыт.

    Все членства закрыты - вход закрыт целиком: человек без компании увидел бы
    экран выбора, а выбрать там нечего.
    """
    memberships = _memberships(session, user.id)
    open_ = [item for item in memberships if item.blocked_at is None]
    if memberships and not open_:
        raise AuthError("Вход заблокирован администратором")
    return open_[0] if open_ else None


def _begin(
    session: Session, user: FinanceUser, *, ip: str, user_agent: str, title: str
) -> tuple[Member, str]:
    """Открыть сеанс вошедшему: членство, запись «был в сети», событие входа."""
    membership = _pick_membership(session, user)
    user.last_login_at = _now()
    token, record = _open_session(
        session,
        user=user,
        workspace_id=membership.workspace_id if membership else None,
        user_agent=user_agent,
        ip=ip,
    )
    if membership is not None:
        _event(
            session, membership.workspace_id, "auth.login", title=title,
            user_id=user.id, session_id=record.id, ip=ip, user_agent=user_agent,
            actor=user.email or user.phone or "", entity_id=user.id,
        )
    member = resolve(session, token, ip=ip, user_agent=user_agent)
    if member is None:  # pragma: no cover - сессия только что создана
        raise AuthError("Не удалось открыть сессию")
    return member, token


def _success(
    session: Session,
    user: FinanceUser,
    *,
    key: str,
    ip: str,
    user_agent: str,
    title: str,
) -> tuple[Member, str]:
    _forget_failures(key)
    return _begin(session, user, ip=ip, user_agent=user_agent, title=title)


# ── Доверенные устройства (слабые пароли) ────────────────────────────────────


def new_device_token() -> str:
    return secrets.token_urlsafe(32)


def _device_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _gate_device(
    session: Session,
    user: FinanceUser,
    *,
    password: str,
    device_token: str | None,
    ip: str,
    user_agent: str,
) -> None:
    """Пустить ли сразу или ждать подтверждения устройства.

    Сильный пароль - пускаем всегда: знание стойкого пароля достаточно.
    Слабый, но уже заданный пароль (до политики, в том числе у владельца) -
    только с доверенного устройства; с незнакомого заводим «ждёт
    подтверждения» и поднимаем `NeedsDeviceApproval`. Запись должна пережить
    коммит, поэтому маршрут ловит исключение внутри транзакции.

    Выключено по умолчанию (`FINANCE_DEVICE_APPROVAL`): проект в разработке,
    входят с разных устройств, и барьер подтверждения мешает. Тогда вход
    открывается сразу, как при сильном пароле.
    """
    from app.finance.config import finance_settings

    if not finance_settings.device_approval:
        return
    if not passwords.is_weak(password or ""):
        return
    token = device_token or ""
    set_token: str | None = None
    if not token:
        token = new_device_token()
        set_token = token
    dhash = _device_hash(token)
    now = _now()
    row = session.scalar(
        sa.select(LoginDevice).where(
            LoginDevice.user_id == user.id, LoginDevice.device_hash == dhash
        )
    )
    if row is not None and row.status == "trusted":
        row.last_seen_at = now
        session.flush()
        return
    if row is None:
        row = LoginDevice(
            user_id=user.id,
            device_hash=dhash,
            status="pending",
            user_agent=(user_agent or "")[:400],
            ip=(ip or "")[:64],
        )
        session.add(row)
    else:
        # Было отклонено или ещё ждёт - просим снова: отклонить по ошибке
        # человек мог, и второй показ дешевле, чем тупик.
        row.status = "pending"
        row.decided_at = None
        row.user_agent = (user_agent or "")[:400]
        row.ip = (ip or "")[:64]
        row.last_seen_at = now
    session.flush()
    raise NeedsDeviceApproval(set_token)


def pending_devices(session: Session, user_id: uuid.UUID) -> list[dict[str, Any]]:
    """Входы с новых устройств, ждущие решения, - для рамы того, кто уже вошёл."""
    since = _now() - APPROVAL_WINDOW
    rows = session.scalars(
        sa.select(LoginDevice)
        .where(
            LoginDevice.user_id == user_id,
            LoginDevice.status == "pending",
            LoginDevice.created_at >= since,
        )
        .order_by(LoginDevice.created_at.desc())
    )
    return [
        {
            "id": str(row.id),
            "user_agent": row.user_agent or "",
            "ip": row.ip or "",
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


def decide_device(session: Session, member: Member, device_id: uuid.UUID, *, trust: bool) -> None:
    """Подтвердить или отклонить вход с нового устройства. Только своё устройство."""
    row = session.get(LoginDevice, device_id)
    if row is None or row.user_id != member.user_id:
        raise AuthError("Это устройство не ваше или запрос уже закрыт")
    row.status = "trusted" if trust else "denied"
    row.decided_at = _now()
    session.flush()


def claim_device(
    session: Session, device_token: str | None, *, ip: str = "", user_agent: str = ""
) -> tuple[Member, str] | str:
    """Ждущий экран забирает сеанс, когда вход подтвердили. Иначе - его состояние.

    Возвращает `(member, token)`, если устройство только что подтвердили, или
    строку состояния: `pending` (ещё ждёт), `denied` (отклонили), `none`
    (нечего забирать). Подтверждение забирается один раз: `decided_at`
    снимается, чтобы метка устройства сама по себе не открывала сеанс потом.
    """
    if not device_token:
        return "none"
    dhash = _device_hash(device_token)
    row = session.scalar(
        sa.select(LoginDevice)
        .where(LoginDevice.device_hash == dhash)
        .order_by(LoginDevice.decided_at.is_(None), LoginDevice.last_seen_at.desc())
    )
    if row is None:
        return "none"
    if row.status == "pending":
        return "pending"
    decided = _aware(row.decided_at)
    if row.status == "trusted" and decided is not None and (_now() - decided) < APPROVAL_WINDOW:
        user = session.get(FinanceUser, row.user_id)
        if user is None or user.status != "active":
            return "none"
        row.decided_at = None
        row.last_seen_at = _now()
        session.flush()
        return _begin(session, user, ip=ip, user_agent=user_agent, title="вход с нового устройства")
    if row.status == "denied" and decided is not None and (_now() - decided) < APPROVAL_WINDOW:
        return "denied"
    return "none"


def login(
    session: Session,
    *,
    email: str,
    password: str,
    user_agent: str = "",
    ip: str = "",
    device_token: str | None = None,
) -> tuple[Member, str]:
    """Вход по почте и паролю."""
    clean_email = normalize_email(email)
    if _too_many_attempts(clean_email) or (ip and _too_many_attempts(f"ip:{ip}", IP_LIMIT)):
        raise TooManyAttempts("Слишком много попыток. Подождите минуту и попробуйте снова")

    user = session.scalar(
        sa.select(FinanceUser).where(FinanceUser.email_normalized == clean_email)
    )
    # Одинаковый текст на «нет такой почты» и «неверный пароль»: разные
    # сообщения позволяют перебором узнать, кто зарегистрирован.
    if user is None or not verify_password(user.password_hash, password or ""):
        _failure(session, user, key=clean_email, ip=ip, user_agent=user_agent, login_text=clean_email)
        raise AuthError("Неверная почта или пароль")
    if user.status != "active":
        raise AuthError("Учётная запись заблокирована")
    # Пароль подошёл. Слабый, но уже заданный - только с доверенного
    # устройства; с нового поднимет NeedsDeviceApproval до открытия сеанса.
    _gate_device(session, user, password=password, device_token=device_token, ip=ip, user_agent=user_agent)
    return _success(session, user, key=clean_email, ip=ip, user_agent=user_agent, title="вход")


def phone_start(session: Session, *, phone: str, ip: str = "") -> str:
    """Первый шаг входа по номеру: `password`, `set_password` или `expired`.

    Незнакомый номер - `password`, как и знакомый: иначе по ответу перебором
    собирался бы список сотрудников. `set_password` видит только учётка,
    ждущая пароль, - ей этот шаг и нужен. `expired` - она же, но окно ожидания
    прошло (01.10.2026): раньше человек набирал новый пароль дважды и только
    потом узнавал, что задать его нельзя. Тайны в этом не больше, чем в
    `set_password`.
    """
    clean = normalize_phone(phone)
    if ip and (_too_many_attempts(f"ip:{ip}", IP_LIMIT) or _too_many_attempts(f"start:{ip}", START_LIMIT)):
        raise TooManyAttempts(TOO_MANY_PHONE)
    # Каждый шаг в счёт: без этого перебор номеров в поисках «задайте пароль»
    # не упирался ни во что - неудачей первый шаг не бывает никогда.
    if ip:
        _note_failure(f"start:{ip}")
    user = session.scalar(sa.select(FinanceUser).where(FinanceUser.phone == clean))
    if user is not None and user.status == "pending":
        until = _aware(user.pending_until)
        return "set_password" if until is not None and until > _now() else "expired"
    return "password"


def phone_login(
    session: Session,
    *,
    phone: str,
    password: str,
    user_agent: str = "",
    ip: str = "",
    device_token: str | None = None,
) -> tuple[Member, str]:
    clean = normalize_phone(phone)
    key = f"phone:{clean}"
    if _too_many_attempts(key) or (ip and _too_many_attempts(f"ip:{ip}", IP_LIMIT)):
        raise TooManyAttempts(TOO_MANY_PHONE)
    user = session.scalar(sa.select(FinanceUser).where(FinanceUser.phone == clean))
    if user is not None and user.status == "pending":
        # Пароля у учётки нет вовсе - «неверный пароль» тут ложь, из-за которой
        # человек сбрасывает то, что сбрасывать не нужно. Ответ говорит то же,
        # что первый шаг, поэтому и считается с ним вместе.
        if ip:
            if _too_many_attempts(f"start:{ip}", START_LIMIT):
                raise TooManyAttempts(TOO_MANY_PHONE)
            _note_failure(f"start:{ip}")
        until = _aware(user.pending_until)
        if until is None or until <= _now():
            raise WindowExpired(WINDOW_EXPIRED)
        raise NeedsPassword(NEEDS_PASSWORD)
    if user is None or not verify_password(user.password_hash, password or ""):
        _failure(session, user, key=key, ip=ip, user_agent=user_agent, login_text=clean)
        raise AuthError(WRONG_PHONE_OR_PASSWORD)
    if user.status != "active":
        raise AuthError("Вход заблокирован администратором")
    _gate_device(session, user, password=password, device_token=device_token, ip=ip, user_agent=user_agent)
    return _success(session, user, key=key, ip=ip, user_agent=user_agent, title="вход по номеру")


def phone_set_password(
    session: Session, *, phone: str, password: str, user_agent: str = "", ip: str = ""
) -> tuple[Member, str]:
    """Задать пароль по номеру - только учётке в окне ожидания - и войти.

    Раньше экран говорил «Пароль задан, войдите с ним» и просил тот же пароль
    в третий раз. Пароль только что набран дважды, окно ожидания проверено -
    повторный ввод ничего не охраняет. Вход идёт той же `_success`, что и
    обычный: с событием в журнале и сбросом счётчиков.
    """
    from app.finance import notifications

    clean = normalize_phone(phone)
    if ip and _too_many_attempts(f"ip:{ip}", IP_LIMIT):
        raise TooManyAttempts(TOO_MANY_PHONE)
    _check_password(password)
    user = session.scalar(sa.select(FinanceUser).where(FinanceUser.phone == clean))
    if user is None or user.status != "pending":
        # Тот же текст для незнакомого и для уже активного номера.
        if ip:
            _note_failure(f"ip:{ip}")
        raise AuthError(NO_PENDING)
    until = _aware(user.pending_until)
    if until is None or until <= _now():
        raise WindowExpired(WINDOW_EXPIRED)
    user.password_hash = hash_password(password)
    user.status = "active"
    user.pending_until = None
    user.must_change_password = False
    session.execute(sa.delete(FinanceSession).where(FinanceSession.user_id == user.id))
    session.flush()
    _forget_failures(f"phone:{clean}")
    for membership in _memberships(session, user.id):
        _event(
            session, membership.workspace_id, "auth.password_set", title="пароль задан",
            user_id=user.id, ip=ip, user_agent=user_agent, actor=clean, entity_id=user.id,
        )
        notifications.notify(
            session, membership.workspace_id, "password_set",
            subject_user_id=user.id,
            payload={"ip": ip, "user_agent": user_agent[:400]},
        )
    return _success(
        session, user, key=f"phone:{clean}", ip=ip, user_agent=user_agent, title="вход по номеру"
    )


#: «Забыл пароль» - не чаще раза в десять минут на номер.
FORGOT_EVERY = timedelta(minutes=10)


def phone_forgot(session: Session, *, phone: str, user_agent: str = "", ip: str = "") -> None:
    """Просьба сбросить пароль. Ответ маршрута одинаковый, есть номер или нет."""
    from app.finance import notifications

    clean = normalize_phone(phone)
    user = session.scalar(sa.select(FinanceUser).where(FinanceUser.phone == clean))
    if user is None or user.status == "blocked":
        return
    last = notifications.last_request_at(session, "password_reset_requested", user.id)
    if last is not None and _now() - last < FORGOT_EVERY:
        return
    for membership in _memberships(session, user.id):
        if membership.blocked_at is not None:
            continue
        _event(
            session, membership.workspace_id, "auth.reset_requested", title="запрос сброса пароля",
            user_id=None, ip=ip, user_agent=user_agent, actor="", entity_id=user.id,
        )
        notifications.notify(
            session, membership.workspace_id, "password_reset_requested",
            subject_user_id=user.id,
            payload={"ip": ip, "user_agent": user_agent[:400]},
        )


def email_forgot(session: Session, *, email: str, user_agent: str = "", ip: str = "") -> None:
    """«Забыли?» у входа по почте - просьба администраторам (01.10.2026).

    До этого просьба уходила только по номеру, а учётка, входящая по почте
    (администратор, сотрудник по приглашению на почту), могла только прочитать
    «пароль сбрасывает владелец». Ответ маршрута одинаковый, есть почта или нет.

    Владельцу просьба не пишется: его пароль сбрасывается только командой на
    сервере, и «владелец просит сбросить пароль» у администраторов было бы
    просьбой, которую им нечем выполнить.
    """
    from app.finance import notifications

    clean = normalize_email(email)
    if not _EMAIL.match(clean):
        raise AuthError("Это не похоже на адрес почты")
    user = session.scalar(sa.select(FinanceUser).where(FinanceUser.email_normalized == clean))
    if user is None or user.status == "blocked":
        return
    last = notifications.last_request_at(session, "password_reset_requested", user.id)
    if last is not None and _now() - last < FORGOT_EVERY:
        return
    for membership in _memberships(session, user.id):
        if membership.blocked_at is not None or membership.role == "owner":
            continue
        _event(
            session, membership.workspace_id, "auth.reset_requested", title="запрос сброса пароля",
            user_id=None, ip=ip, user_agent=user_agent, actor="", entity_id=user.id,
        )
        notifications.notify(
            session, membership.workspace_id, "password_reset_requested",
            subject_user_id=user.id,
            payload={"ip": ip, "user_agent": user_agent[:400]},
        )


def _open_session(
    session: Session,
    *,
    user: FinanceUser,
    workspace_id: uuid.UUID | None,
    user_agent: str,
    ip: str = "",
) -> tuple[str, FinanceSession]:
    token = secrets.token_urlsafe(32)
    record = FinanceSession(
        user_id=user.id,
        token_hash=_token_hash(token),
        workspace_id=workspace_id,
        expires_at=_now() + SESSION_TTL,
        user_agent=(user_agent or "")[:400],
        ip=(ip or "")[:64],
    )
    session.add(record)
    session.flush()
    return token, record


def resolve(
    session: Session, token: str | None, *, ip: str = "", user_agent: str = ""
) -> Member | None:
    """Токен из cookie → снимок вошедшего с правами. Просрочку удаляем сразу.

    Считается на каждом запросе, включая опрос реестра раз в две секунды,
    поэтому сеанс, учётка, компания и членство - одним запросом, а права -
    вторым и только у сотрудника (`access.load`).
    """
    if not token:
        return None
    row = session.execute(
        sa.select(FinanceSession, FinanceUser, FinanceMembership, Workspace.title)
        .join(FinanceUser, FinanceUser.id == FinanceSession.user_id)
        .outerjoin(Workspace, Workspace.id == FinanceSession.workspace_id)
        .outerjoin(
            FinanceMembership,
            sa.and_(
                FinanceMembership.workspace_id == FinanceSession.workspace_id,
                FinanceMembership.user_id == FinanceSession.user_id,
            ),
        )
        .where(FinanceSession.token_hash == _token_hash(token))
    ).first()
    if row is None:
        return None
    record, user, membership, workspace_title = row
    now = _now()
    expires = _aware(record.expires_at)
    if expires is not None and expires <= now:
        session.delete(record)
        session.flush()
        return None

    # Ждёт пароль или заблокирована - ни одного маршрута. Одна проверка на все
    # двери: сеанс такой учётки существовать не должен (сброс их обрывает), но
    # если он есть, он ничего не открывает.
    if user.status != "active":
        return None

    workspace_id = record.workspace_id
    if (
        workspace_id is None
        or workspace_title is None
        or membership is None
        or membership.blocked_at is not None
    ):
        # Из компании исключили или закрыли вход, пока сессия была открыта.
        # Не молчим и не оставляем доступ: контекст сбрасывается, человек
        # выберет другую.
        workspace_id, workspace_title, role = None, "", ""
    else:
        role = membership.role

    # «Был в сети» пишется не чаще раза в минуту. Реестр договоров опрашивает
    # сервер раз в две секунды из каждой открытой вкладки, и запись на каждый
    # запрос превращала бы чтение в поток обновлений одной строки.
    seen = _aware(record.last_seen_at)
    if seen is None or (now - seen) >= LAST_SEEN_STEP:
        record.last_seen_at = now
        if ip and record.ip != ip[:64]:
            record.ip = ip[:64]
        session.flush()

    rights = access_module.load(session, workspace_id, user.id, role) if workspace_id else Rights()
    return Member(
        user_id=user.id,
        email=user.email or "",
        full_name=user.full_name or "",
        workspace_id=workspace_id,
        workspace_title=workspace_title or "",
        role=role,
        must_change_password=bool(user.must_change_password),
        session_id=record.id,
        phone=user.phone or "",
        rights=rights,
        ip=ip,
        user_agent=user_agent,
    )


def logout(session: Session, token: str | None, *, ip: str = "", user_agent: str = "") -> None:
    if not token:
        return
    row = session.scalar(
        sa.select(FinanceSession).where(FinanceSession.token_hash == _token_hash(token))
    )
    if row is not None:
        if row.workspace_id is not None:
            user = session.get(FinanceUser, row.user_id)
            _event(
                session, row.workspace_id, "auth.logout", title="выход",
                user_id=row.user_id, session_id=row.id, ip=ip, user_agent=user_agent,
                actor=(user.email or user.phone or "") if user else "", entity_id=row.user_id,
            )
        session.delete(row)
        session.flush()


def companies_of(session: Session, user_id: uuid.UUID) -> list[dict[str, object]]:
    """Компании человека - для переключателя в шапке. Закрытые не показываются."""
    rows = session.execute(
        sa.select(Workspace.id, Workspace.title, FinanceMembership.role)
        .join(FinanceMembership, FinanceMembership.workspace_id == Workspace.id)
        .where(FinanceMembership.user_id == user_id, FinanceMembership.blocked_at.is_(None))
        .order_by(Workspace.created_at)
    )
    return [{"id": str(wid), "title": title, "role": role} for wid, title, role in rows]


def switch_company(session: Session, token: str, workspace_id: uuid.UUID) -> Member:
    """Сменить компанию в текущей сессии."""
    member = resolve(session, token)
    if member is None:
        raise AuthError("Сессия истекла")
    membership = session.scalar(
        sa.select(FinanceMembership).where(
            FinanceMembership.workspace_id == workspace_id,
            FinanceMembership.user_id == member.user_id,
        )
    )
    if membership is None or membership.blocked_at is not None:
        raise AuthError("Эта компания вам не открыта")
    row = session.get(FinanceSession, member.session_id)
    if row is None:  # pragma: no cover - resolve уже проверил
        raise AuthError("Сессия истекла")
    row.workspace_id = workspace_id
    session.flush()
    resolved = resolve(session, token)
    if resolved is None:  # pragma: no cover
        raise AuthError("Сессия истекла")
    return resolved


def add_company(session: Session, member: Member, *, title: str) -> dict[str, object]:
    """Ещё одна компания тому же человеку. Он её владелец."""
    from app.finance import people, service

    clean = (title or "").strip()
    if len(clean) < 2:
        raise AuthError("У компании должно быть название")
    workspace = service.create_workspace(session, title=clean)
    session.add(
        FinanceMembership(workspace_id=workspace.id, user_id=member.user_id, role="owner")
    )
    session.flush()
    user = session.get(FinanceUser, member.user_id)
    if user is not None:
        people.ensure_employee(session, workspace.id, user)
    _event(
        session, workspace.id, "company.create", title=f"компания «{clean}» заведена",
        user_id=member.user_id, session_id=member.session_id, ip=member.ip,
        user_agent=member.user_agent, actor=member.login, entity="workspace", entity_id=workspace.id,
    )
    return {"id": str(workspace.id), "title": workspace.title, "role": "owner"}


def invite(
    session: Session,
    member: Member,
    *,
    email: str,
    role: str,
    full_name: str = "",
    password: str,
) -> dict[str, object]:
    """Добавить человека в компанию по почте с временным паролем.

    Пароль задаёт владелец и передаёт лично; флаг `must_change_password`
    заставит сменить его при первом входе. Сотрудников по телефону заводит
    кабинет (`people.create_employee`) - там пароль человек задаёт сам.

    Старые роли `accountant`/`viewer` превращаются в сотрудника с личными
    правами прежней роли; администратора назначает только владелец.
    """
    from app.finance import access as access_mod, people

    if not member.can("people"):
        raise AuthError("Добавлять людей может владелец или администратор")
    if member.workspace_id is None:
        raise AuthError("Сначала выберите компанию")
    if role not in INVITE_ROLES:
        raise AuthError("Такой роли нет")
    if role == "admin" and member.role != "owner":
        raise AuthError("Администратора назначает владелец компании")
    _check_password(password)

    clean_email = normalize_email(email)
    if not _EMAIL.match(clean_email):
        raise AuthError("Это не похоже на адрес почты")

    user = session.scalar(
        sa.select(FinanceUser).where(FinanceUser.email_normalized == clean_email)
    )
    if user is None:
        user = FinanceUser(
            email=(email or "").strip(),
            email_normalized=clean_email,
            password_hash=hash_password(password),
            full_name=(full_name or "").strip(),
            must_change_password=True,
            status="active",
        )
        session.add(user)
        session.flush()

    existing = session.scalar(
        sa.select(FinanceMembership).where(
            FinanceMembership.workspace_id == member.workspace_id,
            FinanceMembership.user_id == user.id,
        )
    )
    if existing is not None:
        raise AuthError("Этот человек уже в компании")

    stored = "admin" if role == "admin" else "employee"
    session.add(
        FinanceMembership(
            workspace_id=member.workspace_id,
            user_id=user.id,
            role=stored,
            invited_by=member.user_id,
        )
    )
    session.flush()
    employee = people.ensure_employee(session, member.workspace_id, user)
    if role in access_mod.LEGACY_GRANTS:
        access_mod.grant_legacy(session, member.workspace_id, employee.id, role, by=member.user_id)
    _event(
        session, member.workspace_id, "people.invite",
        title=f"новый сотрудник: {employee.full_name}",
        user_id=member.user_id, session_id=member.session_id, ip=member.ip,
        user_agent=member.user_agent, actor=member.login, entity="employee", entity_id=employee.id,
        after={"role": stored, "email": user.email},
    )
    return {"id": str(user.id), "email": user.email, "role": stored}


def members_of(session: Session, workspace_id: uuid.UUID) -> list[dict[str, object]]:
    rows = session.execute(
        sa.select(
            FinanceUser.id,
            FinanceUser.email,
            FinanceUser.phone,
            FinanceUser.full_name,
            FinanceUser.status,
            FinanceUser.last_login_at,
            FinanceMembership.role,
            FinanceMembership.blocked_at,
        )
        .join(FinanceMembership, FinanceMembership.user_id == FinanceUser.id)
        .where(FinanceMembership.workspace_id == workspace_id)
        .order_by(FinanceMembership.created_at)
    )
    return [
        {
            "id": str(uid),
            "email": email or "",
            "phone": phone or "",
            "full_name": full_name or "",
            "status": "blocked" if blocked else status,
            "last_login_at": last.isoformat() if last else None,
            "role": role,
        }
        for uid, email, phone, full_name, status, last, role, blocked in rows
    ]


def change_role(session: Session, member: Member, *, user_id: uuid.UUID, role: str) -> None:
    """Роль в компании. Администраторов назначает и снимает только владелец.

    Иначе администратор мог бы поднять до себя любого сотрудника - а через
    сброс пароля и окно ожидания войти его учёткой.
    """
    from app.finance import access as access_mod, people

    if not member.can("people"):
        raise AuthError("Менять роли может владелец или администратор")
    if role not in INVITE_ROLES:
        raise AuthError("Такой роли нет")
    if user_id == member.user_id:
        raise AuthError("Свою роль менять нельзя - иначе можно остаться без прав")
    membership = session.scalar(
        sa.select(FinanceMembership).where(
            FinanceMembership.workspace_id == member.workspace_id,
            FinanceMembership.user_id == user_id,
        )
    )
    if membership is None:
        raise AuthError("Этот человек не в компании")
    if membership.role == "owner":
        raise AuthError("Владельца компании нельзя понизить")
    stored = "admin" if role == "admin" else "employee"
    if (stored == "admin" or membership.role == "admin") and member.role != "owner":
        raise AuthError("Назначать и снимать администраторов может только владелец")
    before = membership.role
    membership.role = stored
    session.flush()
    user = session.get(FinanceUser, user_id)
    employee = people.ensure_employee(session, member.workspace_id, user) if user else None
    if employee is not None and role in access_mod.LEGACY_GRANTS:
        access_mod.grant_legacy(session, member.workspace_id, employee.id, role, by=member.user_id)
    if before != stored or role in access_mod.LEGACY_GRANTS:
        _event(
            session, member.workspace_id, "people.role",
            title=f"роль: {employee.full_name if employee else user_id} - {before} → {stored}",
            user_id=member.user_id, session_id=member.session_id, ip=member.ip,
            user_agent=member.user_agent, actor=member.login,
            entity="employee", entity_id=employee.id if employee else user_id,
            after={"before": before, "after": stored},
        )


#: Кем остаётся прежний владелец после передачи.
KEEP_ROLES = ("admin", "employee")
_ROLE_WORDS = {"owner": "владелец", "admin": "администратор", "employee": "сотрудник"}


def transfer_ownership(
    session: Session, member: Member, *, user_id: uuid.UUID, password: str, keep: str = "admin"
) -> FinanceMembership:
    """Передать владение компанией другому человеку (29.09.2026).

    Исключительное право владельца: только он, только со своим паролем и
    только тому, кто уже входит в компанию сам (действующая учётка - не
    «ждёт пароль» и не заблокирована): иначе компания осталась бы без
    владельца, который может войти. Прежний владелец становится
    администратором (или сотрудником - `keep`). Вернуть владение может только
    новый владелец: у администратора этой двери нет.
    """
    from app.finance import people

    if member.role != "owner":
        raise AuthError("Передать владение может только владелец")
    if keep not in KEEP_ROLES:
        raise AuthError("Кем остаться: администратором или сотрудником")
    if user_id == member.user_id:
        raise AuthError("Выберите другого человека")
    me = session.get(FinanceUser, member.user_id)
    if me is None or not verify_password(me.password_hash, password or ""):
        raise AuthError("Пароль не подошёл")
    rows = {
        row.user_id: row
        for row in session.scalars(
            sa.select(FinanceMembership).where(
                FinanceMembership.workspace_id == member.workspace_id,
                FinanceMembership.user_id.in_([member.user_id, user_id]),
            )
        )
    }
    target, mine = rows.get(user_id), rows.get(member.user_id)
    if target is None or mine is None:
        raise AuthError("Этот человек не в компании")
    user = session.get(FinanceUser, user_id)
    if user is None or user.status != "active" or not user.password_hash:
        raise AuthError("Владельцем может стать только тот, кто уже входит сам: у этой учётки нет пароля или вход закрыт")
    target_before = target.role
    target.role = "owner"
    mine.role = keep
    session.flush()
    new_owner = people.ensure_employee(session, member.workspace_id, user)
    old_owner = people.ensure_employee(session, member.workspace_id, me)
    _event(
        session, member.workspace_id, "people.owner",
        title=f"владение передано: {old_owner.full_name} → {new_owner.full_name}; "
        f"{old_owner.full_name} теперь {_ROLE_WORDS[keep]}",
        user_id=member.user_id, session_id=member.session_id, ip=member.ip,
        user_agent=member.user_agent, actor=member.login,
        entity="employee", entity_id=new_owner.id,
        before={"owner": str(member.user_id), "new_owner_role": target_before},
        after={"owner": str(user_id), "previous_owner_role": keep},
    )
    return target


def remove_member(session: Session, member: Member, *, user_id: uuid.UUID) -> None:
    from app.finance import people

    if not member.can("people"):
        raise AuthError("Убирать людей может владелец или администратор")
    membership = session.scalar(
        sa.select(FinanceMembership).where(
            FinanceMembership.workspace_id == member.workspace_id,
            FinanceMembership.user_id == user_id,
        )
    )
    if membership is None:
        raise AuthError("Этот человек не в компании")
    if membership.role == "owner":
        raise AuthError("Владельца компании убрать нельзя")
    if membership.role == "admin" and member.role != "owner":
        raise AuthError("Администратора убирает только владелец")
    if user_id == member.user_id:
        raise AuthError("Себя из компании убрать нельзя")
    user = session.get(FinanceUser, user_id)
    employee = people.ensure_employee(session, member.workspace_id, user) if user else None
    session.delete(membership)
    end_sessions(session, user_id, workspace_id=member.workspace_id)
    session.flush()
    _event(
        session, member.workspace_id, "people.remove",
        title=f"доступ снят: {employee.full_name if employee else user_id}",
        user_id=member.user_id, session_id=member.session_id, ip=member.ip,
        user_agent=member.user_agent, actor=member.login,
        entity="employee", entity_id=employee.id if employee else user_id,
    )


def end_sessions(
    session: Session,
    user_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None = None,
    keep: uuid.UUID | None = None,
) -> int:
    """Закрыть сеансы человека: все, в одной компании, или все, кроме `keep`."""
    statement = sa.delete(FinanceSession).where(FinanceSession.user_id == user_id)
    if workspace_id is not None:
        statement = statement.where(FinanceSession.workspace_id == workspace_id)
    if keep is not None:
        statement = statement.where(FinanceSession.id != keep)
    result = session.execute(statement)
    return int(result.rowcount or 0)


def set_password(session: Session, member: Member, *, old: str, new: str) -> int:
    """Смена своего пароля. Старый спрашиваем всегда.

    Даже когда пароль временный: человек, получивший доступ к чужому открытому
    браузеру, не должен менять пароль, не зная прежнего. Остальные сеансы
    закрываются - экран говорит об этом заранее. Возвращает, сколько закрыто.
    """
    user = session.get(FinanceUser, member.user_id)
    if user is None:
        raise AuthError("Учётная запись не найдена")
    if not verify_password(user.password_hash, old or ""):
        raise AuthError("Старый пароль не подошёл")
    _check_password(new)
    if new == old:
        raise AuthError("Новый пароль совпадает со старым")
    user.password_hash = hash_password(new)
    user.must_change_password = False
    closed = end_sessions(session, user.id, keep=member.session_id)
    session.flush()
    if member.workspace_id is not None:
        _event(
            session, member.workspace_id, "auth.password_changed", title="пароль изменён",
            user_id=member.user_id, session_id=member.session_id, ip=member.ip,
            user_agent=member.user_agent, actor=member.login, entity_id=member.user_id,
            after={"sessions_closed": closed},
        )
    return closed


def _session_out(row: FinanceSession, current: uuid.UUID | None) -> dict[str, object]:
    return {
        "id": str(row.id),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "user_agent": row.user_agent or "",
        "ip": row.ip or "",
        "current": current is not None and row.id == current,
    }


def sessions_of(
    session: Session,
    user_id: uuid.UUID,
    *,
    current: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
) -> list[dict[str, object]]:
    statement = sa.select(FinanceSession).where(FinanceSession.user_id == user_id)
    if workspace_id is not None:
        statement = statement.where(FinanceSession.workspace_id == workspace_id)
    rows = session.scalars(statement.order_by(FinanceSession.last_seen_at.desc()))
    return [_session_out(row, current) for row in rows]


def revoke_session(session: Session, member: Member, session_id: uuid.UUID) -> None:
    row = session.get(FinanceSession, session_id)
    if row is None or row.user_id != member.user_id:
        raise AuthError("Такой сессии нет")
    session.delete(row)
    session.flush()
    if member.workspace_id is not None:
        _event(
            session, member.workspace_id, "auth.session_end", title="сеанс завершён",
            user_id=member.user_id, session_id=member.session_id, ip=member.ip,
            user_agent=member.user_agent, actor=member.login, entity_id=member.user_id,
            after={"session_id": str(session_id)},
        )


def set_profile(session: Session, member: Member, *, full_name: str | None = None, phone: str | None = None) -> FinanceUser:
    """Свои ФИО и телефон - владельцу и администратору.

    Сотруднику их задаёт администратор: телефон - это логин, и менять его
    себе значило бы переносить учётку на чужой номер без свидетелей.
    """
    from app.finance import people

    if member.role not in ("owner", "admin"):
        raise AuthError("ФИО и телефон сотрудника меняет администратор")
    user = session.get(FinanceUser, member.user_id)
    if user is None:
        raise AuthError("Учётная запись не найдена")
    changes: dict[str, Any] = {}
    if full_name is not None:
        clean = full_name.strip()
        if len(clean) < 2:
            raise AuthError("Укажите ФИО")
        changes["full_name"] = (user.full_name, clean)
        user.full_name = clean
    if phone is not None:
        clean_phone = normalize_phone(phone) if phone.strip() else None
        if clean_phone and clean_phone != user.phone and member.workspace_id is not None:
            try:
                people._free_phone(session, member.workspace_id, clean_phone, user)
            except people.PeopleError as exc:
                raise AuthError(str(exc)) from exc
        if clean_phone is None and not user.email_normalized:
            raise AuthError("Без телефона войти будет нечем")
        changes["phone"] = (user.phone, clean_phone)
        user.phone = clean_phone
    session.flush()
    if member.workspace_id is not None and "full_name" in changes:
        employee = people.ensure_employee(session, member.workspace_id, user)
        people.rename_employee(session, employee, user.full_name)
    if member.workspace_id is not None and changes:
        _event(
            session, member.workspace_id, "auth.profile", title="профиль изменён",
            user_id=member.user_id, session_id=member.session_id, ip=member.ip,
            user_agent=member.user_agent, actor=member.login, entity_id=member.user_id,
            after={key: value[1] for key, value in changes.items()},
        )
    return user


def set_email(session: Session, member: Member, *, email: str, password: str) -> FinanceUser:
    """Сменить свою почту - логин входа (29.09.2026: «одну почту поставили, и
    её никак не изменить»).

    Пароль спрашиваем всегда: почта - это логин, и человек у чужого открытого
    браузера не должен переносить учётку на свой адрес. Сеансы не закрываются:
    пароль прежний, поменялось только имя для входа.

    Владелец, зарегистрированный без ФИО, заведён сотрудником под своей
    почтой (`people.ensure_employee`), - такая запись получает новую почту,
    иначе в списках и истории стояла бы прежняя.
    """
    from app.finance.contracts.models import Employee
    from app.finance import people

    user = session.get(FinanceUser, member.user_id)
    if user is None:
        raise AuthError("Учётная запись не найдена")
    if not verify_password(user.password_hash, password or ""):
        raise AuthError("Пароль не подошёл")
    clean = normalize_email(email)
    if not _EMAIL.match(clean):
        raise AuthError("Это не похоже на адрес почты")
    if clean == (user.email_normalized or ""):
        raise AuthError("Это и есть ваша почта")
    taken = session.scalar(
        sa.select(FinanceUser.id).where(FinanceUser.email_normalized == clean, FinanceUser.id != user.id)
    )
    if taken is not None:
        raise AuthError("Эта почта уже занята другой учётной записью")
    before = user.email or ""
    user.email, user.email_normalized = email.strip(), clean
    session.flush()
    if before:
        for employee in session.scalars(
            sa.select(Employee).where(Employee.user_id == user.id, Employee.full_name == before)
        ):
            try:
                people.rename_employee(session, employee, user.email)
            except people.PeopleError:
                pass
    if member.workspace_id is not None:
        _event(
            session, member.workspace_id, "auth.email", title=f"почта для входа изменена: {before or '-'} → {user.email}",
            user_id=member.user_id, session_id=member.session_id, ip=member.ip,
            user_agent=member.user_agent, actor=user.email, entity_id=member.user_id,
            before={"email": before}, after={"email": user.email},
        )
    return user


__all__ = [
    "COOKIE_NAME",
    "DEVICE_COOKIE",
    "DEVICE_TTL",
    "INVITE_ROLES",
    "MIN_PASSWORD_LENGTH",
    "PENDING_WINDOW",
    "AuthError",
    "Member",
    "NeedsDeviceApproval",
    "TooManyAttempts",
    "WindowExpired",
    "add_company",
    "change_role",
    "claim_device",
    "companies_of",
    "decide_device",
    "email_forgot",
    "end_sessions",
    "hash_password",
    "invite",
    "login",
    "logout",
    "new_device_token",
    "pending_devices",
    "members_of",
    "normalize_email",
    "normalize_phone",
    "phone_forgot",
    "phone_login",
    "phone_set_password",
    "phone_start",
    "register",
    "remove_member",
    "resolve",
    "revoke_session",
    "sessions_of",
    "set_password",
    "set_email",
    "transfer_ownership",
    "set_profile",
    "switch_company",
    "temporary_password",
    "verify_password",
]
