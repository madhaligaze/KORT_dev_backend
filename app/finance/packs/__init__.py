"""Пакеты: страна, отрасль, компания.

Ядро знает смыслы: участник, договор, факт, начисление, долг, порог, вид.
Чем смысл наполнен, приходит из пакета:

* страна - номер участника (у РК - БИН/ИИН с контрольной цифрой), налоговые
  числа года (расчётный показатель, порог НДС в нём, ставка), режимы, срок
  электронного счёта-фактуры, пояс и валюта (`kz.py`);
* отрасль - поля реестра с подписями и написаниями шапок, статусы, виды
  договоров, смыслы, шаблоны видов (`services.py` - «услуги по договорам»);
* компания - её данные в базе: свои поля, словарь колонок её таблиц, слова
  таблицы, виды. В коде у компании пакета нет, и ядро не знает её по имени.

Какие пакеты у компании - `workspaces.country` и `workspaces.industry`
(миграция 0043). Пакеты страны и отрасли - код без базы: ядро спрашивает их
через `country_of` / `industry_of` и ничего о стране и отрасли у себя не
держит. Второй пакет (тест, другая страна) добавляется `register`, без правки
ядра.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import tzinfo
from decimal import Decimal
from typing import Any, Mapping

# ── Номер участника ──────────────────────────────────────────────────────────

#: Вид лица по номеру: организация, человек, индивидуальный предприниматель.
ORG = "org"
PERSON = "person"
IP = "ip"
KINDS = (ORG, PERSON, IP)

_DIGITS = re.compile(r"\D+")


def digits(text: object) -> str:
    """Только цифры: «0905 4000 2228» → «090540002228»."""
    return _DIGITS.sub("", str(text or ""))


@dataclass(frozen=True)
class Checked:
    """Итог проверки: `number` - очищенный номер, `error` - пусто, если номер годен."""

    number: str
    kind: str
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class Ids:
    """Номер участника страны: как пишется, как проверяется, вид лица по нему.

    Номер не угадывается: неверный - отказ, а не «наверное, опечатка».
    """

    #: Как номер называют люди: «БИН/ИИН».
    title: str = ""
    length: int = 0

    def digits(self, text: object) -> str:
        return digits(text)

    def checksum_ok(self, number: str) -> bool:
        raise NotImplementedError

    def kind_of(self, number: str, name: str = "") -> str:
        raise NotImplementedError

    def check(self, raw: object, name: str = "") -> Checked:
        raise NotImplementedError

    def found_in(self, text: object) -> list[str]:
        """Номера, подписанные в свободном тексте, с верной проверкой, по порядку."""
        raise NotImplementedError


# ── Страна ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TaxNumbers:
    """Налоговые числа года по закону страны: показатель, порог НДС в показателях, ставка."""

    unit: Decimal
    threshold_units: int
    vat_rate: Decimal


@dataclass(frozen=True)
class Country:
    code: str
    title: str
    ids: Ids
    #: Год → числа по закону. Год за пределами известных - прогноз ядра (`core/tax.py`).
    years: Mapping[int, TaxNumbers]
    #: Режим налогообложения → подпись; ключи - смыслы ядра (`core/models.TAX_REGIMES`).
    regimes: Mapping[str, str]
    #: Режимы, на которых бывают плательщиком НДС.
    vat_regimes: frozenset[str]
    #: Почему на другом режиме плательщиком НДС не бывают - отказ словами.
    vat_regime_refusal: str
    #: Срок выписки электронного счёта-фактуры после даты оборота, календарных дней.
    esf_days: int
    tz: tzinfo
    currency: str
    currency_sign: str


# ── Отрасль ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FieldDef:
    """Системное поле: ключ, тип, подпись по умолчанию и написания шапки."""

    key: str
    type: str
    title: str
    names: tuple[str, ...] = ()
    #: Поле только для чтения везде: «как было в файле».
    readonly: bool = False
    #: Спрятано по умолчанию (есть, но в листе и карточке не показывается).
    hidden: bool = False
    #: Способ заполнения по умолчанию (`contracts/fields.FILLS`); пусто - у типа его нет.
    fill: str = ""


@dataclass(frozen=True)
class Template:
    """Шаблон вида: из него компания или отдел сохраняет себе вид (`core/views.py`)."""

    key: str
    title: str
    source: str
    note: str
    forms: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    measures: tuple[str, ...] = ()
    params: Mapping[str, Any] = field(default_factory=dict)
    #: Нужен отдел-владелец: вид отдела, а не компании.
    department: bool = False


@dataclass(frozen=True)
class Industry:
    code: str
    title: str
    #: Поля реестра в порядке листа; ключи - смыслы ядра.
    fields: tuple[FieldDef, ...]
    #: Статусы: подпись → смысл (`phase`).
    statuses: tuple[tuple[str, dict[str, Any]], ...]
    #: Виды договоров: подпись → как начислять, смысл, подписи сторон.
    types: tuple[tuple[str, dict[str, Any]], ...]
    #: Хозяйственные смыслы: подпись → системный смысл.
    economic_roles: tuple[tuple[str, str], ...]
    #: Смысл предмета по словам в названии - подстановка с пометкой, правит человек.
    subject_hints: tuple[tuple[re.Pattern[str], dict[str, Any]], ...]
    #: Шаблоны видов отрасли сверх видов ядра.
    templates: tuple[Template, ...]
    #: Виды новой компании - ключи шаблонов ядра и отрасли.
    default_views: tuple[str, ...]
    #: Общие написания колонок таблиц «договор за месяц»: ключ ядра → написания.
    month_names: Mapping[str, tuple[str, ...]]


# ── Реестр пакетов ───────────────────────────────────────────────────────────

COUNTRIES: dict[str, Country] = {}
INDUSTRIES: dict[str, Industry] = {}
DEFAULT_COUNTRY = "KZ"
DEFAULT_INDUSTRY = "services"


def register(pack: Country | Industry) -> None:
    """Добавить пакет страны или отрасли (второй пакет, тест)."""
    if isinstance(pack, Country):
        COUNTRIES[pack.code] = pack
    else:
        INDUSTRIES[pack.code] = pack


def country(code: str | None = None) -> Country:
    return COUNTRIES.get(code or DEFAULT_COUNTRY) or COUNTRIES[DEFAULT_COUNTRY]


def industry(code: str | None = None) -> Industry:
    return INDUSTRIES.get(code or DEFAULT_INDUSTRY) or INDUSTRIES[DEFAULT_INDUSTRY]


def country_of(workspace: Any) -> Country:
    """Страна компании; у пространства без записи - страна по умолчанию."""
    return country(getattr(workspace, "country", None))


def industry_of(workspace: Any) -> Industry:
    return industry(getattr(workspace, "industry", None))


def out(workspace: Any) -> dict[str, Any]:
    """Пакеты компании словами - для «описать» API модели."""
    land, trade = country_of(workspace), industry_of(workspace)
    return {
        "country": {"code": land.code, "title": land.title, "ids": land.ids.title, "currency": land.currency},
        "industry": {"code": trade.code, "title": trade.title},
    }


from app.finance.packs import kz, services  # noqa: E402  - пакеты регистрируются при загрузке

register(kz.COUNTRY)
register(services.INDUSTRY)

__all__ = [
    "COUNTRIES",
    "Checked",
    "Country",
    "DEFAULT_COUNTRY",
    "DEFAULT_INDUSTRY",
    "FieldDef",
    "INDUSTRIES",
    "IP",
    "Ids",
    "Industry",
    "KINDS",
    "ORG",
    "PERSON",
    "TaxNumbers",
    "Template",
    "country",
    "country_of",
    "digits",
    "industry",
    "industry_of",
    "out",
    "register",
]
