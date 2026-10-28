"""Таблицы ядра: журнал фактов, документы договора, налоговый профиль наших юрлиц, налоговые годы.

Что здесь правда, а что производное
───────────────────────────────────
**Журнал фактов** (`facts`, `core/journal.py`) - одна форма для всего, что
случилось с договором: оплаты, счета, АВР, ЭСФ, соглашения, поправки, сальдо,
решения. Факты проводятся из хранилищ - операций журнала денег, документов
1С, документов ядра, соглашений - и не правятся: изменение - новая версия.
Итоги ядра читают журнал.

**Правда** - документы договора за месяц (`documents`: поправка начисления,
счёт, АВР, ЭСФ, входящее сальдо), режим нашего юрлица с даты
(`entity_tax_periods`), поправки компании к налоговым числам года
(`tax_years`): МРП, порог постановки на учёт по НДС в МРП, ставка (без
поправки действуют числа РК из `core/tax.py`), и настройки ядра компании
(`core_settings`): с какого дня ведутся оплаты и откуда они у каждого юрлица.

**Производное** - начисления (`core/accruals.py` - из условий договора),
оборот юрлица к порогу, НДС в документе, прогноз по месяцам: считаются из
событий и условий договоров. На сегодня начисления и расчёты договоров
хранятся в регистрах итогов (`register_*`, `core/registers.py`, этап 2,
шаг 2): это вывод, который пересчитывается, когда сменились его входы, и
руками не правится.

Почему режим - по датам, а не флажок «плательщик НДС» у юрлица: юрлицо встаёт
на учёт по НДС с числа, и обороты, документы и счета до этого числа
считаются без НДС, после - с ним. Флажок без даты переписал бы прошлое.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.finance.db import FinanceBase
from app.finance.models import JSONB, MONEY, _in

#: Налоговый режим юрлица: общеустановленный, упрощённая декларация, другой
#: специальный (патент, розничный налог и т.п.). На специальном режиме
#: плательщиком НДС не бывают (НК РК с 2026 года) - проверяет `core/tax.py`.
TAX_REGIMES = ("general", "simplified", "other")
#: Документы договора: поправка начисления, счёт, АВР, ЭСФ, входящее сальдо.
#: `verified` - решение человека по сверке с таблицей: месяц договора у KORT
#: верен, таблица в нём ошиблась или ещё не дописана (`core/changes.verify_month`).
DOCUMENT_KINDS = ("adjustment", "bill", "act", "esf", "opening", "verified")
#: Откуда документ: записан в KORT, прочитан из книги или из 1С.
DOCUMENT_SOURCES = ("kort", "book", "1c")
#: Виды фактов журнала (`core/journal.py`): оплата, счёт, АВР, ЭСФ,
#: соглашение, поправка начисления, входящее сальдо, решение человека.
FACT_KINDS = ("payment", "bill", "act", "esf", "agreement", "adjustment", "opening", "decision")
#: Откуда факт: записан человеком в KORT, журнал движения денег (выписки),
#: 1С, таблица Google.
FACT_SOURCES = ("kort", "journal", "1c", "book")


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Document(FinanceBase):
    """Документ договора за месяц - событие, а не клетка книги.

    `period` - первый день месяца начисления, к которому документ относится:
    счёт за октябрь, выставленный 28 сентября, - всё равно октябрьский.

    Поправка (`adjustment`) - решение человека о начислении месяца: другая
    сумма или «не начислять» (`skip`), с причиной. Само начисление не хранится
    (`core/accruals.py`), поэтому поправка - единственный способ сказать «в
    этом месяце не так, как по договору», и он виден в истории.

    Из книги и 1С документ приходит с ключом источника: повторное чтение
    находит свой документ, а не заводит второй.
    """

    __tablename__ = "documents"
    __table_args__ = (
        sa.CheckConstraint(_in("kind", DOCUMENT_KINDS), name="document_kind"),
        sa.CheckConstraint(_in("source", DOCUMENT_SOURCES), name="document_source"),
        sa.Index("ix_documents_contract_period", "workspace_id", "contract_id", "period"),
        sa.Index(
            "uq_documents_source_key",
            "workspace_id",
            "source",
            "kind",
            "source_key",
            unique=True,
            postgresql_where=sa.text("source_key <> ''"),
            sqlite_where=sa.text("source_key <> ''"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    contract_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("contracts.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(sa.Text)
    period: Mapped[date | None] = mapped_column(sa.Date)
    number: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    issued_on: Mapped[date | None] = mapped_column(sa.Date)
    amount: Mapped[Decimal | None] = mapped_column(MONEY)
    skip: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    state: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    reason: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    source: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'kort'"))
    source_key: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    meta: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    seq: Mapped[int] = mapped_column(sa.BigInteger, server_default=sa.text("0"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Fact(FinanceBase):
    """Факт журнала: что случилось с договором, в одной форме для всех источников.

    Журнал не правится. Источник изменился - пишется новая версия факта с тем
    же ключом (`replaces` - прежняя, у прежней ставится `ended_seq`); источник
    убрал факт - пишется пометка «снят» (`void`). Так у любого числа есть
    ответ «из каких фактов и с какой записи», а у подписчика - «что изменилось
    после номера N» (`seq`). Единственная правка строки - `ended_seq`, и она
    выводима из `replaces`; хранится, чтобы «действующие» выбирались индексом.

    `key` - кто этот факт в своём хранилище: `op:<операция>`, `1c:<документ
    1С>`, `doc:<документ KORT>`, `amend:<соглашение>`, `cp:<операция>`
    (решение по оплате), `1cm:<документ 1С>` (решение по документу 1С).
    Ссылки на договор и стороны - без внешних ключей: журнал помнит и то, чего
    в реестре уже нет.
    """

    __tablename__ = "facts"
    __table_args__ = (
        sa.CheckConstraint(_in("kind", FACT_KINDS), name="fact_kind"),
        sa.CheckConstraint(_in("source", FACT_SOURCES), name="fact_source"),
        sa.Index(
            "uq_facts_current_key",
            "workspace_id",
            "key",
            unique=True,
            postgresql_where=sa.text("ended_seq IS NULL"),
            sqlite_where=sa.text("ended_seq IS NULL"),
        ),
        sa.Index(
            "ix_facts_current_kind",
            "workspace_id",
            "kind",
            "contract_id",
            postgresql_where=sa.text("ended_seq IS NULL"),
            sqlite_where=sa.text("ended_seq IS NULL"),
        ),
        sa.Index("ix_facts_workspace_seq", "workspace_id", "seq"),
        sa.Index("ix_facts_contract", "workspace_id", "contract_id", "seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"))
    #: Номер проведения, в котором факт записан: один на всё проведение.
    seq: Mapped[int] = mapped_column(sa.BigInteger)
    key: Mapped[str] = mapped_column(sa.Text)
    kind: Mapped[str] = mapped_column(sa.Text)
    source: Mapped[str] = mapped_column(sa.Text)
    #: Когда случилось: день платежа, дата документа, «с какого дня» соглашения.
    day: Mapped[date | None] = mapped_column(sa.Date)
    #: За какой месяц (первое число): месяц АВР, поправки, сальдо.
    period: Mapped[date | None] = mapped_column(sa.Date)
    amount: Mapped[Decimal | None] = mapped_column(MONEY)
    contract_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    #: Вторая сторона: плательщик, клиент.
    party_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    #: Наше юрлицо: чей счёт, чья база 1С.
    entity_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    #: Остальное своего вида: № и состояние, «за какие месяцы», как найден договор…
    data: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    #: Кто записал: человек у фактов KORT, пусто у фактов источника.
    author: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    recorded_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())
    #: Прежняя версия того же факта.
    replaces: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    #: Источник факт убрал: строка - пометка «снят», без суммы.
    void: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    #: Номер проведения, в котором факт сменила новая версия или пометка «снят».
    ended_seq: Mapped[int | None] = mapped_column(sa.BigInteger)


class FactInput(FinanceBase):
    """Правка хранилища, ещё не проведённая в журнал: что именно изменилось.

    Писатель кладёт строку тем же коммитом, что и правку: номер правки
    (`inputs` - значение счётчика `facts.inputs`), часть журнала и ключ -
    операция (`journal`), договор (`kort`: его документы и соглашения) или
    `WHOLE` - вся часть (сменилась форма реестра: стороны договора, написания,
    наши юрлица; документы 1С). Читатель проводит только это, а не весь
    журнал (`core/journal.ensure`), и снимает строки до номера проведённой
    правки.
    """

    __tablename__ = "fact_inputs"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    inputs: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    part: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    ref: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)


class RegisterState(FinanceBase):
    """Регистры итогов компании (`core/registers.py`): на какой день и при каких настройках посчитаны.

    `signature` - подпись того, что меняет итоги всех договоров разом: начало
    учёта и источники оплат, наши юрлица, смыслы статусов, месяц. Сменилась -
    пересчитывается всё; иначе - только договоры с другой подписью входов.
    `facts_seq` - номер проведения журнала, после которого посчитано начало
    учёта, когда его не задал человек (оно - от первой оплаты).
    """

    __tablename__ = "register_state"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    signature: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    day: Mapped[date | None] = mapped_column(sa.Date)
    since: Mapped[date | None] = mapped_column(sa.Date)
    facts_seq: Mapped[int] = mapped_column(sa.BigInteger, server_default=sa.text("0"))
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())


class RegisterContract(FinanceBase):
    """Итог договора на день регистра: сальдо, переплата, оплачено и до какого дня это верно.

    Строка - вывод, руками не правится: пересчитывается, когда сменилась
    подпись входов договора (`signature`: условия договора и версия его фактов
    журнала) или наступил `valid_until` (в учёте есть оплата будущим числом).
    Входящее сальдо гасят оплаты так же, как месяц: `opening_paid` - разнесено
    на него всего, `opening_cash` - из этого платежами, без аванса на начало.
    """

    __tablename__ = "register_contracts"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    signature: Mapped[str] = mapped_column(sa.Text)
    valid_until: Mapped[date | None] = mapped_column(sa.Date)
    #: Считает ли KORT начисления договора; нет - долг неизвестен.
    accrues: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("true"))
    opening: Mapped[Decimal | None] = mapped_column(MONEY)
    opening_paid: Mapped[Decimal] = mapped_column(MONEY, server_default=sa.text("0"))
    opening_cash: Mapped[Decimal] = mapped_column(MONEY, server_default=sa.text("0"))
    opening_executor: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    #: Сверх всех начислений - платежами (переплата).
    over: Mapped[Decimal] = mapped_column(MONEY, server_default=sa.text("0"))
    #: Оплачено в учёте KORT на день регистра.
    paid: Mapped[Decimal] = mapped_column(MONEY, server_default=sa.text("0"))


class RegisterMonth(FinanceBase):
    """Месяц расчётов договора: начислено, разнесено оплат, оплачено за месяц и поступило.

    `bucket` - месяц, который гасят оплаты (начисленный к дню регистра или уже
    оплаченный вперёд); остальные строки несут только «оплачено за месяц, как
    держит таблица» (`attributed`) и «поступило в месяце» (`received`).
    """

    __tablename__ = "register_months"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    month: Mapped[date] = mapped_column(sa.Date, primary_key=True)
    bucket: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    accrued: Mapped[Decimal] = mapped_column(MONEY, server_default=sa.text("0"))
    #: С какого дня начисление месяца - долг: «Период с» или первое число.
    starts: Mapped[date | None] = mapped_column(sa.Date)
    #: Наше юрлицо месяца: исполнитель на день периода.
    executor_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    paid: Mapped[Decimal] = mapped_column(MONEY, server_default=sa.text("0"))
    cash: Mapped[Decimal] = mapped_column(MONEY, server_default=sa.text("0"))
    attributed: Mapped[Decimal] = mapped_column(MONEY, server_default=sa.text("0"))
    received: Mapped[Decimal] = mapped_column(MONEY, server_default=sa.text("0"))


class RegisterAccrual(FinanceBase):
    """Начисление периода договора - из условий, соглашений и поправок, от начала договора до горизонта."""

    __tablename__ = "register_accruals"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    period_from: Mapped[date] = mapped_column(sa.Date, primary_key=True)
    month: Mapped[date] = mapped_column(sa.Date)
    period_to: Mapped[date | None] = mapped_column(sa.Date)
    base: Mapped[Decimal | None] = mapped_column(MONEY)
    amount: Mapped[Decimal | None] = mapped_column(MONEY)
    executor_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    #: Поправка месяца (документ ядра) и «не начислять».
    adjustment_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    skipped: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))


class EntityTaxPeriod(FinanceBase):
    """Режим нашего юрлица с даты: какой и плательщик ли НДС."""

    __tablename__ = "entity_tax_periods"
    __table_args__ = (
        sa.CheckConstraint(_in("regime", TAX_REGIMES), name="entity_tax_period_regime"),
        sa.UniqueConstraint("entity_id", "valid_from", name="uq_entity_tax_periods_from"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("group_entities.counterparty_id", ondelete="CASCADE"), index=True
    )
    valid_from: Mapped[date] = mapped_column(sa.Date)
    regime: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'general'"))
    vat_payer: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    note: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class TaxYear(FinanceBase):
    """Поправка компании к налоговым числам года.

    Числа года меняет закон о бюджете (МРП) и Налоговый кодекс (порог, ставка).
    Умолчания РК лежат в коде (`core/tax.py`); строка здесь нужна, когда
    компания знает число раньше, чем вышло обновление KORT, или ведёт учёт
    по-своему. `forecast` - год, числа которого ещё прогноз (МРП следующего
    года до закона о бюджете).
    """

    __tablename__ = "tax_years"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    year: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    mrp: Mapped[Decimal] = mapped_column(MONEY)
    vat_threshold_mrp: Mapped[int] = mapped_column(sa.Integer)
    vat_rate: Mapped[Decimal] = mapped_column(sa.Numeric(5, 2))
    forecast: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    note: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class CoreSettings(FinanceBase):
    """Настройки ядра компании - решения человека, которых нет в данных.

    `data` - словарь по разделам (`core/settings.py` знает ключи и умолчания):
    `payments.since` - с какого дня KORT ведёт оплаты (до него долг - входящее
    сальдо, а не неоплаченные начисления), `payments.sources` - откуда оплаты
    нашего юрлица: журнал движения денег или 1С (один источник на юрлицо,
    второй - для сверки).
    """

    __tablename__ = "core_settings"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    data: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


#: Откуда строки вида: договоры листами, «Долги», «Не выставлены» (`core/views.py`).
VIEW_SOURCES = ("contracts", "debts", "unbilled")
#: Кому вид стоит в колонке разделов: всем, кто видит его данные; только
#: владельцу (отдел - его людям, человек - ему); никому - вид открывается из «Ещё».
VIEW_PINS = ("all", "owner", "")


class View(FinanceBase):
    """Вид - сохранённый вопрос к ядру: отбор, меры, группировки, форма, владелец.

    «Реестр», вид отдела, «Долги», «Не выставлены» - виды, а не разделы,
    зашитые в код (этап 2, шаг 6). Колонка разделов собирается из
    закреплённых видов. Вид договоров - книга листов: `entity_views.book` -
    ключ вида; реестр - вид с ключом `""`. Владелец - компания (оба поля
    пусты), отдел или человек; отдел-владелец - ещё и доля: в «Разовых ЮО»
    сумма договора - доля ЮО.

    Меры - итоги ядра (`core/views.MEASURES`), а не поля договора и не
    формулы человека: формула вернула бы сверки.
    """

    __tablename__ = "views"
    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "key", name="uq_views_key"),
        sa.CheckConstraint(_in("source", VIEW_SOURCES), name="view_source"),
        sa.CheckConstraint(_in("pin", VIEW_PINS), name="view_pin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(sa.Text)
    title: Mapped[str] = mapped_column(sa.Text)
    source: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'contracts'"))
    owner_department_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("departments.id", ondelete="SET NULL")
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE")
    )
    #: Отбор - правило того же языка, что у листов (`contracts/views.py`); пусто - все.
    filter: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    measures: Mapped[list] = mapped_column(JSONB, server_default=sa.text("'[]'"))
    groups: Mapped[list] = mapped_column(JSONB, server_default=sa.text("'[]'"))
    forms: Mapped[list] = mapped_column(JSONB, server_default=sa.text("'[]'"))
    #: Своё у источника: у долгов - сторона, основание, группа; у «Не выставлены» - отбор юрлица и отдела.
    params: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    pin: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'all'"))
    position: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    #: Шаблон, из которого вид сохранён; пусто - свой.
    template: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


__all__ = [
    "CoreSettings",
    "DOCUMENT_KINDS",
    "DOCUMENT_SOURCES",
    "Document",
    "EntityTaxPeriod",
    "FACT_KINDS",
    "FACT_SOURCES",
    "Fact",
    "FactInput",
    "RegisterAccrual",
    "RegisterContract",
    "RegisterMonth",
    "RegisterState",
    "TAX_REGIMES",
    "TaxYear",
    "VIEW_PINS",
    "VIEW_SOURCES",
    "View",
]
