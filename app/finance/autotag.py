"""Авторазметка: статья по тексту операции, без сети и без угадывания.

Зачем
─────
Выписка за год - это две тысячи строк без статей. Finmap в этом месте
предлагает платного ИИ-агента; до него человек видит «Без категории» во всех
отчётах. Правила (`rules.py`) решают задачу, но их сначала надо придумать.
Авторазметка - первый проход: то, что по тексту операции ясно наверняка,
размечается сразу, остальное остаётся правилам и человеку.

Почему не таксономия «Анализа выписок»
──────────────────────────────────────
В продукте уже есть разметка продавцов (`app.services.ai_engine`). Её прогнали
21 сентября 2026 по настоящей выписке Kaspi Gold, и для учёта она не годится:
шаблоны там - подстроки, поэтому «Перевод · Аяна Ж.» уходил в «Супермаркеты»
(шаблон «аян»), «Магазин Окей» и «Нургазы А.» - в ЖКХ («газ»), «ИП
Назаралиева» - в одежду («зара»), «Банк ЦентрКредит» - в страхование
(«кредит»). Всё это с уверенностью 0.92 и без единого признака ошибки. Для
графика трат это шум, для учёта - неверная цифра в отчёте о прибыли. А самые
крупные продавцы выписки - Anytime, Yandex Go, Jet - уходили в безликое
«Покупки».

Поэтому здесь своя, короче и строже:

* **целые слова**, а не подстроки - слово обязано начинаться с границы;
* **тип операции банка первым**: «Снятие», «Перевод», «Пополнение», «Покупка»
  печатает сам банк, это не догадка, и статья не может ему противоречить -
  покупка не станет переводом, поступление не станет супермаркетом;
* **деловые статьи раньше бытовых**: налоги, зарплата, аренда - то, что
  встречается в выписке юр. счёта, - и названия ровно те, что заводятся в
  новой компании, чтобы разметка не плодила двойников «Налоги» / «Налоги и
  сборы»;
* **широкие группы помечены**: «Покупки без уточнения» правда, но ничего не
  говорит; такие группы экран предлагает, но не отмечает сам.

Размечается только неразмеченное. Одна загрузка разметки - одна запись в
истории, и отменяется она целиком.
"""
from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.layout import norm
from app.finance import service
from app.finance.models import Counterparty, Operation, Workspace

#: Буквы для границ слова: кириллица с казахскими, латиница, цифры. Шва - в
#: двух видах: Kaspi печатает имена то кириллической «Ә», то латинской «Ə»
#: («Əділет О.»), и без второй такое имя не узнавалось как имя человека.
_LETTER = r"0-9A-Za-zА-Яа-яЁёӘәƏəҒғҚқҢңӨөҰұҮүҺһІі"


def _words(*terms: str, whole: bool = False) -> re.Pattern[str]:
    """Шаблон из слов: начало слова обязательно, конец - если `whole`.

    Основы вроде «аптек» совпадают с «аптека», «аптеки», но не с серединой
    чужого слова. Короткие названия (`zara`, `activ`, `small`) - только целым
    словом: «Назаралиева» не магазин одежды.
    """
    tail = rf"(?![{_LETTER}])" if whole else ""
    body = "|".join(terms)
    return re.compile(rf"(?<![{_LETTER}])(?:{body}){tail}", re.IGNORECASE)


@dataclass(frozen=True)
class Rule:
    category: str
    side: str  # income | expense
    pattern: re.Pattern[str]
    #: Широкая группа: правдива, но почти ничего не сообщает об операции.
    broad: bool = False


# ── Деловые статьи: выписки юр. счетов ──────────────────────────────────────
#
# Названия совпадают с начальными статьями компании (`service.SEED_CATEGORIES`):
# разметка ложится в уже заведённую статью, а не рядом с ней.
BUSINESS: tuple[Rule, ...] = (
    Rule("Налоги и сборы", "expense", _words("налог", r"соц\.? ?отчисл", "соцплат", "бюджет", "госпошлин")),
    Rule("Налоги и сборы", "expense", _words(
        "ипн", "кпн", "ндс", "опв", "оппв", "осмс", "восмс", "кгд", "угд", "пеня", "пени", whole=True,
    )),
    Rule("Зарплата", "expense", _words("заработн", "зарплат", "з/п", "оплата труда")),
    Rule("Аренда", "expense", _words("аренд")),
    Rule("Комиссии банка", "expense", _words(
        "комисси", "за ведение сч", "обслуживание сч", "годовое обслуживание", "sms-информ", "смс-информ",
    )),
)

# ── Покупки: продавцы по названию ───────────────────────────────────────────
#
# Порядок значим: первое совпадение выигрывает. Доставка раньше такси - «Bolt
# Food» это еда, а не поездка.
PURCHASES: tuple[Rule, ...] = (
    Rule("Доставка еды", "expense", _words(
        "wolt", "glovo", "chocofood", r"yandex\.?\s?eda", r"яндекс\.?\s?еда", "yandex eats", "bolt food",
    )),
    Rule("Такси и каршеринг", "expense", _words(
        "anytime", r"yandex\.?\s?go", r"яндекс\.?\s?go", r"yandex\.?\s?taxi", r"яндекс\s?такси",
        "indrive", "jet sharing", "whoosh", "такси", "taxi", "carsharing", "каршеринг",
    )),
    Rule("Такси и каршеринг", "expense", _words("uber", "bolt", whole=True)),
    Rule("Общественный транспорт", "expense", _words(
        "onay", "avtobys", "metro almaty", "метрополитен", "оплата проезда", "автобус",
    )),
    Rule("Топливо", "expense", _words(
        "азс", "azs", "petrol", "helios", "гелиос", "sinooil", "qazaq oil", "kazmunaygas", "казмунайгаз",
        "gazprom", "газпром", "lukoil", "лукойл", "бензин",
    )),
    Rule("Топливо", "expense", _words("shell", whole=True)),
    Rule("Продукты", "expense", _words(
        "magnum", "магнум", "galmart", "галмарт", "anvar", "анвар", "arbuz", "арбуз", "metro cash",
        "продуктов", "супермаркет", "supermarket", "гипермаркет", "hypermarket",
    )),
    Rule("Продукты", "expense", _words("small", "смол", "spar", "спар", "okey", "окей", whole=True)),
    Rule("Кафе и рестораны", "expense", _words(
        "ресторан", "restaurant", "кофейн", "шаурм", "пицц", "столов", "starbucks", "mcdonald", "burger king",
    )),
    Rule("Кафе и рестораны", "expense", _words(
        "кафе", "cafe", "coffee", "кофе", "kfc", "doner", "донер", "суши", "sushi", "бистро", "bistro", "pizza",
        whole=True,
    )),
    Rule("Аптеки и медицина", "expense", _words(
        "аптек", "apteka", "pharm", "фармац", "клиник", "clinic", "стоматолог", "dental", "invivo", "инвиво",
        "медцентр", "медицинск",
    )),
    Rule("Связь и интернет", "expense", _words(
        "tele2", "теле2", "beeline", "билайн", "kcell", "кселл", "altel", "алтел", "казахтелеком",
        "kazakhtelecom", "activ",
        whole=True,
    )),
    Rule("Коммунальные услуги", "expense", _words(
        "алсеко", "alseco", "энергосбыт", "водоканал", "коммунал", "теплосет",
    )),
    Rule("Коммунальные услуги", "expense", _words("ерц", "жкх", "осмд", "кск", whole=True)),
    Rule("Маркетплейсы", "expense", _words(
        "kaspi magazin", "kaspi магазин", "wildberries", "ozon", "aliexpress", "lamoda", "temu", "shein",
    )),
    Rule("Электроника", "expense", _words(
        "sulpak", "сулпак", "technodom", "технодом", "alser", "алсер", "mechta", "мечта", "dns",
        whole=True,
    )),
    Rule("Одежда и обувь", "expense", _words(
        "zara", "lc waikiki", "h&m", "adidas", "nike", "puma", "uniqlo", "reserved", "bershka",
        "stradivarius", "обувь", "одежда",
        whole=True,
    )),
    Rule("Кино и развлечения", "expense", _words(
        "cinema", "кинотеатр", "chaplin", "kinopark", "кинопарк", "netflix", "spotify", "steam", "ivi",
        whole=True,
    )),
)

#: Природа статей, которые разметка заводит сама.
#:
#: Новая статья получает природу по умолчанию - «операционная», а для
#: поступлений это выручка. Без этого словаря «Внесение наличных» и «Переводы
#: от людей» после разметки считались выручкой в «Показателях», и маржа
#: считалась от собственных же денег. Природу существующей статьи разметка не
#: трогает: её мог выбрать человек.
NATURES: dict[tuple[str, str], str] = {
    ("income", "Внесение наличных"): "capital",
    ("income", "Переводы от людей"): "other",
    ("income", "Пополнение с других карт"): "other",
    ("income", "Пенсии и пособия"): "other",
    ("income", "Возвраты покупок"): "other",
    ("income", "Прочие поступления"): "other",
    ("expense", "Снятие наличных"): "capital",
    ("expense", "Комиссии банка"): "financial",
    ("expense", "Налоги и сборы"): "tax",
}

#: Имя человека так, как его печатает Kaspi: «Тофиг Т.», «Ергалий С.»,
#: «Fotima Abdubakirovna T.» - одно-три слова и инициал в конце.
_PERSON = re.compile(rf"^(?:[{_LETTER}][{_LETTER}\-]+\s+){{1,3}}[{_LETTER}]\.?$")


@dataclass(frozen=True)
class Verdict:
    category: str
    side: str
    broad: bool
    #: Чем доказано - для экрана: «тип операции банка», «продавец», «назначение».
    reason: str


def _split(comment: str) -> tuple[str, str]:
    """«Покупка · Magnum Cash&Carry» → («покупка», «Magnum Cash&Carry»)."""
    text = (comment or "").strip()
    if " · " in text:
        head, detail = text.split(" · ", 1)
        return norm(head), detail.strip()
    return "", text


def classify(comment: str, kind: str, counterparty: str = "") -> Verdict | None:
    """Статья для операции - или `None`, если по тексту она не ясна.

    `kind` - вид операции в учёте (income / expense). Статья с другой стороны
    учёта не возвращается никогда: это и есть противоречие типу операции.
    """
    if kind not in ("income", "expense"):
        return None
    operation, detail = _split(comment)
    text = " ".join(part for part in (detail, counterparty) if part)
    whole = " ".join(part for part in (comment, counterparty) if part)

    # Деловые статьи - по всему тексту: назначение платежа юр. счёта идёт одной
    # строкой без «операции». Только для списаний: налог или зарплата,
    # пришедшие НА счёт, - это возврат, и угадывать его статью нельзя.
    if kind == "expense":
        for rule in BUSINESS:
            if rule.pattern.search(whole):
                return Verdict(rule.category, rule.side, rule.broad, "назначение платежа")

    if kind == "expense":
        if operation == "снятие":
            return Verdict("Снятие наличных", "expense", False, "тип операции банка")
        if operation == "перевод":
            if _PERSON.match(detail):
                return Verdict("Переводы людям", "expense", False, "тип операции банка")
            return Verdict("Переводы", "expense", True, "тип операции банка")
        if operation in ("покупка", ""):
            for rule in PURCHASES:
                if rule.pattern.search(text):
                    return Verdict(rule.category, rule.side, rule.broad, "продавец")
            if operation == "покупка":
                return Verdict("Покупки без уточнения", "expense", True, "тип операции банка")
            return None
        if operation == "разное":
            return Verdict("Прочие списания", "expense", True, "тип операции банка")
        return None

    # Поступления
    if operation == "покупка":
        # Покупка со знаком плюс - это возврат денег за покупку, а не доход.
        return Verdict("Возвраты покупок", "income", False, "тип операции банка")
    if operation == "пополнение":
        low = norm(detail)
        if "банкомат" in low or "терминал" in low:
            return Verdict("Внесение наличных", "income", False, "тип операции банка")
        if "пенси" in low or "пособи" in low:
            return Verdict("Пенсии и пособия", "income", False, "тип операции банка")
        if _PERSON.match(detail):
            return Verdict("Переводы от людей", "income", False, "тип операции банка")
        if "другого банка" in low or "с карты" in low:
            return Verdict("Пополнение с других карт", "income", False, "тип операции банка")
        return Verdict("Прочие поступления", "income", True, "тип операции банка")
    return None


# ── Работа с учётом ─────────────────────────────────────────────────────────


def _candidates(session: Session, workspace_id: uuid.UUID) -> Iterable[tuple[Operation, str]]:
    """Неразмеченные доходы и расходы с именем контрагента, если он есть."""
    parties = dict(
        session.execute(
            sa.select(Counterparty.id, Counterparty.name).where(Counterparty.workspace_id == workspace_id)
        ).all()
    )
    rows = session.scalars(
        sa.select(Operation).where(
            Operation.workspace_id == workspace_id,
            Operation.deleted_at.is_(None),
            Operation.category_id.is_(None),
            Operation.kind.in_(("income", "expense")),
        )
    )
    for operation in rows:
        yield operation, parties.get(operation.counterparty_id, "")


def preview(session: Session, workspace: Workspace) -> dict[str, Any]:
    """Что разметится: группы по статьям, с суммой и примерами. Ничего не пишет."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    uncategorized = 0
    for operation, party in _candidates(session, workspace.id):
        uncategorized += 1
        verdict = classify(operation.comment or "", operation.kind, party)
        if verdict is None:
            continue
        key = (verdict.side, verdict.category)
        group = groups.setdefault(
            key,
            {
                "category": verdict.category,
                "side": verdict.side,
                "broad": verdict.broad,
                "reason": verdict.reason,
                "count": 0,
                "amount": Decimal("0"),
                "examples": [],
            },
        )
        group["count"] += 1
        group["amount"] += Decimal(str(operation.amount_base))
        if len(group["examples"]) < 3 and operation.comment and operation.comment not in group["examples"]:
            group["examples"].append(operation.comment)

    existing = {
        (item.side, norm(item.name)) for item in service.list_categories(session, workspace.id)
    }
    items = sorted(groups.values(), key=lambda item: (item["broad"], -item["amount"]))
    for item in items:
        item["exists"] = (item["side"], norm(item["category"])) in existing
        item["amount"] = str(item["amount"])
    covered = sum(item["count"] for item in items if not item["broad"])
    return {"uncategorized": uncategorized, "covered": covered, "groups": items}


def apply(
    session: Session, workspace: Workspace, chosen: Sequence[tuple[str, str]]
) -> dict[str, Any]:
    """Разметить неразмеченное по выбранным группам `(сторона, статья)`.

    Возвращает, сколько размечено, и пары `(операция, статья)` - их пишет в
    историю маршрут, чтобы разметку можно было отменить целиком.
    """
    wanted = {(side, norm(category)) for side, category in chosen}
    if not wanted:
        raise service.FinanceError("Не выбрано ни одной группы")
    categories: dict[tuple[str, str], Any] = {}
    items: list[list[str]] = []
    by_category: defaultdict[str, int] = defaultdict(int)
    for operation, party in list(_candidates(session, workspace.id)):
        verdict = classify(operation.comment or "", operation.kind, party)
        if verdict is None or (verdict.side, norm(verdict.category)) not in wanted:
            continue
        key = (verdict.side, verdict.category)
        if key not in categories:
            existing = service.ensure_category(
                session, workspace.id, verdict.side, verdict.category, create=False
            )
            category = existing or service.ensure_category(
                session, workspace.id, verdict.side, verdict.category
            )
            if existing is None and key in NATURES:
                category.nature = NATURES[key]
            categories[key] = category
        category = categories[key]
        operation.category_id = category.id
        operation.version += 1
        items.append([str(operation.id), str(category.id)])
        by_category[verdict.category] += 1
    session.flush()
    return {"updated": len(items), "by_category": dict(by_category), "items": items}


__all__ = ["BUSINESS", "PURCHASES", "Verdict", "apply", "classify", "preview"]
