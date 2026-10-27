"""Сальдо по 1210 из базы 1С по REST: долги покупателей на первое число по договорам.

У компании с 1С в интернете ОСВ выгружать не нужно: остатки счёта 1210 на
дату отдаёт сам регистр бухгалтерии - виртуальная таблица остатков
`AccountingRegister_Хозрасчетный/Balance(Period=…)`, по субконто «Контрагенты»
и «Договоры». Остаток на `Period` - на начало этого дня, без его движений:
то, что KORT берёт входящим сальдо на начало учёта.

Результат - та же `Osv`, что у ведомости файлом (`onec/osv.py`): договоры
с подписью 1С, номером и датой, сальдо дебет минус кредит (аванс - минус,
полигон С3). Дальше шаги «из 1С» не различают, откуда сальдо.

Счёт - по коду из плана счетов (`ChartOfAccounts_Хозрасчетный`), организация -
базы. 1С не трогается: только GET.
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
REGISTER = "AccountingRegister_Хозрасчетный"
CHART = "ChartOfAccounts_Хозрасчетный"
PARTY_TYPE = "StandardODATA.Catalog_Контрагенты"
CONTRACT_TYPE = "StandardODATA.Catalog_ДоговорыКонтрагентов"
_ZERO = Decimal("0")


def _side(row: dict[str, Any], suffix: str) -> Decimal:
    """Ресурс остатка: `СуммаBalanceDr` - имя ресурса у конфигураций разное, суффикс - один."""
    for key, value in row.items():
        if key.endswith(suffix):
            money = _money(value)
            if money is not None:
                return money
    return _ZERO


def account_ref(client: Client, code: str = ACCOUNT) -> str:
    rows = list(client.rows(CHART, select="Ref_Key,Code", where=f"Code eq '{code}'", order=""))
    if not rows:
        raise OnecError(f"В плане счетов базы нет счёта {code}")
    return str(rows[0]["Ref_Key"])


def read(client: Client, *, organization_ref: str, day: date, code: str = ACCOUNT) -> Osv:
    """Остатки счёта `code` на начало дня `day` по контрагентам и договорам - как ОСВ."""
    try:
        account = account_ref(client, code)
        path = (
            f"{REGISTER}/Balance(Period=datetime'{day:%Y-%m-%d}T00:00:00',"
            f"AccountCondition='Account_Key eq guid''{account}''',"
            f"Condition='Организация_Key eq guid''{organization_ref}''')"
        )
        rows = list(client.rows(path, order=""))
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
            whole = next((_money(value) for key, value in row.items() if key.endswith("Balance")), None)
            debit = whole or _ZERO
        sums[(party, contract)] += debit - credit
    end = date(day.year + (day.month == 12), day.month % 12 + 1, 1) - timedelta(days=1)
    out = Osv(file=f"1С по REST, счёт {code}", sheet="", start=day, end=end, account=code)
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


__all__ = ["ACCOUNT", "account_ref", "read"]
