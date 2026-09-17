"""Board registry.

Only the Tang Nano 9K is supported today. Board-specific details (pin
constraints, PLL primitives, default clock) must stay behind this module so
that porting to the Tang Nano 20K later does not touch the RTL.
"""

from __future__ import annotations

from typing import Any, Callable


def _tang_nano_9k():
    from amaranth_boards.tang_nano_9k import TangNano9kPlatform

    return TangNano9kPlatform()


_BOARDS: dict[str, Callable[[], Any]] = {
    "tang-nano-9k": _tang_nano_9k,
}


def board_names() -> list[str]:
    return sorted(_BOARDS)


def get_board(name: str):
    try:
        factory = _BOARDS[name]
    except KeyError:
        raise SystemExit(
            f"unknown board {name!r}; known boards: {', '.join(board_names())}"
        ) from None
    return factory()
