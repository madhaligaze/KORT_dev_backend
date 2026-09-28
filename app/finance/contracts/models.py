"""Модель реестра договоров.

Что здесь правда, а что производное
───────────────────────────────────
**Правда** — договоры, их стороны, соглашения, ответственные, справочники
реестра (поля, значения списков, листы-отборы), наши юрлица и псевдонимы
контрагентов.

**Производное** — принадлежность договора к листам и замечания к нему. Они не
хранятся: считаются при чтении. Храни мы «договор лежит в листе „Аренда“»
колонкой, правка правила листа оставляла бы старые метки, и лист показывал бы
не то, что написано в его правиле. Ровно это случилось с ручными копиями в
Excel: копии разъехались с «Сводной», и заметить это было нечем.

Четыре решения, которые стоит объяснить
───────────────────────────────────────
**1. Две стороны — два слота, а не «наше юрлицо + контрагент».** Исполнитель и
заказчик; каждая сторона — строка `counterparties`, а наше юрлицо — контрагент
с паспортом (`GroupEntity`). Направление выводится: наш исполнитель —
продажа, наш заказчик — закупка, оба наши — оборот внутри группы. Подписи
слотов (Продавец, Арендодатель, Займодавец) задаёт вид договора или блок
листа, а не отдельные поля: у реестра BBC в одном листе «Заказчик ГК» соседние
блоки держат стороны в колонках наоборот.

**2. Договор хранит текущие значения, соглашения — историю с датами.** Перевод
клиента на другое ТОО — это соглашение «исполнитель с даты», а не перезапись
ячейки: иначе история стирается, и порог НДС старого юрлица считается по
новому. Соглашение с датой в будущем текущее значение не меняет; его
применяет фоновая задача в свой день.

**3. Номер договора не уникален.** В реестре BBC один номер стоит у разных
клиентов больше десяти раз — это данные, а не ошибка ввода. Уникальность
сломала бы загрузку; вместо неё — замечание «номер уже есть у другого
контрагента», которое человек может отметить «так и должно быть».

**4. Конфликт правок — по полю, а не по записи.** `field_seq` помнит номер
последнего изменения каждого поля. Двое, правящие разные поля одной карточки,
друг другу не мешают; 409 получает только тот, чьё поле успели поменять.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.finance.db import FinanceBase
from app.finance.models import JSONB, MONEY, _in

#: Как начислять по договору. `terms` — условие текстом («20% по разовым»):
#: график по такому договору не ставится никогда.
BILLING_KINDS = ("month", "total", "terms")
#: Смысл даты окончания. Одна колонка файла «дата расторжения / дата
#: исполнения разового» держит два смысла; какой — хранится явно.
END_KINDS = ("terminated", "fulfilled", "unknown")
#: Системный смысл значения «хозяйственный смысл» — что договор делает с
#: нашим юрлицом. Порог НДС считает только `revenue`.
ECONOMIC_ROLES = ("revenue", "expense", "financing", "intra_group")
#: Фаза договора — системный смысл значения статуса.
STATUS_PHASES = ("draft", "active", "in_progress", "suspended", "fulfilled", "terminated", "failed")
CONTRACT_SOURCES = ("app", "grid", "import", "api")
AMENDMENT_EFFECTS = ("none", "amount", "executor", "customer", "end_date", "other")
#: `change` — «изменение с даты» из листа или карточки; `parsed` — кусок
#: текста соглашений из файла, подтверждённый человеком.
AMENDMENT_ORIGINS = ("change", "parsed")
ALIAS_SOURCES = ("registry", "bank", "1c", "manual")
FIELD_TYPES = (
    "text", "number", "money", "date", "bool", "list", "multi_list",
    "url", "person", "party", "department", "choice",
)
#: Как поле заполняют руками — в листе, карточке, через API. `list` — только
#: из списка; `hint` — из списка или своё (новое значение заводится само);
#: `own` — сторона только из наших юрлиц. Пусто — по умолчанию поля
#: (`fields.fill_of`). Загрузку Excel настройка не ограничивает: у файла свой
#: протокол, где незнакомое значение решает человек.
FILLS = ("list", "hint", "own")
CONTRACT_IMPORT_STATUSES = ("preview", "applied", "cancelled")


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class GroupEntity(FinanceBase):
    """Наше юрлицо: контрагент с паспортом.

    Отдельной таблицей, а не флагом на контрагенте: у юрлица группы свои
    реквизиты (код, БИН, плательщик ли НДС), и расчётные счета ссылаются на
    него, а не на «контрагента вообще».
    """

    __tablename__ = "group_entities"

    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("counterparties.id", ondelete="CASCADE"), primary_key=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    #: Короткое имя, которым юрлицо называют в книгах: BBC, BBCL, EA.
    code: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    full_name: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    bin: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    vat_payer: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    position: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class CounterpartyName(FinanceBase):
    """Другое написание контрагента: «ТОО «Атриум плюс»», «АТРИУМ ПЛЮС».

    Реестр ФО держал колонки «По выписке банка» и «По 1С» ровно для этого —
    чтобы свести руками одного клиента, записанного тремя способами. Здесь
    написание, подтверждённое человеком, запоминается, и в следующий раз
    сводит система.
    """

    __tablename__ = "counterparty_names"
    __table_args__ = (
        sa.CheckConstraint(_in("source", ALIAS_SOURCES), name="counterparty_name_source"),
        sa.UniqueConstraint("counterparty_id", "normalized", name="uq_counterparty_names_alias"),
        sa.Index("ix_counterparty_names_workspace_normalized", "workspace_id", "normalized"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("counterparties.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(sa.Text)
    normalized: Mapped[str] = mapped_column(sa.Text)
    source: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'manual'"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class Department(FinanceBase):
    """Отдел компании: ЮО, ОБО, НО, HR, ФО. Им пользуются договоры и права."""

    __tablename__ = "departments"
    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "normalized_name", name="uq_departments_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    #: Код, которым отдел пишут в книгах. По нему же сравнивается написание.
    code: Mapped[str] = mapped_column(sa.Text)
    normalized_name: Mapped[str] = mapped_column(sa.Text)
    title: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    position: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Employee(FinanceBase):
    """Человек компании. Учётка — только у тех, кому дан вход.

    Ответственный в договоре не обязан входить в систему: в реестре BBC
    ответственные — это «Ануар», «Нурболат», и половина из них учёт не ведёт.
    Поэтому человек — отдельная запись, а связь с учёткой необязательна.
    """

    __tablename__ = "employees"
    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "normalized_name", name="uq_employees_name"),
        # Одна учётка — один человек компании: права сотрудника пишутся на эту
        # запись, и две записи на одну учётку давали бы два набора прав.
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_employees_workspace_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    full_name: Mapped[str] = mapped_column(sa.Text)
    normalized_name: Mapped[str] = mapped_column(sa.Text)
    job_title: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("departments.id", ondelete="SET NULL")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    position: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class ListValue(FinanceBase):
    """Значение списка: статус, вид, предмет, хозяйственный смысл, своё поле.

    `meaning` — что значение значит для системы: у статуса — фаза или
    «передача бухгалтеру», у вида — способ начисления, смысл и подписи
    сторон, у предмета — отдел, у хозяйственного смысла — системный смысл.
    Пустой `meaning` законен: незнакомый статус из файла сохраняется как
    написан, смысл ему назначают потом.
    """

    __tablename__ = "list_values"
    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id", "field_key", "normalized", name="uq_list_values_value"
        ),
        sa.Index("ix_list_values_workspace_field", "workspace_id", "field_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    field_key: Mapped[str] = mapped_column(sa.Text)
    value: Mapped[str] = mapped_column(sa.Text)
    normalized: Mapped[str] = mapped_column(sa.Text)
    meaning: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    position: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class EntityField(FinanceBase):
    """Колонка сущности: системная или своя.

    Один список управляет листом, карточкой, разбором файла, выгрузкой и
    правами на поля. Системное поле можно переименовать и спрятать, но не
    удалить и не сменить ему тип: на нём держатся начисления и долги.
    `names` — написания шапки, по которым загрузка узнаёт колонку;
    подтверждённые человеком дописываются.
    """

    __tablename__ = "entity_fields"
    __table_args__ = (
        sa.CheckConstraint(_in("type", FIELD_TYPES), name="entity_field_type"),
        sa.UniqueConstraint("workspace_id", "entity", "key", name="uq_entity_fields_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    entity: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'contract'"))
    key: Mapped[str] = mapped_column(sa.Text)
    system: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    type: Mapped[str] = mapped_column(sa.Text)
    title: Mapped[str] = mapped_column(sa.Text)
    names: Mapped[list] = mapped_column(JSONB, server_default=sa.text("'[]'"))
    required: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    hidden: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    #: Способ заполнения (`FILLS`); пусто — умолчание поля.
    fill: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    position: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class EntityView(FinanceBase):
    """Лист-отбор реестра: «Сводная», «Исполнитель ГК», «Прочие договоры».

    Лист ничего не хранит, он показывает: договор стоит в листе, если подходит
    под правило одного из его блоков. `blocks` — список
    `{title, filter, roles, columns, defaults}`; правило `filter` — группы
    условий «и», соединённые «или». У блока своя шапка и свой порядок колонок:
    в «Заказчик ГК / Заказчик ГК» заказчик стоит в F, исполнитель в G.
    """

    __tablename__ = "entity_views"
    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "entity", "key", name="uq_entity_views_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    entity: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'contract'"))
    #: Постоянный ключ листа для адресов и ответа `views[]`; название меняют.
    key: Mapped[str] = mapped_column(sa.Text)
    title: Mapped[str] = mapped_column(sa.Text)
    main: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    #: Книга, в которой лист: `""` — реестр (карточки и «Таблица»), `oneoff` —
    #: «Разовые». Договоры одни и те же, книги — разные наборы листов: у
    #: финансистов разовые услуги жили отдельной книгой со своими отборами
    #: («до 2 мес», «Остатки»), и в общий реестр эти листы не просятся.
    book: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    blocks: Mapped[list] = mapped_column(JSONB, server_default=sa.text("'[]'"))
    sort: Mapped[list] = mapped_column(JSONB, server_default=sa.text("'[]'"))
    #: Оформление шапки исходного файла — для выгрузки .xlsx.
    style: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    position: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class ContractImport(FinanceBase):
    """Партия загрузки реестра из Excel: разбор, решения человека, заведение.

    Разобранные строки лежат здесь же (`staged`) до заведения: протокол
    разбора показывается целиком, решения копятся, и только «Завести» пишет
    договоры. Файл второй раз не читается.
    """

    __tablename__ = "contract_imports"
    __table_args__ = (
        sa.CheckConstraint(_in("status", CONTRACT_IMPORT_STATUSES), name="contract_import_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    file_name: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    status: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'preview'"))
    report: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    decisions: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    staged: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    applied_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Contract(FinanceBase):
    """Договор. Одна запись на все листы и оба вида — таблицу и карточку."""

    __tablename__ = "contracts"
    __table_args__ = (
        sa.CheckConstraint(
            "billing = '' OR " + _in("billing", BILLING_KINDS), name="contract_billing"
        ),
        sa.CheckConstraint(
            "end_kind = '' OR " + _in("end_kind", END_KINDS), name="contract_end_kind"
        ),
        sa.CheckConstraint(_in("source", CONTRACT_SOURCES), name="contract_source"),
        sa.Index("ix_contracts_workspace_seq", "workspace_id", "seq"),
        sa.Index("ix_contracts_workspace_number_key", "workspace_id", "number_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    #: Как написан в договоре: «№ 17-7-BBC-BUH», «№ЮО/141». Не уникален.
    number: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    #: Номер без пробелов, «№» и регистра — только для замечания «номер уже есть
    #: у другого контрагента».
    number_key: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    signed_at: Mapped[date | None] = mapped_column(sa.Date)
    executor_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("counterparties.id", ondelete="SET NULL")
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("counterparties.id", ondelete="SET NULL")
    )
    type_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("list_values.id", ondelete="SET NULL")
    )
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("list_values.id", ondelete="SET NULL")
    )
    status_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("list_values.id", ondelete="SET NULL")
    )
    economic_role_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("list_values.id", ondelete="SET NULL")
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("departments.id", ondelete="SET NULL")
    )
    billing: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    amount: Mapped[Decimal | None] = mapped_column(MONEY)
    #: Условие суммы текстом, когда числа нет: «20% по разовым, 40% по абон.».
    amount_terms: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    currency: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'KZT'"))
    planned_end_at: Mapped[date | None] = mapped_column(sa.Date)
    end_date: Mapped[date | None] = mapped_column(sa.Date)
    end_kind: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    #: Папка договора (у BBC — Битрикс): открывается кнопкой из карточки.
    folder_url: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    #: Колонки соглашений файла — как есть. В записи с датой раскладывает
    #: только человек, кнопкой «Разобрать».
    amendments_text: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    amendments_summary_text: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    note: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    #: Свои поля компании: ключ поля → значение.
    attrs: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    #: «Как было в файле»: оплачено, остаток, дата и имя файла. Только чтение —
    #: живой остаток появится, когда оплаты начнут находить договор сами.
    file_snapshot: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    #: Откуда значение у начисления и смысла: `type` / `block` / `subject` /
    #: `parties` / `manual`. Для порога НДС «решил человек» и «подставила
    #: система» — разные вещи.
    provenance: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    #: Замечания, отмеченные «так и должно быть»: код → {by, at, ref}.
    acknowledged: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    #: Номер последнего изменения каждого поля — конфликт считается по полю.
    field_seq: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    #: Порядок в реестре: гнёзда по 1024, чтобы вставка не перенумеровывала всё.
    position: Mapped[int] = mapped_column(sa.BigInteger, server_default=sa.text("0"))
    #: Номер последнего изменения договора в компании (см. `counters`).
    seq: Mapped[int] = mapped_column(sa.BigInteger, server_default=sa.text("0"))
    source: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'app'"))
    import_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("contract_imports.id", ondelete="SET NULL")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class ContractPerson(FinanceBase):
    """Ответственный по договору. Их бывает несколько: «Елжас, Тимур»."""

    __tablename__ = "contract_people"

    contract_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("contracts.id", ondelete="CASCADE"), primary_key=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("employees.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    position: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))


class ContractAmendment(FinanceBase):
    """Подтверждённое соглашение к договору.

    Только подтверждённое: кусок текста, который «Разобрать» лишь предложил,
    сюда не пишется, пока человек его не подтвердил. Значение договора на
    прошлый месяц считается только по этим записям.
    """

    __tablename__ = "contract_amendments"
    __table_args__ = (
        sa.CheckConstraint(_in("effect", AMENDMENT_EFFECTS), name="contract_amendment_effect"),
        sa.CheckConstraint(_in("origin", AMENDMENT_ORIGINS), name="contract_amendment_origin"),
        sa.Index("ix_contract_amendments_due", "applied_at", "effective_from"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("contracts.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    signed_at: Mapped[date | None] = mapped_column(sa.Date)
    summary: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    effect: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'none'"))
    effective_from: Mapped[date | None] = mapped_column(sa.Date)
    before: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    after: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    origin: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'change'"))
    #: Кусок исходного текста, из которого соглашение разобрано.
    piece: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    #: Когда значение соглашения стало текущим у договора. Пусто — «впереди».
    applied_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    position: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class ContractPayment(FinanceBase):
    """Решение человека: эта операция журнала — оплата по этому договору.

    Разнесение само по себе не хранится — оно считается при чтении
    (`payments.py`), как принадлежность договора к листам: храни мы «оплата
    лежит в договоре» колонкой, новая выписка или правка договора оставляли бы
    старые метки. Хранится только то, чего система решить не может: у клиента
    два договора с одним нашим ТОО, и платёж подходит к обоим. `contract_id`
    пустой — «ни к одному»: платёж не по договору (возврат, ошибка банка).
    """

    __tablename__ = "contract_payments"
    __table_args__ = (sa.UniqueConstraint("operation_id", name="uq_contract_payments_operation"),)

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("operations.id", ondelete="CASCADE")
    )
    contract_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("contracts.id", ondelete="CASCADE"), index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class SummarySource(FinanceBase):
    """Книга Google, из которой берётся «Оплачено (сводка)» договоров.

    У BBC это «Осн.Общая сводка BBC 2026», лист «Сводка все ЮР лица»: туда
    сходятся книги всех юрлиц, и «Сумма Факт Поступ.» там — факт оплаты. Книга
    юротдела «Разовые» брала «Оплачено» оттуда формулой (`VLOOKUP` по «Заказчик
    + № договора + код фирмы»); здесь — тем же ключом, но по названиям колонок
    и без угадывания (`summary.py`). Одна книга на компанию; нет записи — нет
    колонки «по сводке».
    """

    __tablename__ = "contract_summary_sources"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    spreadsheet_id: Mapped[str] = mapped_column(sa.Text)
    worksheet: Mapped[str] = mapped_column(sa.Text)
    #: Название книги на момент подключения — для подписи «откуда цифры».
    title: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class Counter(FinanceBase):
    """Счётчик компании: номер изменения реестра (`seq`).

    `UPDATE … SET value = value + 1 RETURNING value` держит блокировку строки
    до конца транзакции. Поэтому номера видны строго по порядку: опрос
    «изменения после N» не пропустит правку, чья транзакция закоммитилась
    позже правки с большим номером. Последовательность Postgres такого не
    гарантирует — номер выдаётся раньше коммита.
    """

    __tablename__ = "counters"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    value: Mapped[int] = mapped_column(sa.BigInteger, server_default=sa.text("0"))


__all__ = [
    "AMENDMENT_EFFECTS",
    "AMENDMENT_ORIGINS",
    "ALIAS_SOURCES",
    "BILLING_KINDS",
    "CONTRACT_IMPORT_STATUSES",
    "CONTRACT_SOURCES",
    "Contract",
    "ContractAmendment",
    "ContractImport",
    "ContractPayment",
    "ContractPerson",
    "Counter",
    "CounterpartyName",
    "Department",
    "ECONOMIC_ROLES",
    "END_KINDS",
    "Employee",
    "EntityField",
    "EntityView",
    "FIELD_TYPES",
    "FILLS",
    "GroupEntity",
    "ListValue",
    "STATUS_PHASES",
    "SummarySource",
]
