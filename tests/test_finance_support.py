"""Поддержка KORT: обращение - в канал Telegram, ответ - обратно в KORT.

Telegram в тестах подменён (`FakeBot`): проверяется, что и в каком порядке
бот отправляет, а не сеть. Набор держит обещания поддержки:

* сообщение с картинками, файлом и голосовым ложится в базу и уходит в канал
  шапкой с кнопкой «Отправить ответ» и вложениями ответами на неё; голосовое
  из Chrome (WebM) - голосовым Telegram (Ogg);
* Telegram промолчал на середине - повтор не шлёт второй раз уже отправленное;
* «Отправить ответ» + сообщение в личку бота - ответ у человека, непрочитанный,
  с началом текста для напоминания; повтор события не задваивает ответ;
  альбом - одно сообщение; без открытого ответа личка никуда не пишет;
* чужую переписку и чужой файл не видно; пустое, лишнее и слишком большое -
  отказ словами;
* перекладка WebM → Ogg держит пакеты байт в байт и считает длительность.
"""
from __future__ import annotations

import base64
import struct
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.finance import auth, opus_ogg, support, support_bot
from app.finance.config import finance_settings

BASE = "/api/v1/finance"
PASSWORD = "pass-12345"
CHAT = "-1004474041636"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
)
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.core.config import settings
    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    monkeypatch.setattr(settings, "environment", "test")
    # Настоящий бот в тестах не живёт, даже если токен стоит в окружении машины.
    monkeypatch.setattr(finance_settings, "support_bot_token", "")
    auth._ATTEMPTS.clear()
    yield engine
    auth._ATTEMPTS.clear()


@pytest.fixture
def app(finance_db) -> FastAPI:
    from finance_routes import finance_app

    return finance_app()


def client(app: FastAPI, ip: str = "10.0.0.7") -> TestClient:
    return TestClient(app, headers={"x-client-ip": ip, "user-agent": "pytest-agent"})


def register(app: FastAPI, email: str = "owner@bbc.kz", name: str = "Ермеков Нурболат") -> TestClient:
    person = client(app)
    response = person.post(
        f"{BASE}/auth/register",
        json={"email": email, "password": PASSWORD, "company": "BBC (тест)", "full_name": name},
    )
    assert response.status_code == 201, response.text
    return person


class FakeBot:
    """Telegram, который всё принимает и всё записывает."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict]] = []
        self.fail: dict[str, support_bot.TelegramError] = {}
        self.files: dict[str, bytes] = {}
        self._next = 500

    def _id(self) -> int:
        self._next += 1
        return self._next

    def call(self, method, params=None, *, files=None, timeout=40):
        sent = {key: (value[0], value[1], value[2]) for key, value in (files or {}).items()}
        self.calls.append((method, dict(params or {}), sent))
        if method in self.fail:
            raise self.fail.pop(method)
        if method == "sendMediaGroup":
            return [{"message_id": self._id()} for _ in params["media"]]
        if method in ("sendMessage", "sendPhoto", "sendDocument", "sendVoice", "copyMessage"):
            return {"message_id": self._id()}
        return True

    def download(self, file_id: str) -> bytes:
        return self.files[file_id]

    def username(self) -> str:
        return "kort_support_bot"

    def methods(self) -> list[str]:
        return [method for method, _, _ in self.calls]


# ── WebM → Ogg ───────────────────────────────────────────────────────────────


def _element(element: int, payload: bytes = b"", *, unknown: bool = False) -> bytes:
    head = element.to_bytes((element.bit_length() + 7) // 8, "big")
    size = b"\x01\xff\xff\xff\xff\xff\xff\xff" if unknown else (0x10000000 | len(payload)).to_bytes(4, "big")
    return head + size + payload


def _packet(index: int, length: int = 30) -> bytes:
    # 0xF8: CELT 20 мс, один кадр - 960 отсчётов.
    return bytes([0xF8]) + bytes([index % 251]) * length


def _synthetic_webm() -> tuple[bytes, list[bytes]]:
    """WebM, как его пишет MediaRecorder, и все виды сборки блоков Matroska."""
    head = b"OpusHead" + bytes((1, 1)) + struct.pack("<HIhB", 312, 48_000, 0, 0)
    track = _element(
        0xAE,
        _element(0xD7, b"\x01") + _element(0x86, b"A_OPUS") + _element(0x63A2, head) + _element(0xE1, _element(0x9F, b"\x01")),
    )
    first = [_packet(index) for index in range(20)]
    simple = b"".join(_element(0xA3, b"\x81" + struct.pack(">h", index * 20) + b"\x80" + packet) for index, packet in enumerate(first))
    # Поток MediaRecorder: и Segment, и Cluster - «неизвестного размера».
    cluster_stream = _element(0x1F43B675, _element(0xE7, b"\x00") + simple, unknown=True)
    xiph = [_packet(100, 40), _packet(101, 44)]
    laced_xiph = b"\x81" + struct.pack(">h", 0) + bytes([0x80 | (1 << 1), 1, len(xiph[0])]) + b"".join(xiph)
    ebml = [_packet(102, 30), _packet(103, 33), _packet(104, 35)]
    # Размеры: первый - 31 (vint 0x9F), второй - разность +3 со смещением 63 (vint 0xC2).
    laced_ebml = b"\x81" + struct.pack(">h", 20) + bytes([0x80 | (3 << 1), 2, 0x80 | len(ebml[0]), 0x80 | 66]) + b"".join(ebml)
    grouped = _packet(105, 25)
    cluster_known = _element(
        0x1F43B675,
        _element(0xE7, b"\x01")
        + _element(0xA3, laced_xiph)
        + _element(0xA3, laced_ebml)
        + _element(0xA0, _element(0xA1, b"\x81" + struct.pack(">h", 60) + b"\x00" + grouped)),
    )
    segment = _element(0x18538067, _element(0x1654AE6B, track) + cluster_stream + cluster_known, unknown=True)
    return _element(0x1A45DFA3, _element(0x4282, b"webm")) + segment, first + xiph + ebml + [grouped]


def _slow_crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def _ogg(data: bytes) -> list[tuple[int, int, list[bytes]]]:
    """Страницы Ogg: флаги, позиция, пакеты - с проверкой CRC каждой."""
    pages = []
    pos = 0
    while pos < len(data):
        assert data[pos : pos + 4] == b"OggS"
        count = data[pos + 26]
        lacing = data[pos + 27 : pos + 27 + count]
        page = data[pos : pos + 27 + count + sum(lacing)]
        assert _slow_crc(page[:22] + b"\0\0\0\0" + page[26:]) == struct.unpack_from("<I", page, 22)[0]
        packets, current, offset = [], b"", 27 + count
        for size in lacing:
            current += page[offset : offset + size]
            offset += size
            if size < 255:
                packets.append(current)
                current = b""
        pages.append((page[5], struct.unpack_from("<q", page, 6)[0], packets))
        pos += len(page)
    return pages


def test_webm_perekladyvaetsya_v_ogg_paket_v_paket() -> None:
    data, packets = _synthetic_webm()
    voice = opus_ogg.webm_to_ogg(data)
    pages = _ogg(voice.data)
    assert pages[0][0] == 0x02 and pages[0][2][0].startswith(b"OpusHead")
    assert pages[1][2][0].startswith(b"OpusTags")
    audio = [packet for _, _, page_packets in pages[2:] for packet in page_packets]
    assert audio == packets, "пакеты Opus переложены не байт в байт или не по порядку"
    assert pages[-1][0] & 0x04, "последняя страница - с концом потока"
    assert pages[-1][1] == 960 * len(packets)
    assert voice.duration == pytest.approx((960 * len(packets) - 312) / 48_000)


def test_zapis_chrome_perekladyvaetsya_tselikom() -> None:
    """Настоящая запись MediaRecorder Chrome 153 (32 кбит/с, 1,3 с): 22 пакета у ffprobe."""
    voice = opus_ogg.webm_to_ogg((FIXTURES / "voice_chrome.webm").read_bytes())
    pages = _ogg(voice.data)
    assert sum(len(page_packets) for _, _, page_packets in pages[2:]) == 22
    assert 1.0 < voice.duration < 1.6


def test_ne_webm_otkaz() -> None:
    with pytest.raises(opus_ogg.RemuxError):
        opus_ogg.webm_to_ogg(b"OggS" + b"\0" * 40)
    with pytest.raises(opus_ogg.RemuxError):
        opus_ogg.webm_to_ogg(_element(0x1A45DFA3, b"") + _element(0x18538067, b"", unknown=True))


# ── Канал: как выглядит обращение ────────────────────────────────────────────


def test_dlinnyy_tekst_rezhetsya_po_slovam() -> None:
    text = ("Слово & ещё <слово>. " * 600).strip()
    pieces = support_bot.chunks(text)
    assert len(pieces) > 1
    assert all(len(support_bot._esc(piece)) <= support_bot.CHUNK for piece in pieces)
    assert " ".join(pieces).split() == text.split()


def test_telefon_i_dlitelnost() -> None:
    assert support_bot.phone_text("+77011234567") == "+7 701 123 45 67"
    assert support_bot.duration_text(75.4) == "1:15"


def test_oshibka_telegram_bez_tokena(monkeypatch) -> None:
    """Исключение `requests` несёт адрес с токеном - в текст ошибки он не попадает."""
    import requests

    api = support_bot.Bot("123456:SECRET-TOKEN")

    def boom(*args, **kwargs):
        raise requests.ConnectionError(f"Max retries exceeded with url: /bot123456:SECRET-TOKEN/{args[0]}")

    monkeypatch.setattr(api._http(), "post", boom)
    with pytest.raises(support_bot.TelegramError) as caught:
        api.call("getMe")
    assert "SECRET" not in str(caught.value) and caught.value.code is None


# ── Туда и обратно ───────────────────────────────────────────────────────────


def _send(person: TestClient, body: str = "", files=None, voice: bytes | None = None, duration: str = "1.3"):
    parts = list(files or [])
    if voice is not None:
        parts.append(("voice", ("голосовое.webm", voice, "audio/webm;codecs=opus")))
    return person.post(f"{BASE}/support/messages", data={"body": body, "voice_duration": duration}, files=parts or None)


def test_obrashchenie_v_kanal_i_otvet_obratno(app: FastAPI) -> None:
    owner = register(app)
    state = owner.get(f"{BASE}/auth/me").json()["support"]
    assert state == {"unread": 0, "last_reply": None, "notice_off": False, "notice_seen_on": None}

    voice = (FIXTURES / "voice_chrome.webm").read_bytes()
    sent = _send(
        owner,
        "Реестр не открывается после входа",
        files=[
            ("files", ("скрин 1.png", PNG, "image/png")),
            ("files", ("скрин 2.png", PNG, "image/png")),
            ("files", ("отчёт.xlsx", b"PK\x03\x04-xlsx", XLSX)),
        ],
        voice=voice,
    )
    assert sent.status_code == 201, sent.text
    message = sent.json()
    assert message["author"] == "user" and message["delivered"] is False and message["late"] is False
    assert [item["kind"] for item in message["files"]] == ["image", "image", "file", "voice"]

    fake = FakeBot()
    # Telegram оборвался на файле - шапка и альбом уже в канале.
    fake.fail["sendDocument"] = support_bot.TelegramError("нет связи с Telegram (ReadTimeout)")
    assert support_bot.deliver(fake, CHAT, uuid.UUID(message["id"])) is False
    assert fake.methods() == ["sendMessage", "sendMediaGroup", "sendDocument"]
    method, header, _ = fake.calls[0]
    assert header["chat_id"] == CHAT and header["parse_mode"] == "HTML"
    text = header["text"]
    assert text.startswith("<b>Новое обращение</b>")
    for piece in ("Ермеков Нурболат", "владелец", "BBC (тест)", "owner@bbc.kz", "UTC+5", "Реестр не открывается после входа"):
        assert piece in text, piece
    assert "Вложения: 2 фото, голосовое 0:01, файл «отчёт.xlsx»" in text
    button = header["reply_markup"]["inline_keyboard"][0][0]
    assert button == {"text": "Отправить ответ", "callback_data": f"r:{uuid.UUID(message['id']).hex}"}
    header_post = 501
    assert fake.calls[1][1]["reply_parameters"]["message_id"] == header_post

    # Повтор: ни шапки, ни альбома второй раз - только недошедшее.
    fake.calls.clear()
    assert support_bot.deliver(fake, CHAT, uuid.UUID(message["id"])) is True
    assert fake.methods() == ["sendDocument", "sendVoice"]
    voice_name, voice_bytes, voice_mime = fake.calls[1][2]["voice"]
    assert voice_mime == "audio/ogg" and voice_bytes.startswith(b"OggS") and voice_name == "голосовое.ogg"
    assert fake.calls[1][1]["duration"] == 1
    assert owner.get(f"{BASE}/support").json()["items"][0]["delivered"] is True
    assert support_bot.deliver(fake, CHAT, uuid.UUID(message["id"])) is False, "доставленное не шлётся снова"

    # «Отправить ответ» под обращением - сотрудник поддержки отвечает в личке бота.
    agent = {"id": 505093989, "first_name": "Metal", "last_name": "Rain"}
    fake.calls.clear()
    support_bot.handle(
        fake,
        CHAT,
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb-1",
                "from": agent,
                "message": {"message_id": header_post, "chat": {"id": int(CHAT), "type": "channel"}},
                "data": f"r:{uuid.UUID(message['id']).hex}",
            },
        },
    )
    prompt = next(params for method, params, _ in fake.calls if method == "sendMessage")
    assert prompt["chat_id"] == agent["id"] and "Ермеков Нурболат" in prompt["text"]
    opened = next(params for method, params, _ in fake.calls if method == "answerCallbackQuery")
    assert opened["url"] == "https://t.me/kort_support_bot?start=reply"

    reply = {
        "update_id": 2,
        "message": {
            "message_id": 77,
            "from": agent,
            "chat": {"id": agent["id"], "type": "private"},
            "text": "Пришлите, пожалуйста, скриншот ошибки",
        },
    }
    fake.calls.clear()
    support_bot.handle(fake, CHAT, reply)
    support_bot.handle(fake, CHAT, reply)  # то же событие ещё раз - после перезапуска
    me = owner.get(f"{BASE}/auth/me").json()["support"]
    assert me["unread"] == 1, "повтор события задвоил ответ"
    assert me["last_reply"]["preview"] == "Пришлите, пожалуйста, скриншот ошибки"
    log = next(params for method, params, _ in fake.calls if method == "sendMessage" and params["chat_id"] == CHAT)
    assert log["reply_parameters"]["message_id"] == header_post and "Metal Rain" in log["text"]
    edited = next(params for method, params, _ in fake.calls if method == "editMessageText")
    assert edited["message_id"] == header_post and "Отвечено · Metal Rain" in edited["text"]

    # Голосовое и альбом из лички - в KORT; альбом - одним сообщением.
    fake.files.update({"v-1": b"OggS-voice", "p-1": PNG, "p-2": PNG})
    support_bot.handle(fake, CHAT, {"update_id": 3, "message": {
        "message_id": 78, "from": agent, "chat": {"id": agent["id"], "type": "private"},
        "voice": {"file_id": "v-1", "duration": 4, "mime_type": "audio/ogg", "file_size": 10},
    }})
    for update_id, file_id in ((4, "p-1"), (5, "p-2")):
        support_bot.handle(fake, CHAT, {"update_id": update_id, "message": {
            "message_id": 70 + update_id, "from": agent, "chat": {"id": agent["id"], "type": "private"},
            "media_group_id": "album-1", "caption": "Вот так должно быть" if file_id == "p-1" else None,
            "photo": [{"file_id": file_id, "file_size": len(PNG)}],
        }})
    items = owner.get(f"{BASE}/support").json()["items"]
    replies = [item for item in items if item["author"] == "support"]
    assert [len(item["files"]) for item in replies] == [0, 1, 2]
    assert replies[1]["files"][0]["kind"] == "voice" and replies[2]["body"] == "Вот так должно быть"
    assert [item["kind"] for item in replies[2]["files"]] == ["image", "image"]
    assert owner.get(f"{BASE}/auth/me").json()["support"]["unread"] == 3

    read = owner.post(f"{BASE}/support/read")
    assert read.status_code == 200 and read.json()["unread"] == 0
    assert all(item["read"] for item in owner.get(f"{BASE}/support").json()["items"] if item["author"] == "support")

    # «Готово» - ответ закрыт; дальше личка никуда не пишет.
    fake.calls.clear()
    support_bot.handle(fake, CHAT, {"update_id": 6, "callback_query": {
        "id": "cb-2", "from": agent, "message": {"message_id": 90, "chat": {"id": agent["id"], "type": "private"}},
        "data": "done",
    }})
    support_bot.handle(fake, CHAT, {"update_id": 7, "message": {
        "message_id": 91, "from": agent, "chat": {"id": agent["id"], "type": "private"}, "text": "ещё одно",
    }})
    assert owner.get(f"{BASE}/auth/me").json()["support"]["unread"] == 0
    assert fake.calls[-1][1]["text"] == support_bot.NO_TARGET

    # Следующее сообщение человека - продолжение переписки, цепочкой под прошлым.
    follow = _send(owner, "Скриншот приложил", files=[("files", ("ошибка.png", PNG, "image/png"))])
    assert follow.status_code == 201, follow.text
    fake.calls.clear()
    assert support_bot.deliver(fake, CHAT, uuid.UUID(follow.json()["id"])) is True
    header = fake.calls[0][1]
    assert header["text"].startswith("<b>Продолжение переписки</b>")
    assert "в ответ на: «" in header["text"]
    assert header["reply_parameters"]["message_id"] == header_post
    assert fake.methods() == ["sendMessage", "sendPhoto"]


def test_knopka_otveta_tolko_iz_kanala(app: FastAPI) -> None:
    owner = register(app)
    sent = _send(owner, "Вопрос")
    fake = FakeBot()
    support_bot.handle(fake, CHAT, {"update_id": 11, "callback_query": {
        "id": "cb", "from": {"id": 1, "first_name": "Чужой"},
        "message": {"message_id": 5, "chat": {"id": -100123, "type": "channel"}},
        "data": f"r:{uuid.UUID(sent.json()['id']).hex}",
    }})
    support_bot.handle(fake, CHAT, {"update_id": 12, "message": {
        "message_id": 6, "from": {"id": 1, "first_name": "Чужой"}, "chat": {"id": 1, "type": "private"}, "text": "ответ",
    }})
    assert owner.get(f"{BASE}/auth/me").json()["support"]["unread"] == 0
    answered = next(params for method, params, _ in fake.calls if method == "answerCallbackQuery")
    assert "только в канале поддержки" in answered["text"]


def test_bez_knopki_bot_vedyot_k_obrashcheniyu(app: FastAPI) -> None:
    """16.10.2026 владелец сначала написал боту напрямую, и ответ никуда не ушёл.
    Адресата бот не угадывает, но даёт ссылку на последнее обращение без ответа."""
    owner = register(app)
    sent = _send(owner, "Не сходится остаток")
    fake = FakeBot()
    assert support_bot.deliver(fake, CHAT, uuid.UUID(sent.json()["id"])) is True
    fake.calls.clear()
    support_bot.handle(fake, CHAT, {"update_id": 21, "message": {
        "message_id": 3, "from": {"id": 7, "first_name": "Агент"}, "chat": {"id": 7, "type": "private"}, "text": "Ответ",
    }})
    said = fake.calls[-1][1]
    assert said["text"].startswith(support_bot.NO_TARGET) and "Ермеков Нурболат" in said["text"]
    assert said["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://t.me/c/4474041636/501"
    assert owner.get(f"{BASE}/auth/me").json()["support"]["unread"] == 0, "без кнопки ответ никуда не уходит"


def test_chuzhaya_perepiska_i_faily_ne_vidny(app: FastAPI) -> None:
    owner = register(app)
    other = register(app, "other@bbc.kz", "Другой Человек")
    sent = _send(owner, "Моё", files=[("files", ("скрин.png", PNG, "image/png")), ("files", ("page.html", b"<script>x</script>", "text/html"))])
    image, page = sent.json()["files"]
    assert other.get(f"{BASE}/support").json()["items"] == []
    assert other.get(f"{BASE}/support/files/{image['id']}").status_code == 404

    shown = owner.get(f"{BASE}/support/files/{image['id']}")
    assert shown.status_code == 200 and shown.content == PNG
    assert shown.headers["content-type"] == "image/png" and shown.headers["content-disposition"].startswith("inline")
    assert "sandbox" in shown.headers["content-security-policy"]
    # HTML не открывается страницей с адреса KORT - только скачивается.
    saved = owner.get(f"{BASE}/support/files/{page['id']}")
    assert saved.headers["content-type"] == "application/octet-stream"
    assert saved.headers["content-disposition"].startswith("attachment")
    assert "filename*=UTF-8''page.html" in saved.headers["content-disposition"]


def test_otkazy_slovami(app: FastAPI, monkeypatch) -> None:
    owner = register(app)
    empty = _send(owner, "   ")
    assert empty.status_code == 400 and "Напишите" in empty.json()["detail"]
    many = _send(owner, "x", files=[("files", (f"{index}.png", PNG, "image/png")) for index in range(11)])
    assert many.status_code == 400
    big = _send(owner, "x", files=[("files", ("большой.bin", b"0" * (support.MAX_FILE_BYTES + 1), "application/octet-stream"))])
    assert big.status_code == 413 and "20 МБ" in big.json()["detail"]
    # Картинка по слову браузера, но не по содержимому - файл, а не картинка в переписке.
    fake = _send(owner, "x", files=[("files", ("svg.png", b"<svg onload=alert(1)>", "image/png"))])
    assert fake.json()["files"][0]["kind"] == "file"
    monkeypatch.setattr(support, "RATE_LIMIT", 2)
    assert _send(owner, "раз").status_code == 201
    flood = _send(owner, "два")
    assert flood.status_code == 429 and "подождите" in flood.json()["detail"]


def test_obyavlenie_raz_v_den_i_navsegda(app: FastAPI) -> None:
    owner = register(app)
    seen = owner.post(f"{BASE}/support/notice", json={"seen_on": "2026-10-16"})
    assert seen.status_code == 200 and seen.json()["notice_seen_on"] == "2026-10-16"
    assert seen.json()["notice_off"] is False
    off = owner.post(f"{BASE}/support/notice", json={"off": True})
    assert off.json()["notice_off"] is True and off.json()["notice_seen_on"] == "2026-10-16"
    me = owner.get(f"{BASE}/auth/me").json()["support"]
    assert me["notice_off"] is True
