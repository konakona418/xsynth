"""CRC-16/CCITT used to protect command frames.

The polynomial and initial value match the ``CRC-16/CCITT-FALSE`` parameters
(``poly=0x1021``, ``init=0xFFFF``, no reflection, no final XOR), which is the
same checksum the host reference implementation in :mod:`xsynth.protocol`
computes.

The hardware version is a fully unrolled bit-serial step: eight XOR/shift stages
in combinational logic, so a byte is absorbed in a single cycle. At 27 MHz that
is far cheaper than a 256-entry table, and the control domain has thousands of
cycles of slack between UART bytes.
"""

from __future__ import annotations

from amaranth import Const, Elaboratable, Module, Mux, Signal

POLY = 0x1021
INIT = 0xFFFF


def crc16_update(crc: int, byte: int, *, poly: int = POLY) -> int:
    """Absorb one byte, MSB first. The reference implementation."""
    crc ^= (byte & 0xFF) << 8
    for _ in range(8):
        if crc & 0x8000:
            crc = ((crc << 1) ^ poly) & 0xFFFF
        else:
            crc = (crc << 1) & 0xFFFF
    return crc


def crc16(data: bytes | bytearray, crc: int = INIT, *,
          poly: int = POLY) -> int:
    for byte in data:
        crc = crc16_update(crc, byte, poly=poly)
    return crc


class Crc16(Elaboratable):
    """Combinational CRC step behind a register.

    Pulse ``start`` to reload ``init``, or ``stb`` with ``byte`` valid to absorb
    one byte. Both are synchronous to ``domain``.
    """

    def __init__(self, *, poly: int = POLY, init: int = INIT,
                 domain: str = "sync"):
        self.poly = poly
        self.init = init
        self.domain = domain

        self.start = Signal()
        self.stb = Signal()
        self.byte = Signal(8)
        self.value = Signal(16, init=init)

    def elaborate(self, platform):
        m = Module()
        d = m.d[self.domain]

        poly = Const(self.poly, 16)
        crc = self.value ^ (self.byte << 8)
        for _ in range(8):
            crc = Mux(crc[15], ((crc << 1) ^ poly)[:16], (crc << 1)[:16])

        with m.If(self.start):
            d += self.value.eq(self.init)
        with m.Elif(self.stb):
            d += self.value.eq(crc)

        return m
