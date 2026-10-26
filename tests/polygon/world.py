"""Правда полигона: группа «Тумар» - кто кому что должен на каждый день.

Группа другого уклада, чем BBC: клининг и техобслуживание (ТОО «Тумар
Сервис», плательщик НДС с 01.03.2026, своя 1С в интернете), аренда офисов
(ТОО «Тумар Офис» и новое ТОО «Тумар Плюс», одна 1С на компьютере
бухгалтера), учебный центр (ИП Сейткали, без 1С, всё в Excel).

Здесь только правда. Как её видят 1С, Excel и банк - `sources.py`; что в
этих источниках неоднозначно - `cases.py`. Особые договоры помечены кодом
случая из каталога; остальные - фон: обычные клиенты с обычными задержками.

Правила начислений те же, что у ядра KORT (`core/accruals.py`): месяц
начисления - месяц начала периода, периоды с первого числа; пауза и
окончание - по дате начала периода; разовый договор начисляет всю сумму в
месяце начала. Долг на день = начисления, чей период начался до этого дня, -
оплаты по договору до этого дня (возврат клиенту - оплата со знаком минус).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from polygon.cases import BY_ID

#: «Сегодня» полигона: на этот день сверяются долги.
TODAY = date(2026, 10, 25)
#: С этого дня KORT ведёт оплаты; долг до него - входящее сальдо.
SINCE = date(2026, 6, 1)
#: С этого дня в 1С «Тумар Сервис» читаются документы.
YEAR = date(2026, 1, 1)

MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")
#: Отделы и люди группы.
CLEANING, SERVICE, RENT, SCHOOL = "Клининг", "Техобслуживание", "Аренда", "Учебный центр"
STAFF = {
    CLEANING: ("Ержан Кабдулов", "Динара Сапарова"),
    SERVICE: ("Тимур Ибраев",),
    RENT: ("Асель Нурпеисова", "Марат Оразов"),
    SCHOOL: ("Айгерим Сейткали",),
}
SUBJECT = {
    CLEANING: "Услуги по уборке помещений",
    SERVICE: "Техническое обслуживание инженерных систем",
    RENT: "Аренда нежилого помещения",
    SCHOOL: "Обучение",
}


# ── Даты и деньги ────────────────────────────────────────────────────────────


def first(day: date) -> date:
    return day.replace(day=1)


def add_months(day: date, n: int) -> date:
    years, month = divmod(day.month - 1 + n, 12)
    return date(day.year + years, month + 1, 1)


def month_end(day: date) -> date:
    return add_months(day, 1) - timedelta(days=1)


def month_range(start: date, last: date) -> list[date]:
    out, month = [], first(start)
    while month <= first(last):
        out.append(month)
        month = add_months(month, 1)
    return out


def month_word(month: date) -> str:
    return f"{MONTHS[month.month - 1]} {month.year}"


def tenge(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal(1))


# ── БИН, ИИН, IBAN ───────────────────────────────────────────────────────────

_W1 = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
_W2 = (3, 4, 5, 6, 7, 8, 9, 10, 11, 1, 2)


def control(body: str) -> int | None:
    """Контрольная цифра БИН/ИИН по одиннадцати первым; `None` - такой номер не выдают."""
    values = [int(ch) for ch in body]
    digit = sum(v * w for v, w in zip(values, _W1)) % 11
    if digit == 10:
        digit = sum(v * w for v, w in zip(values, _W2)) % 11
        if digit == 10:
            return None
    return digit


def broken(number: str) -> str:
    """Тот же номер с неверной контрольной цифрой - опечатка, какую делают люди."""
    return number[:11] + str((int(number[11]) + 1) % 10)


class Numbers:
    """Выдаёт годные и неповторяющиеся БИН, ИИН и IBAN."""

    def __init__(self, rng: random.Random):
        self.rng, self.used = rng, set()

    def _take(self, make) -> str:
        while True:
            value = make()
            if value and value not in self.used:
                self.used.add(value)
                return value

    def bin(self, year: int, month: int) -> str:
        def make() -> str:
            body = f"{year % 100:02d}{month:02d}4{self.rng.choice('0124')}{self.rng.randrange(100000):05d}"
            digit = control(body)
            return "" if digit is None else body + str(digit)

        return self._take(make)

    def iin(self, born: date, female: bool) -> str:
        century = (5 if born.year >= 2000 else 3) + (1 if female else 0)

        def make() -> str:
            body = f"{born:%y%m%d}{century}{self.rng.randrange(10000):04d}"
            digit = control(body)
            return "" if digit is None else body + str(digit)

        return self._take(make)

    def iban(self, bank: str = "722") -> str:
        def make() -> str:
            bban = bank + ("S" if bank == "722" else "A") + f"{self.rng.randrange(10**12):012d}"
            numeric = "".join(str(int(ch, 36)) for ch in bban + "KZ00")
            return f"KZ{98 - int(numeric) % 97:02d}{bban}"

        return self._take(make)


# ── Сущности ─────────────────────────────────────────────────────────────────


@dataclass
class Entity:
    """Наше юрлицо. `onec`: online - 1С в интернете (OData), files - 1С на компьютере, none - без 1С."""

    key: str
    name: str
    short: str
    number: str
    iban: str
    kind: str = "org"
    created: date = date(2010, 1, 1)
    regime: str = "general"
    vat_from: date | None = None
    onec: str = "none"


@dataclass
class Party:
    """Вторая сторона: клиент, поставщик, слушатель - и как её пишет каждый источник."""

    key: str
    number: str
    kind: str
    name: str
    onec: str = ""
    registry: str = ""
    bank: str = ""
    registry_number: str = ""
    bank_number: str | None = None
    onec_duplicate: str = ""
    renamed: tuple[date, str, str] | None = None
    iban: str = ""
    cases: tuple[str, ...] = ()

    def bank_name_on(self, day: date) -> str:
        if self.renamed and day >= self.renamed[0]:
            return self.renamed[2]
        return self.bank

    @property
    def onec_name(self) -> str:
        """Имя в 1С: справочник один, поэтому после переименования - новое для всех документов."""
        return self.renamed[1] if self.renamed else self.onec


@dataclass
class Contract:
    key: str
    entity: str
    party: str
    number: str
    signed: date
    start: date
    amounts: list[tuple[date, Decimal]]
    department: str = ""
    staff: tuple[tuple[str, int], ...] = ()
    billing: str = "month"
    end: date | None = None
    executors: list[tuple[date, str]] = field(default_factory=list)
    pauses: list[tuple[date, date]] = field(default_factory=list)
    utilities: dict[date, Decimal] = field(default_factory=dict)
    side: str = "sale"
    subject: str = ""
    premises: str = ""
    deposit: Decimal = Decimal(0)
    in_registry: bool = True
    registry_number: str = ""
    registry_amount: Any = None
    registry_status: str = "действует"
    registry_executor: str = ""
    registry_note: str = ""
    onec_number: str | None = None
    cases: tuple[str, ...] = ()

    def amount_on(self, day: date) -> Decimal:
        value = self.amounts[0][1]
        for since, amount in self.amounts:
            if since <= day:
                value = amount
        return value

    def executor_on(self, day: date) -> str:
        value = self.entity
        for since, code in self.executors:
            if since <= day:
                value = code
        return value

    def paused(self, day: date) -> bool:
        return any(a <= day <= b for a, b in self.pauses)


@dataclass(frozen=True)
class Accrual:
    contract: str
    month: date
    amount: Decimal
    fixed: Decimal
    utilities: Decimal
    executor: str


@dataclass
class Document:
    """Документ 1С нашего юрлица: счёт (`invoice`) или АВР (`act`). `amount_1c` - как записан в 1С."""

    key: str
    kind: str
    entity: str
    contract: str
    month: date
    number: str
    day: date
    amount: Decimal
    amount_1c: Decimal
    content: str
    signed: date | None = None
    esf: date | None = None


@dataclass
class Payment:
    """Движение денег. Наше юрлицо - на той стороне, где ключ юрлица; выписка показывает его там.

    `alloc` - правда: договор, месяц (None - сальдо или аванс без месяца), сумма.
    """

    key: str
    day: date
    src: str
    dst: str
    amount: Decimal
    purpose: str
    kind: str = "contract"
    alloc: list[tuple[str, date | None, Decimal]] = field(default_factory=list)
    payer_name: str = ""
    payer_number: str | None = None
    cases: tuple[str, ...] = ()


@dataclass
class World:
    entities: dict[str, Entity]
    parties: dict[str, Party]
    contracts: dict[str, Contract]
    payments: list[Payment]
    documents: list[Document]
    phantoms: list[dict[str, Any]]

    # ── начисления и долги ──

    def accruals(self, key: str) -> list[Accrual]:
        contract = self.contracts[key]
        if contract.start > TODAY:
            return []
        if contract.billing == "total":
            amount = contract.amount_on(contract.start)
            return [Accrual(key, first(contract.start), amount, amount, Decimal(0), contract.executor_on(contract.start))]
        out = []
        for month in month_range(contract.start, TODAY):
            period = max(month, contract.start)
            if contract.end and period > contract.end:
                break
            if contract.paused(period):
                continue
            fixed = contract.amount_on(period)
            extra = contract.utilities.get(month, Decimal(0))
            out.append(Accrual(key, month, fixed + extra, fixed, extra, contract.executor_on(period)))
        return out

    def debt(self, key: str, day: date = TODAY) -> Decimal:
        """Долг по договору на конец дня: плюс - должны, минус - аванс."""
        accrued = sum((a.amount for a in self.accruals(key) if max(a.month, self.contracts[key].start) <= day), Decimal(0))
        paid = sum(
            (amount for payment in self.payments if payment.day <= day for c, _m, amount in payment.alloc if c == key),
            Decimal(0),
        )
        return accrued - paid

    def opening(self, key: str) -> Decimal:
        return self.debt(key, SINCE - timedelta(days=1))

    def owed(self, day: date = TODAY) -> dict[str, Decimal]:
        """«Нам должны» по нашим юрлицам: только клиенты (не покупки и не между своими), только плюс."""
        out: dict[str, Decimal] = {}
        for key, contract in self.contracts.items():
            if contract.side != "sale":
                continue
            for accrual in self.accruals(key):
                out.setdefault(accrual.executor, Decimal(0))
            debt = self.debt(key, day)
            if debt > 0:
                code = contract.executor_on(day)
                out[code] = out.get(code, Decimal(0)) + debt
        return out

    def revenue(self, year: int = 2026) -> dict[str, Decimal]:
        """Начислено клиентам за год по исполнителю месяца - оборот к порогу НДС."""
        out: dict[str, Decimal] = {}
        for key, contract in self.contracts.items():
            if contract.side != "sale":
                continue
            for accrual in self.accruals(key):
                if accrual.month.year == year:
                    out[accrual.executor] = out.get(accrual.executor, Decimal(0)) + accrual.amount
        return out


# ── Построение ───────────────────────────────────────────────────────────────


BG_ORGS = (
    "Кокше Строй", "Ертис Агро", "Алатау Тех", "Талдыкорган Мебель", "Шымкент Пласт", "Арал Транс",
    "Балхаш Металл", "Ембі Ойл Сервис", "Іле Девелопмент", "Қазына Фуд", "Сырдария Цемент", "Тобыл Зерно",
    "Ұлытау Майнинг", "Хан Тенгри Тур", "Мангистау Сервис", "Бурабай Отель", "Медеу Спорт", "Есиль Финанс",
    "Кулагер Моторс", "Барыс Секьюрити", "Айгыз Текстиль", "Саяхат Тревел", "Qazaq Soft", "Steppe Coffee",
    "Nomad Logistics", "Silk Way Office",
)
BG_PEOPLE = (
    ("Ахметжанов Даурен", False), ("Сулейменова Камила", True), ("Петров Андрей", False),
    ("Жумабаев Нурсултан", False), ("Оспанова Алия", True), ("Ким Виктория", True), ("Бекова Айжан", True),
    ("Тлеуов Санжар", False), ("Мухамедова Диана", True), ("Ли Артём", False), ("Серикова Томирис", True),
)
COURSES = (
    ("1С: Бухгалтерия", 180000), ("Налоги РК 2026", 220000), ("Excel для финансистов", 120000),
    ("Кадровое делопроизводство", 150000), ("Бухгалтерия с нуля", 350000), ("Английский для бизнеса", 250000),
)


#: Номера особых договоров и черновика (Ф9): автонумерация их не выдаёт.
RESERVED = frozenset({
    "ТС-03/2025", "ТС-15/2026", "ТС-16/2026", "ТС-21/2026", "ТС-28/2026", "ТС-30/2026", "СК-2026/01",
    "А-01", "А-02", "А-03", "А-04", "А-05", "А-06", "А-07", "А-08", "А-09", "А-11", "А-12",
})


class Builder:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.numbers = Numbers(self.rng)
        self.entities: dict[str, Entity] = {}
        self.parties: dict[str, Party] = {}
        self.contracts: dict[str, Contract] = {}
        self.payments: list[Payment] = []
        self.phantoms: list[dict[str, Any]] = []
        #: Месяцы договора, которые платятся не сами (особые оплаты или долг).
        self.manual: dict[str, set[date] | None] = {}
        self.seq: dict[str, int] = {}
        self.world = World(self.entities, self.parties, self.contracts, self.payments, [], self.phantoms)

    # ── участники ──

    def entity(self, key: str, name: str, short: str, born: tuple[int, int], **kw: Any) -> Entity:
        number = self.numbers.iin(date(*born), True) if kw.get("kind") == "ip" else self.numbers.bin(*born[:2])
        item = Entity(key, name, short, number, self.numbers.iban(), **kw)
        self.entities[key] = item
        return item

    def org(self, key: str, base: str, born: tuple[int, int] | None = None, *, style: int | None = None, **kw: Any) -> Party:
        born = born or (self.rng.randint(2005, 2023), self.rng.randint(1, 12))
        style = self.rng.randrange(3) if style is None else style
        onec = (f"{base} ТОО", f'ТОО "{base}"', f"ТОО «{base}»")[style]
        number = self.numbers.bin(*born)
        defaults = {
            "onec": onec,
            "registry": f'ТОО "{base}"',
            "bank": f'ТОО "{base.upper()}"',
            "registry_number": number if self.rng.random() < 0.6 else "",
        }
        party = Party(key, number, "org", f'ТОО "{base}"', iban=self.numbers.iban(self.rng.choice(("722", "601", "856"))), **{**defaults, **kw})
        self.parties[key] = party
        return party

    def person(self, key: str, fio: str, female: bool, *, ip: bool = False, **kw: Any) -> Party:
        number = self.numbers.iin(date(self.rng.randint(1970, 2004), self.rng.randint(1, 12), self.rng.randint(1, 28)), female)
        name = f"ИП {fio}" if ip else fio
        defaults = {"onec": name, "registry": name, "bank": name.upper(), "registry_number": ""}
        party = Party(key, number, "ip" if ip else "person", name, iban=self.numbers.iban(), **{**defaults, **kw})
        self.parties[key] = party
        return party

    # ── договоры ──

    def number(self, entity: str, year: int) -> str:
        """Следующий свободный номер юрлица; номера, заданные особым договорам явно, заняты заранее."""
        prefix = {"ТС": "ТС", "ТО": "А", "ТП": "А", "ИП": "УЦ"}[entity]
        while True:
            self.seq[entity] = self.seq.get(entity, 0) + 1
            n = self.seq[entity]
            value = f"ТС-{n:02d}/{year}" if prefix == "ТС" else f"{prefix}-{n:02d}" if prefix == "А" else f"УЦ-{n:03d}"
            if value in RESERVED:
                continue
            if not any(c.number == value and c.entity == entity for c in self.contracts.values()):
                return value

    def contract(self, key: str, entity: str, party: str, department: str, amount: Any, start: date, *, number: str = "", **kw: Any) -> Contract:
        people = STAFF.get(department, ())
        staff = kw.pop("staff", None)
        if staff is None and people:
            staff = ((self.rng.choice(people), 100),)
        amounts = kw.pop("amounts", None) or [(start, tenge(amount))]
        item = Contract(
            key, entity, party, number or self.number(entity, start.year), kw.pop("signed", start - timedelta(days=self.rng.randint(3, 20))),
            start, amounts, department, tuple(staff or ()), subject=kw.pop("subject", SUBJECT.get(department, "")), **kw,
        )
        self.contracts[key] = item
        return item

    def utilities(self, low: int, high: int, start: date) -> dict[date, Decimal]:
        return {month: tenge(self.rng.randrange(low, high, 1000)) for month in month_range(max(start, YEAR), TODAY)}

    # ── оплаты ──

    def pay(self, day: date, src: str, dst: str, amount: Any, purpose: str, alloc=(), *, kind: str = "contract", **kw: Any) -> Payment:
        item = Payment(f"PAY{len(self.payments) + 1:04d}", day, src, dst, tenge(amount), purpose, kind,
                       [(c, m, tenge(a)) for c, m, a in alloc], **kw)
        self.payments.append(item)
        return item

    def hold(self, key: str, *months: date) -> None:
        """Эти месяцы договора не платятся сами: их оплаты заданы руками (или их нет - долг)."""
        if not months:
            self.manual[key] = None
            return
        current = self.manual.setdefault(key, set())
        if current is not None:
            current.update(months)

    def purpose(self, contract: Contract, month: date, *, with_number: bool) -> str:
        what = {CLEANING: "услуги клининга", SERVICE: "техобслуживание", RENT: "аренду", SCHOOL: "обучение"}.get(contract.department, "услуги")
        base = f"Оплата за {what} за {month_word(month)}"
        return f"{base} по договору № {contract.number}" if with_number else base

    def auto(self) -> None:
        """Фон: каждый месяц платится в срок, с опозданием или (с августа) не платится."""
        for key, contract in self.contracts.items():
            held = self.manual.get(key, set())
            if held is None or contract.side == "purchase":
                continue
            src = contract.party
            for accrual in self.world.accruals(key):
                if accrual.month in held:
                    continue
                roll = self.rng.random()
                if accrual.month >= date(2026, 8, 1) and roll < 0.12:
                    continue
                if contract.billing == "total":
                    day = contract.start - timedelta(days=self.rng.randint(0, 4))
                elif roll < 0.3 and accrual.month < first(TODAY):
                    day = add_months(accrual.month, 1) + timedelta(days=self.rng.randint(1, 11))
                else:
                    day = accrual.month + timedelta(days=self.rng.randint(2, 23))
                if day > TODAY:
                    continue
                with_number = self.rng.random() < 0.6
                self.pay(day, src, accrual.executor, accrual.amount, self.purpose(contract, accrual.month, with_number=with_number),
                         [(key, accrual.month, accrual.amount)])


def build(seed: int = 2610) -> World:
    b = Builder(seed)
    m = lambda month: date(2026, month, 1)

    # ── наши юрлица ──
    b.entity("ТС", 'ТОО "Тумар Сервис"', "Тумар Сервис", (2012, 3), vat_from=date(2026, 3, 1), onec="online")
    b.entity("ТО", 'ТОО "Тумар Офис"', "Тумар Офис", (2016, 7), onec="files")
    b.entity("ТП", 'ТОО "Тумар Плюс"', "Тумар Плюс", (2026, 8), created=date(2026, 8, 14), onec="files")
    b.entity("ИП", "ИП Сейткали Айгерим", "ИП Сейткали", (1990, 5, 14), kind="ip", regime="simplified")

    # ── участники и договоры с особенностями (коды - cases.py) ──
    b.org("altyn", "Алтын Дала", style=0, bank="ALTYN DALA LLP", registry_number="", cases=("У1",))
    b.contract("altyn", "ТС", "altyn", CLEANING, 250000, date(2025, 2, 1), number="ТС-03/2025", cases=("У1", "О1"))
    b.hold("altyn", m(6), m(7), m(8), m(10))
    b.pay(date(2026, 8, 20), "altyn", "ТС", 750000, "Оплата за услуги клининга",
          [("altyn", m(6), 250000), ("altyn", m(7), 250000), ("altyn", m(8), 250000)], cases=("О1",))

    b.org("nur", "Нур", cases=("У2",))
    b.org("nurplus", "Нур Плюс", cases=("У2",))
    b.contract("nur", "ТС", "nur", CLEANING, 140000, date(2025, 9, 1), cases=("У2", "Д19"))
    b.contract("nurplus", "ТС", "nurplus", SERVICE, 90000, date(2026, 2, 1), cases=("У2",))

    b.org("koktem", "Көктем Дизайн", style=2, registry='тоо "көктем дизайн"', registry_number="", cases=("У3",))
    b.contract("koktem", "ТС", "koktem", CLEANING, 110000, date(2026, 3, 1), cases=("У3",))

    samal = b.org("samal", "Самал Клиник", cases=("У4",))
    samal.registry_number = broken(samal.number)
    b.contract("samal", "ТС", "samal", CLEANING, 320000, date(2025, 11, 1), cases=("У4", "Д18", "О3"))
    b.hold("samal", m(8))
    b.pay(date(2026, 8, 18), "samal", "ТС", 200000, "Частичная оплата за услуги клининга за август 2026",
          [("samal", m(8), 200000)], cases=("О3",))
    b.contract("samal_course", "ИП", "samal", SCHOOL, 1500000, date(2026, 9, 7), billing="total", subject="Корпоративное обучение: Налоги РК 2026",
               registry_amount="1,5 млн", cases=("Ф2",))
    b.hold("samal_course")
    b.pay(date(2026, 9, 5), "samal", "ИП", 750000, "Предоплата 50% за корпоративное обучение", [("samal_course", m(9), 750000)])

    b.org("taza", "Таза Су", onec="Таза Су", onec_duplicate="Таза Су (не использовать)", cases=("У5",))
    b.contract("taza", "ТС", "taza", SERVICE, 75000, date(2025, 6, 1), cases=("У5",))

    b.org("beton", "Бетон Строй", renamed=(date(2026, 8, 1), 'ТОО "BS Group"', 'ТОО "BS GROUP"'), cases=("У6",))
    b.contract("beton", "ТО", "beton", RENT, 300000, date(2025, 4, 1), number="А-02", premises="офис 301",
               utilities=b.utilities(40000, 90000, date(2025, 4, 1)), cases=("У6", "Д11"))

    b.org("megaR", "Мега Ритейл", cases=("У7",))
    b.org("megaH", "Мега Холдинг", registry="", registry_number="", cases=("У7",))
    b.contract("megaR", "ТО", "megaR", RENT, 450000, date(2025, 10, 1), number="А-07", premises="помещение 1, 1 этаж",
               utilities=b.utilities(60000, 120000, date(2025, 10, 1)), cases=("У7", "Д11"))
    b.hold("megaR", *month_range(SINCE, TODAY))
    for month in month_range(SINCE, TODAY):
        accrual = next(a for a in b.world.accruals("megaR") if a.month == month)
        b.pay(month + timedelta(days=9), "megaH", "ТО", accrual.amount, f"Аренда за {MONTHS[month.month - 1]} за ТОО Мега Ритейл по дог. А-07",
              [("megaR", month, accrual.amount)], cases=("У7",))

    b.person("ahm_ip", "Ахметов Бауыржан", False, ip=True, cases=("У8",))
    b.org("ahm_too", "Ахметов и Ко", cases=("У8",))
    b.contract("ahm_ip", "ТС", "ahm_ip", CLEANING, 60000, date(2026, 1, 1), cases=("У8",))
    b.contract("ahm_too", "ТС", "ahm_too", SERVICE, 85000, date(2025, 12, 1), cases=("У8",))

    b.person("kasym", "Касымов Ерлан", False, bank="КАСЫМОВ Е.", cases=("У9",))
    b.contract("kasym", "ИП", "kasym", SCHOOL, 350000, date(2026, 7, 6), billing="total", subject="Курс «Бухгалтерия с нуля»",
               in_registry=False, cases=("У9",))
    b.hold("kasym")
    b.pay(date(2026, 7, 3), "kasym", "ИП", 350000, "Перевод с карты. Оплата курса", [("kasym", m(7), 350000)], cases=("У9",))

    b.org("orken_clinic", "Өркен Клиник", cases=("У10",))
    b.contract("orken_clinic", "ТС", "orken_clinic", CLEANING, 180000, date(2026, 2, 1), cases=("У10",))
    b.org("orken_media", "Оркен Медиа", cases=("У10", "Д3", "С2"))
    b.contract("orken_media", "ТС", "orken_media", CLEANING, 150000, date(2026, 4, 1), number="ТС-21/2026", onec_number="", cases=("Д3", "С2"))
    b.hold("orken_media", m(5))
    b.pay(date(2026, 6, 25), "orken_media", "ТС", 150000, "Оплата задолженности за май", [("orken_media", m(5), 150000)])

    b.org("saryarka", "Сарыарка Логистик")
    b.contract("d1", "ТС", "saryarka", CLEANING, 200000, date(2026, 3, 1), number="ТС-15/2026", registry_number="ТС 15-2026", cases=("Д1",))
    b.contract("o2", "ТС", "saryarka", SERVICE, 80000, date(2026, 3, 1), number="ТС-16/2026", cases=("О2",))
    b.hold("d1", *month_range(m(3), TODAY))
    for month in month_range(m(3), TODAY):
        if month == m(9):
            continue
        b.pay(month + timedelta(days=11), "saryarka", "ТС", 200000, f"оплата по дог.15 за {MONTHS[month.month - 1]}", [("d1", month, 200000)], cases=("Д1",))
    b.hold("o2", m(9))
    b.pay(date(2026, 9, 10), "saryarka", "ТС", 280000, "Оплата за сентябрь 2026", [("d1", m(9), 200000), ("o2", m(9), 80000)], cases=("О2",))

    b.org("kaspiy", "Каспий Трейд")
    b.contract("kaspiy", "ТО", "kaspiy", RENT, 250000, date(2025, 9, 1), number="А-01", premises="офис 105",
               utilities=b.utilities(30000, 70000, date(2025, 9, 1)), cases=("Д2", "С1", "Д11"))
    b.hold("kaspiy", m(4), m(5))
    b.pay(date(2026, 7, 15), "kaspiy", "ТО", 250000, "Погашение задолженности по аренде за апрель 2026", [("kaspiy", m(4), 250000)])

    b.org("greenoffice", "Green Office", registry='ТОО "Green Office"', bank='ТОО "GREEN OFFICE"')
    b.contract("greenoffice", "ТП", "greenoffice", RENT, 280000, date(2026, 9, 1), number="А-01", premises="офис 410",
               deposit=Decimal(280000), cases=("Д2", "Д12"))
    b.pay(date(2026, 8, 28), "greenoffice", "ТП", 280000, "Обеспечительный платёж по договору аренды А-01", kind="deposit", cases=("Д12",))

    b.org("baiterek", "Байтерек Девелопмент")
    b.contract("baiterek", "ТС", "baiterek", SERVICE, 230000, date(2026, 7, 1), number="ТС-30/2026", in_registry=False, cases=("Д4",))

    b.org("shugyla", "Шұғыла Фарм")
    b.contract("shugyla", "ТС", "shugyla", CLEANING, 150000, date(2025, 8, 1),
               amounts=[(date(2025, 8, 1), tenge(150000)), (m(7), tenge(180000))], registry_amount=180000, cases=("Д5",))

    b.org("alem", "Әлем Принт")
    b.contract("alem", "ТС", "alem", CLEANING, 150000, date(2026, 1, 1), cases=("Д6",))

    for key, base, amount, premises, number in (
        ("teniz", "Тениз Фиш", 250000, "офис 204", "А-05"),
        ("keruen", "Керуен Логистик", 320000, "офис 207-208", "А-06"),
        ("smartlab", "SmartLab", 180000, "офис 215", "А-08"),
    ):
        b.org(key, base, **({"registry": 'ТОО "SmartLab"', "bank": 'ТОО "SMARTLAB"'} if key == "smartlab" else {}))
        b.contract(key, "ТО", key, RENT, amount, date(2025, 11, 1), number=number, premises=premises, executors=[(m(10), "ТП")],
                   registry_executor="ТО", cases=("Д7",), **({"utilities": b.utilities(35000, 80000, date(2025, 11, 1))} if key == "teniz" else {}))

    b.org("miras", "Мирас Колледж")
    b.contract("miras", "ТС", "miras", CLEANING, 280000, date(2025, 9, 1), pauses=[(m(7), date(2026, 8, 31))],
               registry_status="приостановлен", cases=("Д8",))

    b.org("zhetysu", "Жетысу Агро")
    b.contract("zhetysu", "ТС", "zhetysu", SERVICE, 95000, date(2025, 3, 1), end=date(2026, 5, 31), registry_status="расторгнут", cases=("Д9",))

    b.org("dostyk", "Достык Энерго")
    b.contract("dostyk", "ТО", "dostyk", RENT, 380000, date(2025, 7, 1), number="А-04", premises="офис 501-503",
               amounts=[(date(2025, 7, 1), tenge(380000)), (m(7), tenge(418000))], registry_amount=380000,
               registry_note="индексация с июля 10%", utilities=b.utilities(50000, 110000, date(2025, 7, 1)), cases=("Д10", "Д11", "О5"))
    b.hold("dostyk", m(9))
    september = next(a for a in b.world.accruals("dostyk") if a.month == m(9))
    b.pay(date(2026, 9, 12), "dostyk", "ТО", september.amount, "Аренда за сентябрь по дог. А-01", [("dostyk", m(9), september.amount)], cases=("О5",))

    b.person("nurlanova", "Нурланова Сауле", True)
    b.contract("nurlanova", "ИП", "nurlanova", SCHOOL, 900000, date(2026, 7, 6), billing="total", subject="Курс «Главный бухгалтер»",
               in_registry=False, cases=("Д13",))
    b.hold("nurlanova")
    for month in (7, 8, 9):
        b.pay(date(2026, month, 10), "nurlanova", "ИП", 300000, f"Оплата курса, часть {month - 6} из 3", [("nurlanova", m(7), 300000)], cases=("Д13",))

    b.org("zhuldyz", "Жулдыз Кейтеринг")
    zh = b.contract("zhuldyz", "ТС", "zhuldyz", CLEANING, 120000, date(2026, 6, 1), cases=("Д14",))
    b.hold("zhuldyz")
    b.pay(date(2026, 6, 5), "zhuldyz", "ТС", 1440000, f"Предоплата за 12 месяцев по договору № {zh.number}",
          [("zhuldyz", None, 1440000)], cases=("Д14",))

    b.org("tulpar", "Тулпар Авто")
    b.contract("tulpar", "ТС", "tulpar", SERVICE, 45000, date(2026, 7, 14), billing="total", in_registry=False, onec_number="",
               subject="Вызов техника: ремонт вентиляции", cases=("Д15",))
    b.org("ulydala", "Ұлы Дала Тур")
    b.contract("ulydala", "ТС", "ulydala", SERVICE, 60000, date(2026, 9, 2), billing="total", in_registry=False, onec_number="",
               subject="Вызов техника: замена смесителей", cases=("Д15",))
    b.hold("ulydala")

    b.contract("intra", "ТО", "ТС", RENT, 180000, date(2025, 5, 1), number="А-09", side="intra", premises="склад, цокольный этаж", cases=("Д16",))

    b.org("sunkar", "Сункар Клин")
    b.contract("sunkar", "ТС", "sunkar", CLEANING, 200000, date(2026, 1, 1), number="СК-2026/01", side="purchase",
               subject="Субподряд: уборка объектов", cases=("Д17",))

    b.org("bereke", "Береке Маркет")
    b.contract("bereke11", "ТО", "bereke", RENT, 180000, date(2026, 2, 1), number="А-11", premises="помещение 12", cases=("О4",))
    b.contract("bereke12", "ТО", "bereke", RENT, 180000, date(2026, 2, 1), number="А-12", premises="помещение 14", cases=("О4",))
    b.hold("bereke11", m(9))
    b.hold("bereke12", m(9))
    b.pay(date(2026, 9, 5), "bereke", "ТО", 180000, "Аренда за сентябрь", [("bereke12", m(9), 180000)], cases=("О4",))

    b.org("nurtau", "Нуртау Строй")
    nurtau = b.contract("nurtau", "ТС", "nurtau", CLEANING, 250000, date(2026, 2, 1), cases=("О6",))
    b.hold("nurtau", m(7))
    b.pay(date(2026, 7, 8), "nurtau", "ТС", 300000, f"Оплата за июль по договору № {nurtau.number}", [("nurtau", m(7), 300000)])
    b.pay(date(2026, 8, 15), "ТС", "nurtau", 50000, f"Возврат излишне оплаченной суммы по договору № {nurtau.number}",
          [("nurtau", None, -50000)], kind="refund", cases=("О6",))

    b.org("eurasia", "Евразия Фрейт", style=0, bank="EURASIA FREIGHT LLP", cases=("О7",))
    b.contract("eurasia", "ТС", "eurasia", SERVICE, 120000, date(2026, 1, 1), cases=("О7",))

    b.pay(date(2026, 8, 20), "ТС", "ТП", 2000000, "Временная финансовая помощь по договору ФП-1 от 19.08.2026", kind="own", cases=("О8",))
    b.pay(date(2026, 10, 15), "ТП", "ТС", 500000, "Частичный возврат финансовой помощи по договору ФП-1", kind="own", cases=("О8",))

    b.org("arman", "Арман Эксперт")
    b.contract("arman", "ТС", "arman", CLEANING, 130000, date(2026, 1, 1), cases=("О9",))
    b.hold("arman", m(6))
    b.pay(date(2026, 5, 28), "arman", "ТС", 130000, "Оплата за июнь 2026", [("arman", m(6), 130000)], cases=("О9",))

    b.org("bolashak", "Болашак Консалт")
    b.contract("bolashak", "ТС", "bolashak", SERVICE, 180000, date(2025, 10, 1), cases=("О10", "С3"))
    b.hold("bolashak", m(5), m(9))
    b.pay(date(2026, 5, 12), "bolashak", "ТС", 270000, "Оплата за май и часть июня", [("bolashak", m(5), 270000)], cases=("С3",))
    b.pay(date(2026, 9, 9), "bolashak", "ТС", 200000, "Оплата за сентябрь", [("bolashak", m(9), 200000)], cases=("О10",))

    b.person("ivanova", "Иванова Мария", True)
    b.person("ivanova_g", "Иванова Гульнар", True, registry="")
    b.contract("ivanova", "ИП", "ivanova", SCHOOL, 250000, date(2026, 9, 7), billing="total", subject="Курс «Английский для бизнеса»",
               in_registry=False, cases=("О11",))
    b.hold("ivanova")
    b.pay(date(2026, 9, 3), "ivanova_g", "ИП", 250000, "Перевод с карты. За курс дочери", [("ivanova", m(9), 250000)], cases=("О11",))

    b.org("kainar", "Кайнар Бетон")
    b.contract("kainar", "ТО", "kainar", RENT, 280000, date(2025, 6, 1), number="А-03", end=date(2026, 6, 30), premises="офис 112",
               deposit=Decimal(280000), registry_status="действует", cases=("Ф8", "Д12"))
    b.pay(date(2026, 7, 10), "ТО", "kainar", 280000, "Возврат обеспечительного платежа по договору А-03", kind="deposit", cases=("Д12",))

    b.phantoms.append({"registry": 'ТОО "Медеу Спорт"', "number": "ТС-28/2026", "amount": 210000, "status": "черновик", "case": "Ф9"})

    # ── фон: обычные клиенты ──
    names = list(BG_ORGS)
    b.rng.shuffle(names)
    for index in range(12):
        key = f"bgc{index + 1:02d}"
        b.org(key, names.pop())
        start = date(b.rng.choice((2025, 2025, 2026)), b.rng.randint(1, 12), 1)
        start = min(start, date(2026, 8, 1))
        b.contract(key, "ТС", key, CLEANING, b.rng.randrange(80000, 450000, 5000), start)
    for index in range(5):
        key = f"bgs{index + 1:02d}"
        b.org(key, names.pop())
        start = date(2026, b.rng.randint(1, 7), 1)
        b.contract(key, "ТС", key, SERVICE, b.rng.randrange(60000, 200000, 5000), start)
    for index in range(5):
        key = f"bgr{index + 1:02d}"
        b.org(key, names.pop())
        start = date(2025, b.rng.randint(1, 12), 1)
        b.contract(key, "ТО", key, RENT, b.rng.randrange(120000, 300000, 10000), start, premises=f"офис {b.rng.randint(101, 520)}",
                   utilities=b.utilities(20000, 90000, start) if index % 2 == 0 else {})
    for index, (fio, female) in enumerate(BG_PEOPLE):
        key = f"bgp{index + 1:02d}"
        b.person(key, fio, female)
        course, price = b.rng.choice(COURSES)
        start = date(2026, b.rng.randint(3, 10), b.rng.randint(1, 12))
        if start > TODAY:
            start = date(2026, 10, b.rng.randint(1, 12))
        # Слушатели живут в таблице учебного центра, а не в реестре офис-менеджера.
        b.contract(key, "ИП", key, SCHOOL, price, start, billing="total", subject=f"Курс «{course}»", in_registry=False)
        b.hold(key)
        # День оплаты - в том же месяце и не позже 12-го: в таблице слушателей все даты такие (Ф3).
        paid_on = start - timedelta(days=min(start.day - 1, b.rng.randint(0, 3)))
        if b.rng.random() < 0.5:
            b.pay(paid_on, key, "ИП", price, "Перевод с карты. Оплата курса", [(key, first(start), price)])
        else:
            half = tenge(price / 2)
            b.pay(paid_on, key, "ИП", half, "Перевод с карты. Оплата курса, первая часть", [(key, first(start), half)])
            later = add_months(start, 1) + timedelta(days=b.rng.randint(0, 11))
            if later <= TODAY and b.rng.random() < 0.7:
                b.pay(later, key, "ИП", price - half, "Перевод с карты. Оплата курса, вторая часть", [(key, first(start), price - half)])

    b.auto()
    # Покупка: платим сами 20-го числа за этот месяц; октябрь ещё не оплачен.
    for accrual in b.world.accruals("sunkar"):
        if accrual.month < m(10):
            b.pay(accrual.month + timedelta(days=19), "ТС", "sunkar", accrual.amount,
                  f"Оплата по договору СК-2026/01 за {month_word(accrual.month)}", [("sunkar", accrual.month, accrual.amount)])
    b.payments.sort(key=lambda p: (p.day, p.key))
    b.world.documents.extend(documents(b))
    return b.world


# ── Документы 1С ─────────────────────────────────────────────────────────────


def documents(b: Builder) -> list[Document]:
    """Счета и АВР 1С наших юрлиц с 1С. ИП без 1С документов не выставляет.

    Счёт - первого числа месяца начисления (у разового - в день начала), АВР -
    последним числом месяца, и только за прошедшие месяцы: акт октября ещё
    не выписан 25.10. Нумерация - своя у каждого юрлица с начала года.
    """
    world, rng = b.world, b.rng
    out: list[Document] = []
    counters: dict[tuple[str, str], int] = {}

    def next_number(entity: str, kind: str) -> str:
        counters[(entity, kind)] = counters.get((entity, kind), 0) + 1
        return f"{counters[(entity, kind)]:011d}"

    events: list[tuple[date, str, Contract, Accrual]] = []
    for key, contract in world.contracts.items():
        if contract.side == "purchase":
            continue
        for accrual in world.accruals(key):
            entity = world.entities[accrual.executor]
            if entity.onec == "none" or accrual.month < YEAR:
                continue
            if contract.billing == "total":
                events.append((contract.start, "invoice", contract, accrual))
                events.append((contract.start + timedelta(days=rng.randint(0, 3)), "act", contract, accrual))
                continue
            events.append((accrual.month, "invoice", contract, accrual))
            if accrual.month < first(TODAY):
                events.append((month_end(accrual.month), "act", contract, accrual))
    events.sort(key=lambda item: (item[0], item[1], item[2].key))
    unsigned = {("samal", date(2026, month, 1)) for month in (7, 8, 9)}
    no_esf = {("nur", date(2026, 6, 1)), ("nur", date(2026, 7, 1)), ("alem", date(2026, 9, 1)), ("eurasia", date(2026, 9, 1))}
    for day, kind, contract, accrual in events:
        if day > TODAY:
            continue
        entity = world.entities[accrual.executor]
        lines = SUBJECT.get(contract.department, contract.subject)
        if contract.billing == "total":
            content = contract.subject
        else:
            content = f"{lines} за период с {accrual.month:%d.%m.%Y} по {month_end(accrual.month):%d.%m.%Y}"
            if accrual.utilities:
                content += "; коммунальные услуги по показаниям счётчиков"
        amount_1c = accrual.amount
        if kind == "act" and contract.key == "alem" and accrual.month == date(2026, 8, 1):
            amount_1c = tenge(1500000)
        doc = Document(f"{kind}-{entity.key}-{contract.key}-{accrual.month:%Y%m}", kind, entity.key, contract.key, accrual.month,
                       next_number(entity.key, kind), day, accrual.amount, amount_1c, content)
        if kind == "act":
            if (contract.key, accrual.month) not in unsigned and day + timedelta(days=10) <= TODAY:
                doc.signed = day + timedelta(days=rng.randint(2, 9))
            if entity.vat_from and day >= entity.vat_from and (contract.key, accrual.month) not in no_esf:
                doc.esf = day + timedelta(days=rng.randint(1, 10))
                if doc.esf > TODAY:
                    doc.esf = None
        out.append(doc)
    return out


def case_index(world: World) -> dict[str, list[str]]:
    """Где в мире живёт каждый случай каталога: ключи участников, договоров, оплат."""
    out: dict[str, list[str]] = {case: [] for case in BY_ID}
    for kind, items in (("party", world.parties.values()), ("contract", world.contracts.values()), ("payment", world.payments)):
        for item in items:
            for case in item.cases:
                out.setdefault(case, []).append(f"{kind}:{item.key}")
    for phantom in world.phantoms:
        out.setdefault(phantom["case"], []).append(f"phantom:{phantom['number']}")
    return out


__all__ = [
    "SINCE",
    "TODAY",
    "Accrual",
    "Contract",
    "Document",
    "Entity",
    "Party",
    "Payment",
    "World",
    "add_months",
    "broken",
    "build",
    "case_index",
    "control",
    "first",
    "month_end",
    "month_range",
    "month_word",
    "tenge",
]
