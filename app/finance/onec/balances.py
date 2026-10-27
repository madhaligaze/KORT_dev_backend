"""Сальдо по 1210 из базы 1С по REST: долги покупателей на первое число по договорам.

У компании с 1С в интернете ОСВ выгружать не нужно: остатки счёта 1210 на
дату отдаёт сам регистр бухгалтерии - виртуальная таблица остатков
`AccountingRegister_<план>/Balance(Period=…)`, по субконто «Контрагенты»
и «Договоры». Остаток на `Period` - на начало этого дня, без его движений:
то, что KORT берёт входящим сальдо на начало учёта.

Результат - та же `Osv`, что у ведомости файлом (`onec/osv.py`): договоры
с подписью 1С, номером и датой, сальдо дебет минус кредит (аванс - минус,
полигон С3). Дальше шаги «из 1С» не различают, откуда сальдо.

Счёт - по коду из плана счетов (`ChartOfAccounts_Типовой` в Бухгалтерии для
Казахстана, `…_Хозрасчетный` в российской), организация - базы. 1С не
трогается: только GET.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.finance.onec.client import Client, OnecError, OnecMissing
from app.finance.onec.osv import Line, Osv, contract_of
from app.finance.onec.sync import EMPTY_REF, _date, _money

ACCOUNT = "1210"
#: Имя плана счетов и регистра бухгалтерии: «Типовой» - Бухгалтерия для
#: Казахстана (1С:Fresh BBC, 27.10.2026), «Хозрасчетный» - российская линейка.
CHARTS = ("Типовой", "Хозрасчетный")
#: Счёт авансов покупателей рядом со счётом их долга: (план, счёт долга) → счёт авансов.
ADVANCES = {("Типовой", "1210"): "3510", ("Хозрасчетный", "62.01"): "62.02"}
PARTY_TYPE = "StandardODATA.Catalog_Контрагенты"
CONTRACT_TYPE = "StandardODATA.Catalog_ДоговорыКонтрагентов"
_ZERO = Decimal("0")


#: Ресурсы остатка, которые не сумма в тенге: рядом с `СуммаBalanceDr` лежат
#: `ВалютнаяСуммаBalanceDr`, `КоличествоBalanceDr`, `СуммаSplittedBalanceDr`.
_NOT_SUM = ("Валютн", "Количеств", "Splitted")


def _side(row: dict[str, Any], suffix: str) -> Decimal:
    """Ресурс остатка в тенге: `СуммаBalanceDr`; другое имя ресурса - единственный подходящий."""
    exact = row.get("Сумма" + suffix)
    if exact is not None:
        return _money(exact) or _ZERO
    for key, value in row.items():
        if key.endswith(suffix) and not any(word in key for word in _NOT_SUM):
            money = _money(value)
            if money is not None:
                return money
    return _ZERO


def account_ref(client: Client, code: str = ACCOUNT, *, chart: str | None = None) -> tuple[str, str] | None:
    """(имя плана счетов, ключ счёта): план ищется среди `CHARTS`, какой открыт в базе.

    `chart` - план уже известен; счёта в нём нет - `None` (необязательный счёт авансов).
    """
    missing: OnecMissing | None = None
    for name in (chart,) if chart else CHARTS:
        try:
            rows = list(client.rows(f"ChartOfAccounts_{name}", select="Ref_Key,Code", where=f"Code eq '{code}'", order=""))
        except OnecMissing as exc:
            missing = exc
            continue
        if not rows:
            if chart:
                return None
            raise OnecError(f"В плане счетов базы нет счёта {code}")
        return name, str(rows[0]["Ref_Key"])
    raise missing or OnecError("В базе нет плана счетов")


def _balance_rows(client: Client, chart: str, account: str, organization_ref: str, day: date) -> list[dict[str, Any]]:
    path = (
        f"AccountingRegister_{chart}/Balance(Period=datetime'{day:%Y-%m-%d}T00:00:00',"
        f"AccountCondition='Account_Key eq guid''{account}''',"
        f"Condition='Организация_Key eq guid''{organization_ref}''')"
    )
    return list(client.rows(path, order=""))


def read(client: Client, *, organization_ref: str, day: date, code: str = ACCOUNT) -> Osv:
    """Расчёты с покупателями на начало дня `day` по контрагентам и договорам - как ОСВ.

    В плане счетов Казахстана долг покупателя - счёт 1210, а его предоплата -
    отдельный счёт 3510 «Краткосрочные авансы полученные»: в живой 1С:Fresh BBC
    на 28.10.2026 по 1210 +14 077 000, по 3510 −19 064 000, и только их сумма
    равна АВР минус поступления. Поэтому с 1210 читаются и авансы того же
    договора (`ADVANCES`): договор с предоплатой - минус, как кредит в ОСВ.
    """
    try:
        found = account_ref(client, code)
        assert found is not None
        chart, account = found
        rows = _balance_rows(client, chart, account, organization_ref, day)
        codes = [code]
        advance = ADVANCES.get((chart, code))
        extra = account_ref(client, advance, chart=chart) if advance else None
        if extra is not None:
            rows += _balance_rows(client, chart, extra[1], organization_ref, day)
            codes.append(advance)
    except OnecMissing as exc:
        raise OnecError(
            f"Остатки 1С по REST недоступны ({exc}). Откройте в составе REST-сервиса «Журнал проводок» и план счетов "
            "или загрузите ОСВ по 1210 файлом"
        ) from exc
    parties = {
        row["Ref_Key"]: str(row.get("Description") or "").strip()
        for row in client.rows("Catalog_Контрагенты", select="Ref_Key,Description,IsFolder", where="IsFolder eq false")
    }
    contracts = {
        row["Ref_Key"]: row
        for row in client.rows(
            "Catalog_ДоговорыКонтрагентов", select="Ref_Key,НомерДоговора,ДатаДоговора,Description,IsFolder",
            where="IsFolder eq false",
        )
    }
    # Третье субконто (документ расчётов) дробит договор на строки - здесь они складываются.
    sums: dict[tuple[str, str], Decimal] = defaultdict(lambda: _ZERO)
    for row in rows:
        party = contract = ""
        for index in (1, 2, 3):
            kind = row.get(f"ExtDimension{index}_Type")
            value = str(row.get(f"ExtDimension{index}") or "")
            if kind == PARTY_TYPE:
                party = value
            elif kind == CONTRACT_TYPE:
                contract = value
        debit, credit = _side(row, "BalanceDr"), _side(row, "BalanceCr")
        if not debit and not credit:
            whole = row.get("СуммаBalance")
            debit = (_money(whole) if whole is not None else None) or _ZERO
        sums[(party, contract)] += debit - credit
    end = date(day.year + (day.month == 12), day.month % 12 + 1, 1) - timedelta(days=1)
    out = Osv(file=f"1С по REST, счета {', '.join(codes)}", sheet="", start=day, end=end, account=code)
    for number, ((party_ref, contract_ref), opening) in enumerate(sorted(sums.items(), key=lambda item: (parties.get(item[0][0], ""), item[0][1]))):
        if not opening:
            continue
        item = contracts.get(contract_ref) if contract_ref and contract_ref != EMPTY_REF else None
        label = str((item or {}).get("Description") or "").strip()
        number_text = str((item or {}).get("НомерДоговора") or "").strip()
        signed = _date((item or {}).get("ДатаДоговора"))
        if not number_text:
            number_text, parsed = contract_of(label)
            signed = signed or parsed
        out.lines.append(Line(
            party=parties.get(party_ref, ""), contract=label or "Основной договор", number=number_text, signed=signed,
            opening=opening, debit=_ZERO, credit=_ZERO, closing=_ZERO, line=number + 1,
        ))
        out.opening_total += opening
    if not out.lines:
        out.problems.append(f"На {day:%d.%m.%Y} по счёту {code} у организации базы остатков нет")
    if any(not line.party for line in out.lines):
        out.problems.append("У части остатков нет контрагента - их сальдо ляжет только после ответа человека")
    return out


__all__ = ["ACCOUNT", "ADVANCES", "CHARTS", "account_ref", "read"]
