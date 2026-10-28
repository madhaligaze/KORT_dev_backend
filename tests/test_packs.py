"""Пакеты: страна, отрасль, компания - этап 2, п.9 страницы «Ядро KORT».

* В коде ядра нет слов одной компании: ни имени, ни шапки её таблиц. Ядро -
  весь `app/finance` и маршруты `finance*`; миграции - данные, не ядро.
  Проверяется каждый токен: имена, строки, докстринги и комментарии.
* Страна-игрушка из теста (свой номер участника, ставка, порог, срок ЭСФ)
  меняет поведение ядра без правки ядра.
* Отрасль-игрушка засевает новую компанию своими полями, статусами и видами.
* Таблица формы первой компании читается у компании с её словарём колонок и
  не читается молча у новой: отказ называет колонку.
"""
from __future__ import annotations

import io
import re
import tokenize
import uuid
from dataclasses import replace
from datetime import date, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.finance import packs
from app.finance.packs import kz, services
from tests import company_layer

ROOT = Path(__file__).resolve().parents[1] / "app"
#: Имена первой компании и шапка её таблиц - то, чего ядро знать не должно.
WORDS = (
    "bbc", "битрикс", "bitrix", "omar",
    "сводка все юр", "осн.общая", "наша фирма", "техн. 1", "сумма факт поступ", "по выписке банка",
    "число выставления счета", "дата выстав. счета", "счет за период (коммент)", "доп. коммент.",
    "заказчик (название фирмы)", "сальдо начало (авр)", "сальдо конец (авр)", "сальдо начало (без авр)",
    "сальдо конец (без авр)", "авр (наша)", "авр (клиент принял)", "эсф (отпр.)", "сумма (часть", "дата (часть",
    "факт оплата", "продл./приостн.", "разовые юо",
)
_WORDS = re.compile("|".join(re.escape(word) for word in WORDS), re.IGNORECASE)


def _core_files() -> list[Path]:
    files = [path for path in (ROOT / "finance").rglob("*.py")]
    files += [path for path in (ROOT / "api" / "routes").glob("finance*.py")]
    return sorted(path for path in files if "__pycache__" not in path.parts)


def test_v_kode_yadra_net_slov_odnoi_kompanii() -> None:
    found = []
    for path in _core_files():
        text = path.read_text(encoding="utf-8")
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type in (tokenize.NAME, tokenize.STRING, tokenize.COMMENT) or token.type == getattr(tokenize, "FSTRING_MIDDLE", -1):
                for match in _WORDS.finditer(token.string):
                    found.append(f"{path.relative_to(ROOT.parent)}:{token.start[0]}: «{match.group(0)}»")
    assert not found, "Слова одной компании в коде ядра:\n" + "\n".join(found)


def test_proverka_slov_vidit_ih() -> None:
    """Проверка не пустая: имя в комментарии, строке и имени переменной находится."""
    sample = 'x_bbc = "Наша Фирма"  # таблица BBC\n'
    tokens = [token.string for token in tokenize.generate_tokens(io.StringIO(sample).readline)]
    assert sum(bool(_WORDS.search(token)) for token in tokens) == 3


# ── Страна ───────────────────────────────────────────────────────────────────


class NineDigits(packs.Ids):
    """Номер страны-игрушки: девять цифр, последняя - сумма первых восьми по модулю 10."""

    title = "ИНН"
    length = 9

    def checksum_ok(self, number: str) -> bool:
        return len(number) == 9 and number.isdigit() and sum(map(int, number[:8])) % 10 == int(number[8])

    def kind_of(self, number: str, name: str = "") -> str:
        return packs.ORG if number.startswith("1") else packs.PERSON

    def check(self, raw: object, name: str = "") -> packs.Checked:
        number = packs.digits(raw)
        if len(number) != 9:
            return packs.Checked(number, "", "В ИНН 9 цифр")
        if not self.checksum_ok(number):
            return packs.Checked(number, "", "ИНН не сходится")
        return packs.Checked(number, self.kind_of(number, name))

    def found_in(self, text: object) -> list[str]:
        return [item for item in re.findall(r"ИНН\s*(\d{9})", str(text or "")) if self.checksum_ok(item)]


TOY = packs.Country(
    code="XX",
    title="Игрушечная страна",
    ids=NineDigits(),
    years={2026: packs.TaxNumbers(Decimal("1000"), 500, Decimal("7"))},
    regimes={"general": "основной", "simplified": "малый бизнес"},
    vat_regimes=frozenset({"general", "simplified"}),
    vat_regime_refusal="-",
    esf_days=30,
    tz=timezone(timedelta(hours=1)),
    currency="XXX",
    currency_sign="¤",
)


@pytest.fixture
def toy_country():
    packs.register(TOY)
    yield TOY
    packs.COUNTRIES.pop("XX", None)


def test_rk_po_umolchaniyu() -> None:
    country = packs.country()
    assert country.code == "KZ" and country.ids is kz.IDS
    assert country.ids.check("0905 4000 2228").ok
    assert country.years[2026].vat_rate == Decimal("16") and country.esf_days == 15
    assert packs.country_of(type("W", (), {"country": None})()) is country


def test_strana_igrushka_menyaet_yadro_bez_pravki_yadra(toy_country) -> None:
    from app.finance.core import tax

    workspace = type("W", (), {"country": "XX", "industry": "services"})()
    ids = packs.country_of(workspace).ids
    assert ids.check("123456786").ok and not ids.check("090540002228").ok
    year = tax.country_years(packs.country_of(workspace))[2026]
    assert (year.vat_rate, year.threshold) == (Decimal("7"), Decimal("500000"))
    assert tax.regime_title("simplified", packs.country_of(workspace)) == "малый бизнес"


# ── Отрасль ──────────────────────────────────────────────────────────────────


RENTAL = replace(
    services.INDUSTRY,
    code="rental",
    title="Аренда помещений",
    fields=tuple(item for item in services.FIELDS if item.key not in ("subject", "planned_end_at"))
    + (packs.FieldDef("area", "number", "Площадь, м²", ("площадь",)),),
    statuses=(("Сдано", {"phase": "active"}), ("Свободно", {"phase": "terminated"})),
    types=(("Аренда", {"billing": "month", "economic_role": "revenue"}),),
    templates=(),
    default_views=("registry",),
)


@pytest.fixture
def rental():
    packs.register(RENTAL)
    yield RENTAL
    packs.INDUSTRIES.pop("rental", None)


# ── Через базу ───────────────────────────────────────────────────────────────


@pytest.fixture
def session(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module
    from app.finance.db import finance_session

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    with finance_session() as opened:
        yield opened


def _workspace(session, **packs_of):
    from app.finance.models import Workspace

    workspace = Workspace(slug=f"w-{uuid.uuid4().hex[:8]}", title="Новая компания", **packs_of)
    session.add(workspace)
    session.flush()
    return workspace


def test_otrasl_igrushka_zasevaet_svoe(rental, session) -> None:
    from app.finance.contracts import fields as fields_module
    from app.finance.contracts.models import EntityField, ListValue
    from app.finance.core.models import View
    import sqlalchemy as sa

    workspace = _workspace(session, industry="rental")
    fields_module.ensure_registry(session, workspace)
    keys = set(session.scalars(sa.select(EntityField.key).where(EntityField.workspace_id == workspace.id)))
    assert "area" in keys and "subject" not in keys
    statuses = set(session.scalars(sa.select(ListValue.value).where(
        ListValue.workspace_id == workspace.id, ListValue.field_key == "status")))
    assert statuses == {"Сдано", "Свободно"}
    views = list(session.scalars(sa.select(View.title).where(View.workspace_id == workspace.id)))
    assert views == ["Реестр"]


def test_strana_igrushka_v_baze(toy_country, session) -> None:
    from app.finance.contracts.service import Access, Actor
    from app.finance.core import participants, tax
    from app.finance.core.acts import Acts
    from app.finance.models import Counterparty

    workspace = _workspace(session, country="XX")
    party = Counterparty(workspace_id=workspace.id, name="ООО Игрушка", normalized_name="ооо игрушка")
    session.add(party)
    session.flush()
    access = Access(view=True, edit=True, setup=True)
    actor = Actor(user_id=None, email="test@example.com")
    with pytest.raises(participants.ParticipantError):
        participants.set_bin(session, workspace, access, actor, party.id, "090540002228")
    participants.set_bin(session, workspace, access, actor, party.id, "123456786")
    assert party.bin == "123456786" and party.kind == packs.ORG
    assert tax.year_of(session, workspace.id, 2026).vat_rate == Decimal("7")
    assert tax.years(session, workspace.id)[2026].source == "XX"
    assert Acts(session, workspace, access, None, today=date(2026, 10, 1)).esf_days == 30


# ── Слой компании ────────────────────────────────────────────────────────────


def _sheet() -> list[list[str]]:
    header = ["Мес", "Техн. 1", "Наша \nФирма", "Заказчик\n(Название Фирмы)", "№\nДоговора", "Сумма Факт\nПоступ."]
    return [header, ["10", "ОКТЯБРЬ 2026", "ТМС", "ТОО Альфа", "№1", "150 000"]]


def test_tablica_kompanii_chitaetsya_ee_slovarem() -> None:
    from app.finance.core import book

    read = book.build_book(_sheet(), title="t", worksheet="Сводная", columns=book.columns_for(company_layer.COLUMNS))
    assert [(row.own, row.customer, row.number) for row in read.rows] == [("ТМС", "ТОО Альфа", "№1")]


def test_tablica_chuzhoi_formy_ne_chitaetsya_molcha() -> None:
    from app.finance.core import book

    with pytest.raises(book.LayoutBroken) as refusal:
        book.build_book(_sheet(), title="t", worksheet="Сводная")
    assert "Наше юрлицо" in str(refusal.value)
