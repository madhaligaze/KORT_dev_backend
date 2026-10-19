"""Голосовое из браузера - в Ogg Opus, родной формат голосовых Telegram.

Chrome и Edge пишут голос (`MediaRecorder`) в WebM с Opus внутри, Firefox -
сразу в Ogg Opus. Telegram показывает «голосовым» (с волной и перемоткой)
только Ogg Opus, MP3 или M4A, а WebM уходит файлом, который поддержке надо
скачать, чтобы послушать. Звук при этом уже Opus - перекладывать нужно только
контейнер: пакеты те же, байт в байт, без перекодирования и без ffmpeg в
образе API.

WebM (Matroska) читается плоско: заголовки контейнеров (Segment, Cluster,
BlockGroup, Tracks…) не перешагиваются, а входятся - их дети идут следом. Так
и элементы «неизвестного размера», которыми MediaRecorder пишет поток, читаются
без особых случаев. Ogg собирается по RFC 7845: страница OpusHead, страница
OpusTags, страницы звука с позицией в отсчётах 48 кГц.
"""
from __future__ import annotations

import secrets
import struct
import zlib
from dataclasses import dataclass

#: Отсчётов Opus в секунде: позиции и длительности Ogg Opus - всегда в 48 кГц.
RATE = 48_000

# Идентификаторы элементов Matroska - с маркерными битами, как в файле.
_SEGMENT = 0x18538067
_CLUSTER = 0x1F43B675
_TRACKS = 0x1654AE6B
_TRACK_ENTRY = 0xAE
_TRACK_NUMBER = 0xD7
_CODEC_ID = 0x86
_CODEC_PRIVATE = 0x63A2
_CODEC_DELAY = 0x56AA
_AUDIO = 0xE1
_CHANNELS = 0x9F
_BLOCK_GROUP = 0xA0
_BLOCK = 0xA1
_SIMPLE_BLOCK = 0xA3
#: Контейнеры, в которые входим: их дети идут следом за заголовком.
_ENTER = frozenset({_SEGMENT, _CLUSTER, _TRACKS, _TRACK_ENTRY, _BLOCK_GROUP, _AUDIO})


class RemuxError(ValueError):
    """Файл - не WebM с Opus или битый: голосовое уйдёт файлом как есть."""


@dataclass
class Voice:
    data: bytes
    #: Длительность звука в секундах - без отсчётов разгона декодера (pre-skip).
    duration: float


def _vint(data: bytes, pos: int, *, marker: bool) -> tuple[int, int, bool]:
    """Число переменной длины EBML: (значение, длина, «размер неизвестен»)."""
    first = data[pos]
    if first == 0:
        raise RemuxError("битый заголовок элемента")
    length = 1
    mask = 0x80
    while not first & mask:
        mask >>= 1
        length += 1
    value = first if marker else first & (mask - 1)
    for index in range(1, length):
        value = (value << 8) | data[pos + index]
    unknown = not marker and value == (1 << (7 * length)) - 1
    return value, length, unknown


def _uint(payload: bytes) -> int:
    return int.from_bytes(payload, "big") if payload else 0


def _laced(block: bytes) -> tuple[int, list[bytes]]:
    """Дорожка блока и его кадры: без сборки, Xiph, EBML и равными долями."""
    track, length, _ = _vint(block, 0, marker=False)
    flags = block[length + 2]
    body = block[length + 3 :]
    lacing = (flags >> 1) & 0x03
    if lacing == 0:
        return track, [body]
    count = body[0] + 1
    pos = 1
    if lacing == 2:
        each = (len(body) - 1) // count
        return track, [body[1 + index * each : 1 + (index + 1) * each] for index in range(count)]
    sizes: list[int] = []
    if lacing == 1:
        for _ in range(count - 1):
            size = 0
            while True:
                byte = body[pos]
                pos += 1
                size += byte
                if byte != 255:
                    break
            sizes.append(size)
    else:
        first, step, _ = _vint(body, pos, marker=False)
        pos += step
        sizes.append(first)
        for _ in range(count - 2):
            raw, step, _ = _vint(body, pos, marker=False)
            pos += step
            sizes.append(sizes[-1] + raw - ((1 << (7 * step - 1)) - 1))
    frames = []
    for size in sizes:
        frames.append(body[pos : pos + size])
        pos += size
    frames.append(body[pos:])
    return track, frames


def packet_samples(packet: bytes) -> int:
    """Длительность пакета Opus в отсчётах 48 кГц - по его первому байту (RFC 6716, 3.1)."""
    if not packet:
        return 0
    toc = packet[0]
    config = toc >> 3
    if config < 12:
        frame = (480, 960, 1920, 2880)[config & 3]
    elif config < 16:
        frame = (480, 960)[config & 1]
    else:
        frame = (120, 240, 480, 960)[config & 3]
    code = toc & 0x03
    if code == 0:
        count = 1
    elif code in (1, 2):
        count = 2
    else:
        count = packet[1] & 0x3F if len(packet) > 1 else 0
    return frame * count


def _read_webm(data: bytes) -> tuple[bytes, list[bytes]]:
    """OpusHead и пакеты звука дорожки Opus."""
    tracks: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    blocks: list[bytes] = []
    pos = 0
    size_total = len(data)
    while pos < size_total:
        try:
            element, id_length, _ = _vint(data, pos, marker=True)
            size, size_length, unknown = _vint(data, pos + id_length, marker=False)
        except IndexError:
            break  # запись оборвалась на заголовке - дальше читать нечего
        start = pos + id_length + size_length
        if element in _ENTER:
            if element == _TRACK_ENTRY:
                current = {}
                tracks.append(current)
            pos = start
            continue
        if unknown:
            raise RemuxError("элемент неизвестного размера вне потока")
        end = start + size
        if end > size_total:
            break  # последний блок обрезан - всё до него цело
        payload = data[start:end]
        if element in (_SIMPLE_BLOCK, _BLOCK):
            blocks.append(payload)
        elif current is not None:
            if element == _TRACK_NUMBER:
                current["number"] = _uint(payload)
            elif element == _CODEC_ID:
                current["codec"] = payload.rstrip(b"\x00").decode("ascii", "replace")
            elif element == _CODEC_PRIVATE:
                current["private"] = payload
            elif element == _CODEC_DELAY:
                current["delay"] = _uint(payload)
            elif element == _CHANNELS:
                current["channels"] = _uint(payload)
        pos = end
    opus = next((track for track in tracks if track.get("codec") == "A_OPUS"), None)
    if opus is None:
        raise RemuxError("в файле нет дорожки Opus")
    number = opus.get("number")
    packets: list[bytes] = []
    for block in blocks:
        track, frames = _laced(block)
        if number is None or track == number:
            packets.extend(frame for frame in frames if frame)
    if not packets:
        raise RemuxError("в записи нет звука")
    private = opus.get("private")
    if isinstance(private, bytes) and private.startswith(b"OpusHead") and len(private) >= 19:
        head = private
    else:
        channels = int(opus.get("channels") or 1)
        pre_skip = int(opus.get("delay") or 0) * RATE // 1_000_000_000
        head = b"OpusHead" + bytes((1, channels)) + struct.pack("<HIhB", pre_skip, RATE, 0, 0)
    return head, packets


#: Байт с битами в обратном порядке - для `_crc`.
_REVERSED_BITS = bytes(int(f"{value:08b}"[::-1], 2) for value in range(256))


def _crc(data: bytes) -> int:
    """CRC страницы Ogg: многочлен 0x04C11DB7, без отражения, с нуля и без финального XOR.

    У zlib тот же многочлен, но отражённый. Считается им по байтам с
    обращёнными битами - результат тот же, что у прямого счёта, а скорость
    кода на C: прямой цикл на Python тратил полсекунды на 200 КБ голосового.
    """
    raw = zlib.crc32(data.translate(_REVERSED_BITS), 0xFFFFFFFF) ^ 0xFFFFFFFF
    return int(f"{raw:032b}"[::-1], 2)


def _page(serial: int, sequence: int, granule: int, packets: list[bytes], flags: int) -> bytes:
    lacing = bytearray()
    for packet in packets:
        lacing.extend(b"\xff" * (len(packet) // 255))
        lacing.append(len(packet) % 255)
    header = struct.pack("<4sBBqIII", b"OggS", 0, flags, granule, serial, sequence, 0) + bytes((len(lacing),)) + lacing
    page = bytearray(header + b"".join(packets))
    page[22:26] = struct.pack("<I", _crc(bytes(page)))
    return bytes(page)


#: Пакетов на страницу: секунда звука при кадрах 20 мс.
_PER_PAGE = 50
#: Сегментов в таблице страницы Ogg - не больше 255 (поле в один байт).
_MAX_SEGMENTS = 255


def _segments(packet: bytes) -> int:
    return len(packet) // 255 + 1


def webm_to_ogg(data: bytes) -> Voice:
    """Переложить запись WebM/Opus в Ogg/Opus. Не WebM или битый - `RemuxError`."""
    if not data.startswith(b"\x1a\x45\xdf\xa3"):
        raise RemuxError("это не WebM")
    try:
        head, packets = _read_webm(data)
    except IndexError as exc:
        raise RemuxError("битый WebM") from exc
    serial = secrets.randbits(32)
    vendor = b"KORT"
    tags = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)
    pages = [_page(serial, 0, 0, [head], 0x02), _page(serial, 1, 0, [tags], 0x00)]
    chunks: list[list[bytes]] = [[]]
    used = 0
    for packet in packets:
        need = _segments(packet)
        if chunks[-1] and (len(chunks[-1]) >= _PER_PAGE or used + need > _MAX_SEGMENTS):
            chunks.append([])
            used = 0
        chunks[-1].append(packet)
        used += need
    granule = 0
    for index, chunk in enumerate(chunks):
        granule += sum(packet_samples(packet) for packet in chunk)
        last = index == len(chunks) - 1
        pages.append(_page(serial, index + 2, granule, chunk, 0x04 if last else 0x00))
    pre_skip = struct.unpack_from("<H", head, 10)[0]
    return Voice(data=b"".join(pages), duration=max(0.0, (granule - pre_skip) / RATE))


__all__ = ["RATE", "RemuxError", "Voice", "packet_samples", "webm_to_ogg"]
