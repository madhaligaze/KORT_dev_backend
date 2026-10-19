"""Разговор с автоматическим REST-сервисом (OData) базы 1С.

Адрес базы
──────────
Человек вставит то, что видит в браузере: «https://1cfresh.kz/a/ea201/284171/
ru_RU/», ссылку «Начать работу» с `?ProgressiveWebApplicationName=…` или
готовый адрес сервиса. Всё это - одна база, и хранится один вид:
`https://1cfresh.kz/a/ea201/284171`; сервис - `<база>/odata/standard.odata/`.

Только https и только внешний адрес. Сервер KORT стоит во внутренней сети
Railway рядом с базой данных, и адрес, который ввёл человек, не должен
открывать запрос туда (`postgres.railway.internal`, 10.x, 127.0.0.1). Пароль
REST-сервиса уходит заголовком Basic - по http его прочитает любой на пути.

Отказы
──────
1cfresh.kz прячет ошибки 1С за своей страницей: на сбой отвечает
перенаправлением на `/availability/`, а не текстом ошибки. Поэтому
перенаправление здесь не выполняется, а читается как «база недоступна».
"""
from __future__ import annotations

import ipaddress
import socket
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Iterator

import requests

#: Сколько ждать один ответ 1С. Страница в тысячу документов с табличными
#: частями у 1cfresh отвечает за 2–6 секунд.
TIMEOUT = 90
#: Документов на страницу выборки.
PAGE = 1000
#: Повторы при 502/503/504 - у 1cfresh бывают на мгновение при перезапуске.
RETRIES = 2


class OnecError(Exception):
    """Отказ 1С или сети - с текстом для человека."""


class OnecMissing(OnecError):
    """Вида данных нет в составе REST-сервиса базы (или нет прав на него)."""


@dataclass(frozen=True)
class Connection:
    url: str
    login: str
    password: str

    @property
    def root(self) -> str:
        return self.url.rstrip("/") + "/odata/standard.odata/"


_TAILS = ("/odata/", "/ru_ru", "/ru-ru", "/en_us", "/kk_kz", "/e1cib")


def normalize_url(text: str) -> str:
    """Адрес базы из того, что вставил человек (см. описание модуля)."""
    raw = (text or "").strip()
    if not raw:
        raise OnecError("Нужен адрес базы 1С - например, https://1cfresh.kz/a/ea201/284171")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme.lower() != "https":
        raise OnecError("Адрес базы 1С - только https: по http пароль REST-сервиса уйдёт открытым")
    host = (parts.hostname or "").lower()
    if not host:
        raise OnecError("В адресе базы нет сервера")
    path = parts.path or ""
    low = path.lower()
    for tail in _TAILS:
        at = low.find(tail)
        if at >= 0:
            path, low = path[:at], low[:at]
    path = path.rstrip("/")
    port = f":{parts.port}" if parts.port and parts.port != 443 else ""
    return f"https://{host}{port}{path}"


def check_host(url: str) -> None:
    """Сервер базы должен быть во внешней сети (см. описание модуля)."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host in ("localhost",) or host.endswith((".internal", ".local", ".localhost")):
        raise OnecError("Этот адрес не во внешней сети - KORT ходит только на опубликованные базы 1С")
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise OnecError(f"Сервер «{host}» не найден - проверьте адрес базы") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise OnecError("Этот адрес не во внешней сети - KORT ходит только на опубликованные базы 1С")


def _odata_message(response: requests.Response) -> str:
    try:
        error = response.json().get("odata.error") or {}
        return str((error.get("message") or {}).get("value") or "").strip()
    except ValueError:
        return ""


class Client:
    """Чтение из REST-сервиса одной базы."""

    def __init__(self, connection: Connection, *, http: requests.Session | None = None, timeout: int = TIMEOUT):
        self.connection = connection
        self.timeout = timeout
        self.http = http or requests.Session()
        self.http.auth = (connection.login, connection.password)
        self.http.headers.update({"Accept": "application/json"})

    def _url(self, path: str, params: dict[str, str]) -> str:
        query = {"$format": "json", **params}
        return (
            self.connection.root
            + urllib.parse.quote(path, safe="()',=_$/:")
            + "?"
            + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
        )

    def get(self, path: str, **params: str) -> dict[str, Any]:
        url = self._url(path, params)
        response: requests.Response | None = None
        for attempt in range(RETRIES + 1):
            try:
                response = self.http.get(url, timeout=self.timeout, allow_redirects=False)
            except requests.Timeout as exc:
                raise OnecError("1С не ответила вовремя - база занята или недоступна") from exc
            except requests.RequestException as exc:
                raise OnecError(f"1С не отвечает: {exc.__class__.__name__}") from exc
            if response.status_code in (502, 503, 504) and attempt < RETRIES:
                time.sleep(2 * (attempt + 1))
                continue
            break
        assert response is not None
        status = response.status_code
        if status in (301, 302, 303, 307, 308):
            target = response.headers.get("location", "")
            if "availability" in target:
                raise OnecError("1С:Fresh отвечает «приложение недоступно» - база обновляется, выключена или в ней сбой")
            raise OnecError("1С перенаправляет на вход - проверьте адрес базы")
        if status == 401:
            message = _odata_message(response)
            if message and "запрещ" in message.lower():
                raise OnecMissing(f"Нет прав на «{path}» в REST-сервисе базы")
            # 1cfresh отвечает одинаково и на чужой пароль, и на чужой номер
            # базы - пустым 401, поэтому и текст один.
            raise OnecError("1С не пускает: логин, пароль REST-сервиса или адрес базы не подходят")
        if status == 403:
            raise OnecMissing(f"Нет прав на «{path}» в REST-сервисе базы")
        if status == 404:
            message = _odata_message(response)
            if "не найден" in message.lower() and "сущност" in message.lower():
                raise OnecMissing(f"«{path}» не открыт в составе REST-сервиса базы")
            raise OnecError("По этому адресу нет REST-сервиса 1С - проверьте адрес базы и что сервис включён")
        if status >= 400:
            message = _odata_message(response)
            raise OnecError(f"1С ответила ошибкой {status}" + (f": {message}" if message else ""))
        try:
            return response.json()
        except ValueError as exc:
            raise OnecError("1С ответила не данными - проверьте адрес базы") from exc

    def rows(
        self,
        entity: str,
        *,
        select: str = "",
        where: str = "",
        order: str = "Ref_Key",
        page: int = PAGE,
    ) -> Iterator[dict[str, Any]]:
        """Все записи сущности страницами по `page`."""
        skip = 0
        while True:
            params: dict[str, str] = {"$top": str(page), "$skip": str(skip)}
            if select:
                params["$select"] = select
            if where:
                params["$filter"] = where
            if order:
                params["$orderby"] = order
            chunk = self.get(entity, **params).get("value", [])
            yield from chunk
            if len(chunk) < page:
                return
            skip += page


__all__ = ["Client", "Connection", "OnecError", "OnecMissing", "check_host", "normalize_url"]
