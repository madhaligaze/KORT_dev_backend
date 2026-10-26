"""Записать полигон на диск: `uv run python -m polygon.build <папка> [seed]` из `backend/tests`."""
from __future__ import annotations

import sys

from polygon.sources import write
from polygon.world import build


def main(argv: list[str]) -> int:
    if not argv:
        print("Куда записать: python -m polygon.build <папка> [seed]")
        return 2
    world = build(int(argv[1])) if len(argv) > 1 else build()
    for name in write(world, argv[0]):
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
