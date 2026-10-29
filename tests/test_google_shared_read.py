"""Один лист Google - одно чтение на всех читателей сводной таблицы.

«Оплачено (сводка)», «Счета» и сверка ядра обновлялись почти разом и читали лист
трижды подряд, мимо кэша; три разбора могли увидеть три состояния таблицы.
Свежее чтение, которое уже идёт или закончилось секунды назад, отдаётся всем.
"""
from __future__ import annotations

import threading
import time

from app.finance import google


def test_tri_chitatelya_odin_snimok(monkeypatch) -> None:
    calls: list[str] = []
    started = threading.Event()

    def fetch(spreadsheet_id: str, tab_title: str, key: tuple[str, str]) -> list[list[str]]:
        calls.append(tab_title)
        started.set()
        time.sleep(0.2)
        values = [["Заказчик"], [f"снимок {len(calls)}"]]
        with google._lock:
            google._remember(google._values_cache, key, values)
        return values

    monkeypatch.setattr(google, "_fetch_tab", fetch)
    google.invalidate_cache()
    got: list[list[list[str]]] = []
    readers = [threading.Thread(target=lambda: got.append(google.fetch_tab_values("book", "Сводка", fresh=True))) for _ in range(3)]
    readers[0].start()
    started.wait(2)
    for reader in readers[1:]:
        reader.start()
    for reader in readers:
        reader.join(5)
    assert calls == ["Сводка"], "три читателя разом - одно чтение листа"
    assert got == [[["Заказчик"], ["снимок 1"]]] * 3, "и один снимок у всех"
    # Секунды спустя (окно прошло) «Обновить» читает заново.
    monkeypatch.setattr(google, "_SHARED_SECONDS", 0.0)
    assert google.fetch_tab_values("book", "Сводка", fresh=True) == [["Заказчик"], ["снимок 2"]]
    google.invalidate_cache()
