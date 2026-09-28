"""Доступ «Финансов» по HTTP: cookie → зависимость → ответ.

Права проверяются цепочкой запросов, а не юнитом (урок дашборда BBC: сборка,
типы и линтер дыр в правах не ловят, экраны выглядят рабочими). Набор держит
обещания плана:

* каждый маршрут `/finance/*` объявляет свой раздел — обход падает на
  необъявленном;
* учётка, ждущая пароль, не открывает ни одного маршрута; окно истекло —
  пароль не задать; незнакомый и активный номер отвечают одинаково;
* сброс убивает сеансы и старый пароль; пятая неудача — уведомление;
* без права на раздел — 403 и на чтении; договоры чужого отдела не приходят
  ни списком, ни по id; скрытое поле не приходит нигде;
* администратор не сбрасывает владельца и других администраторов; пароль
  владельца — только командой сервера;
* `resolve()` остаётся дешёвым: запросов к базе на опрос — единицы.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.finance import audit, auth
from app.finance.db import finance_session

BASE = "/api/v1/finance"
PASSWORD = "pass-12345"


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.core.config import settings
    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    # Cookie без `Secure`: TestClient ходит по http.
    monkeypatch.setattr(settings, "environment", "test")
    auth._ATTEMPTS.clear()
    audit._VIEWS.clear()
    yield engine
    auth._ATTEMPTS.clear()
    audit._VIEWS.clear()


@pytest.fixture
def app(finance_db) -> FastAPI:
    from finance_routes import finance_app

    return finance_app()


def client(app: FastAPI, ip: str = "10.0.0.7") -> TestClient:
    return TestClient(app, headers={"x-client-ip": ip, "user-agent": "pytest-agent"})


def register(app: FastAPI, email: str = "owner@bbc.kz") -> TestClient:
    owner = client(app)
    response = owner.post(
        f"{BASE}/auth/register",
        json={"email": email, "password": PASSWORD, "company": "BBC (тест)", "full_name": "Ермеков Нурболат"},
    )
    assert response.status_code == 201, response.text
    return owner


def department(owner: TestClient, code: str) -> str:
    response = owner.post(f"{BASE}/people/departments", json={"code": code, "title": code})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def employee(owner: TestClient, name: str, phone: str, department_id: str | None = None, role: str | None = None) -> dict:
    body = {"full_name": name, "phone": phone, "access": True}
    if department_id:
        body["department_id"] = department_id
    if role:
        body["role"] = role
    response = owner.post(f"{BASE}/people/employees", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def activate(app: FastAPI, phone: str, password: str = "secret-123", ip: str = "10.0.0.9") -> TestClient:
    person = client(app, ip)
    assert person.post(f"{BASE}/auth/phone/start", json={"phone": phone}).json() == {"step": "set_password"}
    done = person.post(f"{BASE}/auth/phone/set-password", json={"phone": phone, "password": password})
    assert done.status_code == 200, done.text
    login = person.post(f"{BASE}/auth/phone/login", json={"phone": phone, "password": password})
    assert login.status_code == 200, login.text
    return person


def grant(owner: TestClient, kind: str, subject_id: str, changes: dict) -> dict:
    response = owner.put(f"{BASE}/access/{kind}/{subject_id}", json={"changes": changes})
    assert response.status_code == 200, response.text
    return response.json()


# ── Каждый маршрут объявляет раздел ─────────────────────────────────────────

#: Без входа: вход, регистрация, «кто я», приём от подключений по токену.
PUBLIC = {
    ("POST", "/auth/register"),
    ("POST", "/auth/login"),
    ("POST", "/auth/logout"),
    ("GET", "/auth/me"),
    ("POST", "/auth/phone/start"),
    ("POST", "/auth/phone/login"),
    ("POST", "/auth/phone/set-password"),
    ("POST", "/auth/phone/forgot"),
    ("POST", "/integrations/inbox"),
}
#: Своё: не требует права раздела. Список закрыт — новый маршрут «для себя»
#: добавляется сюда осознанно, а не просто взятием `current_member`.
SELF = {
    ("POST", "/auth/switch"),
    ("POST", "/auth/companies"),
    ("POST", "/auth/password"),
    ("GET", "/auth/sessions"),
    ("DELETE", "/auth/sessions/{session_id}"),
    ("POST", "/auth/sessions/end-others"),
    # Без права «Журнал действий» — только свои события; с ним — вся компания.
    ("GET", "/audit"),
    # Сигнал «открыт раздел» проверяет право на сам раздел внутри.
    ("POST", "/audit/view"),
}
#: Изменяющий метод, который ничего не меняет, — ему хватает «видит».
READONLY_POSTS = {
    ("POST", "/contracts/{contract_id}/amendments/parse"),
    ("POST", "/contracts/setup/views/preview"),
}
#: Меняет только свой экран, а не данные компании: личный вид листа (ширины,
#: цвета) настраивает и тот, кому лист открыт только на чтение.
PERSONAL_WRITES = {
    ("PUT", "/looks/{key}"),
    ("DELETE", "/looks/{key}"),
}


def _declarations(dependant) -> list[tuple[str, object]]:
    found = []
    for dependency in dependant.dependencies:
        marker = getattr(dependency.call, "__finance_access__", None)
        if marker is not None:
            found.append(marker)
        found.extend(_declarations(dependency))
    return found


def test_kazhdyy_marshrut_finansov_obyavlyaet_razdel(app: FastAPI) -> None:
    """Обход всех маршрутов раздела: необъявленный — падение.

    Правило плана: GET требует «видит», изменение — «правит»; «своё» и
    открытые без входа — только из закрытых списков выше.
    """
    from finance_routes import api_routes

    problems: list[str] = []
    seen_self: set[tuple[str, str]] = set()
    seen_public: set[tuple[str, str]] = set()
    routes = [(full, route) for full, route in api_routes() if full.startswith(BASE)]
    # Пустой обход — не «всё объявлено», а слепой тест (FastAPI 0.141).
    assert routes, "обход не нашёл ни одного маршрута раздела"
    for full, route in routes:
        path = full[len(BASE):]
        for method in route.methods:
            key = (method, path)
            marks = _declarations(route.dependant)
            if not marks:
                if key in PUBLIC:
                    seen_public.add(key)
                else:
                    problems.append(f"{method} {path}: раздел не объявлен")
                continue
            if key in PUBLIC:
                problems.append(f"{method} {path}: в списке открытых, но закрыт — уберите из PUBLIC")
            for kind, value in marks:
                if kind == "self":
                    if key not in SELF:
                        problems.append(f"{method} {path}: «своё» без права раздела — не в списке SELF")
                    seen_self.add(key)
                elif kind == "resource":
                    _resources, level = value
                    if method == "GET" and level != "view":
                        problems.append(f"{method} {path}: чтение требует «{level}»")
                    if method != "GET" and level != "edit" and key not in READONLY_POSTS | PERSONAL_WRITES:
                        problems.append(f"{method} {path}: изменение объявлено с «{level}», нужно «edit»")
    assert not problems, "\n".join(problems)
    assert seen_self == SELF, f"в SELF лишнее: {sorted(SELF - seen_self)}"
    assert seen_public == PUBLIC, f"в PUBLIC лишнее: {sorted(PUBLIC - seen_public)}"


# ── Вход по номеру ───────────────────────────────────────────────────────────


def test_vhod_po_nomeru_ot_zavedeniya_do_vhoda(app: FastAPI) -> None:
    owner = register(app)
    yuo = department(owner, "ЮО")
    card = employee(owner, "Сейтова Айдана", "+7 (702) 555-01-22", yuo)
    assert card["status"] == "pending" and card["phone"] == "+77025550122"
    assert card["account"]["pending_until"]

    person = activate(app, "87025550122")
    me = person.get(f"{BASE}/auth/me").json()
    assert me["role"] == "employee"
    assert me["user"]["phone"] == "+77025550122"
    assert me["employee"]["department"]["code"] == "ЮО"
    assert set(me["access"].values()) == {"none"}, "нет записи — нет доступа"
    assert me["pending_requests"] == 0

    # Администратор видит «пароль задан» с устройством и адресом — это сведение, не просьба.
    notes = owner.get(f"{BASE}/notifications").json()
    item = next(note for note in notes["items"] if note["kind"] == "password_set")
    assert item["actionable"] is False
    assert item["payload"]["ip"] == "10.0.0.9" and item["payload"]["user_agent"] == "pytest-agent"
    assert notes["pending"] == 0

    # Задать пароль второй раз нельзя: окно закрылось вместе с первым.
    again = client(app).post(f"{BASE}/auth/phone/set-password", json={"phone": "+77025550122", "password": "other-123"})
    assert again.status_code == 400
    assert client(app).post(f"{BASE}/auth/phone/start", json={"phone": "+77025550122"}).json() == {"step": "password"}


def test_nomer_proveryaetsya_kak_kazahstanskiy_mobilnyy(app: FastAPI) -> None:
    guest = client(app)
    short = guest.post(f"{BASE}/auth/phone/start", json={"phone": "+7 701 123"})
    assert short.status_code == 400 and "десять цифр" in short.json()["detail"]
    city = guest.post(f"{BASE}/auth/phone/start", json={"phone": "+7 (495) 123-45-67"})
    assert city.status_code == 400 and "+7 7" in city.json()["detail"]


def test_neznakomyy_i_aktivnyy_nomer_otvechayut_odinakovo(app: FastAPI) -> None:
    owner = register(app)
    employee(owner, "Ким Алия", "+77072223355")
    activate(app, "+77072223355")
    guest = client(app, "10.0.0.20")

    def answers(phone: str) -> list[tuple[int, object]]:
        return [
            (r.status_code, r.json())
            for r in (
                guest.post(f"{BASE}/auth/phone/start", json={"phone": phone}),
                guest.post(f"{BASE}/auth/phone/set-password", json={"phone": phone, "password": "whatever-1"}),
                guest.post(f"{BASE}/auth/phone/login", json={"phone": phone, "password": "wrong-pass"}),
                guest.post(f"{BASE}/auth/phone/forgot", json={"phone": phone}),
            )
        ]

    assert answers("+77072223355") == answers("+77000000000")


def test_okno_isteklo_parol_ne_zadat(app: FastAPI) -> None:
    from app.finance.accounts_model import FinanceUser

    owner = register(app)
    employee(owner, "Ахметов Ерлан", "+77014445566")
    with finance_session() as session:
        user = session.scalar(sa.select(FinanceUser).where(FinanceUser.phone == "+77014445566"))
        user.pending_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    response = client(app).post(f"{BASE}/auth/phone/set-password", json={"phone": "+77014445566", "password": "secret-123"})
    assert response.status_code == 400
    assert response.json()["detail"] == auth.WINDOW_EXPIRED
    listed = owner.get(f"{BASE}/people/employees").json()["items"]
    assert next(item for item in listed if item["phone"] == "+77014445566")["status"] == "pending_expired"


def test_sbros_ubivaet_seansy_i_staryy_parol(app: FastAPI) -> None:
    owner = register(app)
    card = employee(owner, "Тулегенов Ержан", "+77009998877")
    grant(owner, "employee", card["id"], {"journal": "view"})
    person = activate(app, "+77009998877")
    assert person.get(f"{BASE}/operations").status_code == 200

    # Просьба «забыл пароль» — в «Ждут решения»; сброс закрывает её сам.
    client(app).post(f"{BASE}/auth/phone/forgot", json={"phone": "+77009998877"})
    assert owner.get(f"{BASE}/auth/me").json()["pending_requests"] == 1

    reset = owner.post(f"{BASE}/people/employees/{card['id']}/reset")
    assert reset.status_code == 200, reset.text
    assert reset.json()["status"] == "pending"
    assert owner.get(f"{BASE}/auth/me").json()["pending_requests"] == 0

    # Учётка, ждущая пароль, не открывает ни одного маршрута.
    assert person.get(f"{BASE}/operations").status_code == 401
    assert person.get(f"{BASE}/auth/me").json() == {"authenticated": False}
    # Старый пароль не действует — и ответ не «неверный пароль», а «задайте»:
    # пароля у учётки нет вовсе, экран уводит к «Придумайте пароль».
    old = client(app).post(f"{BASE}/auth/phone/login", json={"phone": "+77009998877", "password": "secret-123"})
    assert old.status_code == 409 and old.json()["detail"] == auth.NEEDS_PASSWORD
    assert client(app).post(f"{BASE}/auth/phone/start", json={"phone": "+77009998877"}).json()["step"] == "set_password"


def test_pyataya_neudacha_uvedomlyaet_a_shestaya_zhdyot(app: FastAPI) -> None:
    owner = register(app)
    card = employee(owner, "Жумабекова Дана", "+77050001144")
    activate(app, "+77050001144")
    guest = client(app, "10.0.0.66")
    for _ in range(5):
        wrong = guest.post(f"{BASE}/auth/phone/login", json={"phone": "+77050001144", "password": "не тот"})
        assert wrong.status_code == 401
    locked = guest.post(f"{BASE}/auth/phone/login", json={"phone": "+77050001144", "password": "secret-123"})
    assert locked.status_code == 429 and locked.json()["detail"] == auth.TOO_MANY_PHONE

    notes = owner.get(f"{BASE}/notifications").json()
    lock = next(note for note in notes["items"] if note["kind"] == "login_locked")
    assert lock["actionable"] is True and lock["payload"]["count"] == 5
    assert lock["subject"]["employee_id"] == card["id"]
    assert notes["pending"] == 1

    # В журнале — пять неудач об этой учётке, автор неизвестен.
    failures = owner.get(
        f"{BASE}/audit", params={"category": "auth", "kind": "auth.login_failed", "employee_id": card["id"]}
    ).json()["items"]
    assert len(failures) == 5
    assert all(item["actor"] is None and item["ip"] == "10.0.0.66" for item in failures)

    resolved = owner.post(f"{BASE}/notifications/{lock['id']}/resolve")
    assert resolved.status_code == 200 and resolved.json()["pending"] == 0


def test_zabyl_parol_ne_chashe_raza_v_10_minut(app: FastAPI) -> None:
    owner = register(app)
    employee(owner, "Сагинтаев Марат", "+77011112233")
    guest = client(app)
    for _ in range(3):
        assert guest.post(f"{BASE}/auth/phone/forgot", json={"phone": "+77011112233"}).json() == {"ok": True}
    notes = [note for note in owner.get(f"{BASE}/notifications").json()["items"] if note["kind"] == "password_reset_requested"]
    assert len(notes) == 1 and "repeats" not in notes[0]["payload"]


# ── Права разделов ───────────────────────────────────────────────────────────


def test_bez_prava_na_razdel_403_i_na_chtenii(app: FastAPI) -> None:
    owner = register(app)
    card = employee(owner, "Ким Алия", "+77072223355")
    grant(owner, "employee", card["id"], {"journal": "view"})
    person = activate(app, "+77072223355")

    assert person.get(f"{BASE}/operations").status_code == 200
    denied = person.post(
        f"{BASE}/operations",
        json={"kind": "income", "paid_at": "2026-09-01", "amount": "1000"},
    )
    assert denied.status_code == 403 and "только смотреть" in denied.json()["detail"]
    for path in ("/reports/debts", "/contracts", "/people", "/notifications", "/grid", "/rules"):
        assert person.get(f"{BASE}{path}").status_code == 403, path
    me = person.get(f"{BASE}/auth/me").json()
    assert me["access"]["journal"] == "view" and me["access"]["contracts"] == "none"
    assert me["abilities"] == ["read"]


def test_svodka_ostatkov_svoim_pravom(app: FastAPI) -> None:
    """Журнал и таблица не открывают остатков: ни сводкой, ни начальным остатком.

    Раньше сводку отдавали любому с журналом, и кассир видел в колонке, сколько
    денег на каждом счёте компании.
    """
    owner = register(app)
    card = employee(owner, "Нурланова Асель", "+77071112233")
    grant(owner, "employee", card["id"], {"journal": "edit", "table": "edit"})
    person = activate(app, "+77071112233")

    assert person.get(f"{BASE}/auth/me").json()["access"]["reports.summary"] == "none"
    denied = person.get(f"{BASE}/overview")
    assert denied.status_code == 403 and "Остатки и долги" in denied.json()["detail"]
    # Формам журнала счета нужны — по имени, без остатка.
    accounts = person.get(f"{BASE}/dictionaries").json()["accounts"]
    assert accounts and all(item["starting_balance"] is None for item in accounts)

    grant(owner, "employee", card["id"], {"reports.summary": "view"})
    assert person.get(f"{BASE}/overview").status_code == 200
    accounts = person.get(f"{BASE}/dictionaries").json()["accounts"]
    assert all(item["starting_balance"] is not None for item in accounts)
    # Сводка — отчёт: правки у неё нет.
    wrong = owner.put(f"{BASE}/access/employee/{card['id']}", json={"changes": {"reports.summary": "edit"}})
    assert wrong.status_code == 400
    # Владельцу запись не нужна.
    assert owner.get(f"{BASE}/overview").status_code == 200


def test_lichnoe_poverh_otdelskogo(app: FastAPI) -> None:
    owner = register(app)
    obo = department(owner, "ОБО")
    card = employee(owner, "Петрова Наталья", "+77013334455", obo)
    grant(owner, "department", obo, {"journal": "edit", "reports.debts": "view"})
    person = activate(app, "+77013334455")
    assert person.get(f"{BASE}/reports/debts").status_code == 200

    # Лично «нет» на отчёт перекрывает отдел; журнал остаётся от отдела.
    view = grant(owner, "employee", card["id"], {"reports.debts": "none"})
    assert view["effective"]["reports.debts"] == "none" and view["effective"]["journal"] == "edit"
    assert view["department_grants"]["journal"]["level"] == "edit"
    assert person.get(f"{BASE}/reports/debts").status_code == 403

    # «Как у отдела» — личная запись снимается.
    back = grant(owner, "employee", card["id"], {"reports.debts": None})
    assert "reports.debts" not in back["grants"] and back["effective"]["reports.debts"] == "view"
    assert person.get(f"{BASE}/reports/debts").status_code == 200

    # У отчётов нет правки: записать «правит» нельзя.
    wrong = owner.put(f"{BASE}/access/department/{obo}", json={"changes": {"reports.debts": "edit"}})
    assert wrong.status_code == 400

    # Каждое изменение прав — в журнале администрирования.
    titles = [item["title"] for item in owner.get(f"{BASE}/audit", params={"category": "admin"}).json()["items"]]
    assert any("права отдела ОБО" in title and "Журнал" in title for title in titles)


def test_dogovory_chuzhogo_otdela_i_skrytoe_pole(app: FastAPI) -> None:
    owner = register(app)
    yuo = department(owner, "ЮО")
    department(owner, "ОБО")

    def contract(number: str, dept: str) -> str:
        response = owner.post(
            f"{BASE}/contracts",
            json={"values": {"executor": "BBC", "customer": f"ТОО {number}", "number": number,
                             "department": dept, "amount": "500000"}},
        )
        assert response.status_code == 201, response.text
        return response.json()["contract"]["id"]

    mine, foreign = contract("ЮО/141", "ЮО"), contract("ОБО/7", "ОБО")
    card = employee(owner, "Юрист Первый", "+77021112233", yuo)
    grant(owner, "department", yuo, {
        "contracts": {"level": "edit", "scope": {"rows": "department"}},
        "contracts.field.amount": "none",
    })
    lawyer = activate(app, "+77021112233")

    listing = lawyer.get(f"{BASE}/contracts").json()
    assert [item["id"] for item in listing["contracts"]] == [mine]
    assert "amount" not in listing["contracts"][0]["values"]
    foreign_answer = lawyer.get(f"{BASE}/contracts/{foreign}")
    missing_answer = lawyer.get(f"{BASE}/contracts/{uuid.uuid4()}")
    assert foreign_answer.status_code == missing_answer.status_code == 404
    assert foreign_answer.json() == missing_answer.json()

    card_view = lawyer.get(f"{BASE}/contracts/{mine}").json()
    assert "amount" not in card_view["contract"]["values"]
    schema = lawyer.get(f"{BASE}/contracts/schema").json()
    assert "amount" not in [field["key"] for field in schema["fields"]]
    changes = lawyer.get(f"{BASE}/contracts/changes", params={"since": 0}).json()
    assert [item["id"] for item in changes["contracts"]] == [mine]
    assert all("amount" not in item["values"] for item in changes["contracts"])
    edit = lawyer.patch(f"{BASE}/contracts/{mine}", json={"values": {"amount": "1"}})
    assert edit.status_code == 403
    assert lawyer.patch(f"{BASE}/contracts/{mine}", json={"values": {"note": "звонили"}}).status_code == 200

    scope = lawyer.get(f"{BASE}/auth/me").json()["contracts_scope"]
    assert scope["rows"] == "department" and scope["fields"] == {"amount": "none"}
    assert card["id"] == scope["employee_id"]


def test_zavedyonnyy_dogovor_ostayotsya_v_svoey_oblasti(app: FastAPI) -> None:
    """Договор из первого поля не пропадает у автора с узкой областью строк."""
    owner = register(app)
    yuo = department(owner, "ЮО")
    own_card = employee(owner, "Ответственный Один", "+77021110001")
    grant(owner, "employee", own_card["id"], {"contracts": {"level": "edit", "scope": {"rows": "own"}}})
    dept_card = employee(owner, "Отделов Два", "+77021110002", yuo)
    grant(owner, "employee", dept_card["id"], {"contracts": {"level": "edit", "scope": {"rows": "department"}}})

    for phone, key, expected in (
        ("+77021110001", "people", [own_card["id"]]),
        ("+77021110002", "department", yuo),
    ):
        person = activate(app, phone)
        made = person.post(f"{BASE}/contracts", json={"values": {"number": f"Н-{phone[-2:]}"}})
        assert made.status_code == 201, made.text
        contract = made.json()["contract"]
        assert contract["values"][key] == expected, contract["values"]
        # Следующая правка того же договора — не «Договор не найден».
        again = person.patch(f"{BASE}/contracts/{contract['id']}", json={"values": {"note": "первый звонок"}})
        assert again.status_code == 200, again.text
        assert [item["id"] for item in person.get(f"{BASE}/contracts").json()["contracts"]] == [contract["id"]]


# ── Кто кого сбрасывает ─────────────────────────────────────────────────────


def test_admin_ne_sbrasyvaet_vladeltsa_i_drugogo_admina(app: FastAPI) -> None:
    owner = register(app)
    first = employee(owner, "Админ Первый", "+77030000001", role="admin")
    second = employee(owner, "Админ Второй", "+77030000002", role="admin")
    worker = employee(owner, "Сотрудник Простой", "+77030000003")
    admin = activate(app, "+77030000001")

    people = admin.get(f"{BASE}/people/employees").json()["items"]
    owner_card = next(item for item in people if item["account"] and item["account"]["role"] == "owner")
    to_owner = admin.post(f"{BASE}/people/employees/{owner_card['id']}/reset")
    assert to_owner.status_code == 403
    assert admin.post(f"{BASE}/people/employees/{second['id']}/reset").status_code == 403
    assert admin.post(f"{BASE}/people/employees/{second['id']}/block").status_code == 403
    assert admin.post(f"{BASE}/people/employees/{worker['id']}/reset").status_code == 200

    # Администратора назначает владелец, не администратор.
    promote = admin.patch(f"{BASE}/people/employees/{worker['id']}", json={"role": "admin"})
    assert promote.status_code == 403
    # Владелец сбрасывает администратора; себя — нет, это делает команда сервера.
    assert owner.post(f"{BASE}/people/employees/{first['id']}/reset").status_code == 200
    own = owner.post(f"{BASE}/people/employees/{owner_card['id']}/reset")
    assert own.status_code == 403 and "командой на сервере" in own.json()["detail"]


def test_parol_vladeltsa_sbrasyvaet_komanda_servera(app: FastAPI) -> None:
    from app.finance import cli

    owner = register(app)
    temporary = cli.reset_password("owner@bbc.kz")
    assert owner.get(f"{BASE}/auth/me").json() == {"authenticated": False}, "сеансы закрыты"
    again = client(app)
    login = again.post(f"{BASE}/auth/login", json={"email": "owner@bbc.kz", "password": temporary})
    assert login.status_code == 200 and login.json()["user"]["must_change_password"] is True
    # Временный пароль открывает только смену пароля.
    assert again.get(f"{BASE}/operations").status_code == 403
    changed = again.post(f"{BASE}/auth/password", json={"old_password": temporary, "new_password": "fresh-pass-1"})
    assert changed.status_code == 200
    assert again.get(f"{BASE}/operations").status_code == 200


def test_vremennyy_parol_otkryvaet_tolko_smenu_parolya(app: FastAPI) -> None:
    owner = register(app)
    invited = owner.post(
        f"{BASE}/auth/members",
        json={"email": "buh@bbc.kz", "password": "temp-pass-1", "role": "accountant", "full_name": "Бухгалтер Б"},
    )
    assert invited.status_code == 201, invited.text
    buh = client(app)
    assert buh.post(f"{BASE}/auth/login", json={"email": "buh@bbc.kz", "password": "temp-pass-1"}).status_code == 200
    for path in ("/operations", "/contracts", "/reports/debts", "/audit"):
        assert buh.get(f"{BASE}{path}").status_code == 403, path
    changed = buh.post(f"{BASE}/auth/password", json={"old_password": "temp-pass-1", "new_password": "own-pass-12"})
    assert changed.status_code == 200
    # Прежняя роль «бухгалтер» — это личные права: журнал правит, счета не заводит.
    me = buh.get(f"{BASE}/auth/me").json()
    assert me["role"] == "employee" and me["access"]["journal"] == "edit"
    assert me["access"]["dictionaries"] == "view" and me["access"]["people"] == "none"
    assert buh.post(f"{BASE}/accounts", json={"name": "Новый счёт"}).status_code == 403


def test_blokirovka_zakryvaet_vhod_v_etu_kompaniyu(app: FastAPI) -> None:
    owner = register(app)
    card = employee(owner, "Сейтова Айдана", "+77025550122")
    grant(owner, "employee", card["id"], {"journal": "view"})
    person = activate(app, "+77025550122")
    blocked = owner.post(f"{BASE}/people/employees/{card['id']}/block")
    assert blocked.status_code == 200 and blocked.json()["status"] == "blocked"
    assert person.get(f"{BASE}/operations").status_code == 401
    refused = client(app).post(f"{BASE}/auth/phone/login", json={"phone": "+77025550122", "password": "secret-123"})
    assert refused.status_code == 401 and "заблокирован" in refused.json()["detail"]
    assert owner.post(f"{BASE}/people/employees/{card['id']}/unblock").status_code == 200
    again = client(app).post(f"{BASE}/auth/phone/login", json={"phone": "+77025550122", "password": "secret-123"})
    assert again.status_code == 200


def test_chuzhoy_sotrudnik_otvechaet_kak_nesushchestvuyushchiy(app: FastAPI) -> None:
    first = register(app, "one@bbc.kz")
    card = employee(first, "Чужой Человек", "+77040000001")
    second = register(app, "two@bbc.kz")
    for path in (f"/people/employees/{card['id']}", f"/people/employees/{card['id']}/sessions"):
        foreign, missing = second.get(f"{BASE}{path}"), second.get(f"{BASE}{path.replace(card['id'], str(uuid.uuid4()))}")
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()
    assert second.post(f"{BASE}/people/employees/{card['id']}/reset").status_code == 404
    taken = second.post(f"{BASE}/people/employees", json={"full_name": "Другой", "phone": "+77040000001", "access": True})
    assert taken.status_code == 400 and "другой компании" in taken.json()["detail"]


# ── Подключение сотрудника: случай «Асхата» 26.09 ─────────────────────────────


def test_nomer_bez_galochki_otkryvaet_vhod_a_ne_propadaet(app: FastAPI) -> None:
    owner = register(app)
    made = owner.post(f"{BASE}/people/employees", json={"full_name": "Асхат", "phone": "+7 747 456 86 61"})
    assert made.status_code == 201, made.text
    assert made.json()["status"] == "pending" and made.json()["phone"] == "+77474568661"
    refused = owner.post(
        f"{BASE}/people/employees", json={"full_name": "Дана", "phone": "+77470000001", "access": False}
    )
    assert refused.status_code == 400 and "вместе с доступом" in refused.json()["detail"]
    assert not any(e["full_name"] == "Дана" for e in owner.get(f"{BASE}/people").json()["employees"])


def test_vhod_do_otkrytiya_dostupa_ne_zastrevaet_na_parole(app: FastAPI) -> None:
    owner = register(app)
    card = owner.post(f"{BASE}/people/employees", json={"full_name": "Асхат"}).json()
    person = client(app, "10.0.0.31")
    # Номер ещё ничей — «пароль», как у любого незнакомого.
    assert person.post(f"{BASE}/auth/phone/start", json={"phone": "+77474568661"}).json() == {"step": "password"}
    opened = owner.post(f"{BASE}/people/employees/{card['id']}/account", json={"phone": "+77474568661"})
    assert opened.status_code == 201, opened.text
    # Человек так и стоит на шаге «пароль»: ответ ведёт к «Придумайте пароль».
    stuck = person.post(f"{BASE}/auth/phone/login", json={"phone": "+77474568661", "password": "что-то"})
    assert stuck.status_code == 409 and stuck.json()["detail"] == auth.NEEDS_PASSWORD
    # Задал пароль — и уже внутри, без третьего ввода.
    done = person.post(f"{BASE}/auth/phone/set-password", json={"phone": "+77474568661", "password": "secret-123"})
    assert done.status_code == 200, done.text
    assert done.json()["authenticated"] is True and done.json()["role"] == "employee"
    assert person.get(f"{BASE}/auth/me").json()["authenticated"] is True
    kinds = [item["kind"] for item in owner.get(f"{BASE}/audit", params={"category": "auth"}).json()["items"]]
    assert "auth.login" in kinds and "auth.password_set" in kinds


def test_pervyy_shag_vhoda_schitaetsya_s_adresa(app: FastAPI) -> None:
    guest = client(app, "10.0.0.77")
    for index in range(auth.START_LIMIT):
        answer = guest.post(f"{BASE}/auth/phone/start", json={"phone": f"+7700{index:07d}"})
        assert answer.status_code == 200, (index, answer.text)
    over = guest.post(f"{BASE}/auth/phone/start", json={"phone": "+77009999999"})
    assert over.status_code == 429
    # Другой адрес — свой счёт.
    assert client(app, "10.0.0.78").post(f"{BASE}/auth/phone/start", json={"phone": "+77009999999"}).status_code == 200


def test_nomer_zakrytoy_uchyotki_svoboden(app: FastAPI) -> None:
    owner = register(app)
    first = employee(owner, "Ошибкин Ошибка", "+77011230001")
    closed = owner.patch(f"{BASE}/people/employees/{first['id']}", json={"access": False})
    assert closed.status_code == 200 and closed.json()["status"] == "no_access"
    second = owner.post(f"{BASE}/people/employees", json={"full_name": "Правильный Человек", "phone": "+77011230001"})
    assert second.status_code == 201, second.text
    assert second.json()["phone"] == "+77011230001"
    # Номер действующего сотрудника — по-прежнему отказ, и правдивый.
    third = owner.post(f"{BASE}/people/employees", json={"full_name": "Третий", "phone": "+77011230001"})
    assert third.status_code == 400 and "другого сотрудника компании" in third.json()["detail"]


def test_povtornoe_otkrytie_vhoda_zhdyot_novyy_parol(app: FastAPI) -> None:
    owner = register(app)
    card = employee(owner, "Возвратов Ерлан", "+77011230002")
    activate(app, "+77011230002")
    assert owner.patch(f"{BASE}/people/employees/{card['id']}", json={"access": False}).status_code == 200
    reopened = owner.post(f"{BASE}/people/employees/{card['id']}/account", json={"phone": "+77011230002"})
    assert reopened.status_code == 201, reopened.text
    row = owner.get(f"{BASE}/people/employees/{card['id']}").json()
    assert row["status"] == "pending" and row["account"]["pending_until"]
    old = client(app).post(f"{BASE}/auth/phone/login", json={"phone": "+77011230002", "password": "secret-123"})
    assert old.status_code == 409


def test_tyozka_iz_arhiva_vozvrashchaetsya(app: FastAPI) -> None:
    owner = register(app)
    card = employee(owner, "Асхат", "+77011230003")
    assert owner.delete(f"{BASE}/people/employees/{card['id']}").status_code == 200
    again = owner.post(f"{BASE}/people/employees", json={"full_name": "асхат", "job_title": "поддержка"})
    assert again.status_code == 201, again.text
    assert again.json()["id"] == card["id"] and again.json()["archived"] is False
    assert again.json()["job_title"] == "поддержка"
    assert again.json()["full_name"] == "асхат"


def test_otdel_s_lyudmi_ne_uhodit_v_arhiv(app: FastAPI) -> None:
    owner = register(app)
    yuo = department(owner, "ЮО")
    employee(owner, "Юристова Айгерим", "+77011230004", yuo)
    refused = owner.patch(f"{BASE}/people/departments/{yuo}", json={"archived": True})
    assert refused.status_code == 400 and "сотрудников: 1" in refused.json()["detail"]
    empty = department(owner, "ПУСТ")
    assert owner.patch(f"{BASE}/people/departments/{empty}", json={"archived": True}).status_code == 200


def test_prava_chuzhim_klyuchom_otkaz_a_ne_tishina(app: FastAPI) -> None:
    owner = register(app)
    card = employee(owner, "Правов Пётр", "+77011230005")
    wrong = owner.put(f"{BASE}/access/employee/{card['id']}", json={"contracts": {"level": "view"}})
    assert wrong.status_code == 422


def test_registraciya_bez_imeni_otkaz(app: FastAPI) -> None:
    response = client(app).post(
        f"{BASE}/auth/register", json={"email": "a@bbc.kz", "password": PASSWORD, "company": "BBC", "full_name": " "}
    )
    assert response.status_code == 400 and "имя" in response.json()["detail"]


# ── Журнал действий ─────────────────────────────────────────────────────────


def test_prosmotry_ne_chashe_raza_v_minutu_i_svoi_deystviya(app: FastAPI) -> None:
    owner = register(app)
    card = employee(owner, "Ким Алия", "+77072223355")
    grant(owner, "employee", card["id"], {"reports.debts": "view"})
    person = activate(app, "+77072223355")

    assert person.post(f"{BASE}/audit/view", json={"section": "reports.debts"}).json() == {"recorded": True}
    assert person.post(f"{BASE}/audit/view", json={"section": "reports.debts"}).json() == {"recorded": False}
    assert person.post(f"{BASE}/audit/view", json={"section": "journal"}).status_code == 403
    assert person.post(f"{BASE}/audit/view", json={"section": "нечто"}).status_code == 422

    # Без права «Журнал действий» — только своё, чужие фильтры не действуют.
    mine = person.get(f"{BASE}/audit", params={"user_id": str(uuid.uuid4())}).json()["items"]
    me = person.get(f"{BASE}/auth/me").json()["user"]["id"]
    assert mine and all(
        (item["actor"] and item["actor"]["user_id"] == me) or item["entity_id"] == me for item in mine
    )
    assert any(item["title"] == "открыт раздел «Долги»" for item in mine)

    views = owner.get(f"{BASE}/audit", params={"category": "view"}).json()["items"]
    assert [item["title"] for item in views] == ["открыт раздел «Долги»"]
    assert views[0]["actor"]["name"] == "Ким Алия" and views[0]["ip"] == "10.0.0.9"


def test_lenta_listaetsya_kursorom(app: FastAPI) -> None:
    owner = register(app)
    for code in ("ЮО", "ОБО", "НО", "HR", "ФО"):
        department(owner, code)
    first = owner.get(f"{BASE}/audit", params={"category": "admin", "limit": 2}).json()
    second = owner.get(f"{BASE}/audit", params={"category": "admin", "limit": 2, "cursor": first["next_cursor"]}).json()
    third = owner.get(f"{BASE}/audit", params={"category": "admin", "limit": 2, "cursor": second["next_cursor"]}).json()
    titles = [item["title"] for page in (first, second, third) for item in page["items"]]
    assert titles == [f"новый отдел: {code} · {code}" for code in ("ФО", "HR", "НО", "ОБО", "ЮО")]
    assert third["next_cursor"] is None and first["next"] == first["next_cursor"]


def test_prosmotry_starshe_180_dney_uhodyat_ostalnoe_ostayotsya(app: FastAPI) -> None:
    from app.finance.models import ActionLog

    owner = register(app)
    owner.post(f"{BASE}/audit/view", json={"section": "journal"})
    old = datetime.now(timezone.utc) - timedelta(days=181)
    with finance_session() as session:
        session.execute(sa.update(ActionLog).values(at=old))
    with finance_session() as session:
        assert audit.purge_views(session) == 1
    with finance_session() as session:
        left = session.scalars(sa.select(ActionLog.category)).all()
    assert "view" not in left and "auth" in left, "входы и регистрация хранятся всегда"


# ── Дешевизна resolve() ─────────────────────────────────────────────────────


def test_resolve_na_opros_stoit_edinits_zaprosov(app: FastAPI, finance_db) -> None:
    """Опрос реестра раз в 2 с на каждой вкладке — resolve не должен расти.

    Владелец: один запрос (сеанс, учётка, компания, членство разом).
    Сотрудник: два (ещё запись сотрудника с обеими стопками прав).
    """
    owner = register(app)
    yuo = department(owner, "ЮО")
    card = employee(owner, "Ким Алия", "+77072223355", yuo)
    grant(owner, "department", yuo, {"journal": "view", "contracts": "view"})
    grant(owner, "employee", card["id"], {"reports.debts": "view"})
    person = activate(app, "+77072223355")

    statements: list[str] = []

    def count(conn, cursor, statement, *args):  # noqa: ANN001
        statements.append(statement)

    for who, expected in ((owner, 1), (person, 2)):
        token = who.cookies.get(auth.COOKIE_NAME)
        with finance_session() as session:
            auth.resolve(session, token)  # первый — может обновить «был в сети»
        statements.clear()
        sa.event.listen(finance_db, "before_cursor_execute", count)
        try:
            with finance_session() as session:
                member = auth.resolve(session, token)
        finally:
            sa.event.remove(finance_db, "before_cursor_execute", count)
        assert member is not None
        assert len(statements) == expected, statements
    assert member.rights.level("reports.debts") == "view" and member.rights.level("journal") == "view"
