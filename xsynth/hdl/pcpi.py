"""The Xsynth co-processor: a custom instruction that feeds the command FIFO.

PicoRV32 hands every instruction it cannot decode to the co-processor through
PCPI. We claim two custom-0 instructions::

    funct3 = 0   push {rs2, rs1} into the FIFO as one 64-bit command
    funct3 = 1   read the FIFO level back into rd

The push *stalls* the CPU while the FIFO is full rather than dropping the
command. That is the whole point: a sequencer which outruns the engine slows
down instead of losing notes, and no amount of firmware care is needed to make
it safe. PicoRV32 abandons an instruction after 16 cycles unless ``pcpi_wait``
is asserted, so asserting the wait is what makes an arbitrarily long stall
legal.

The command word layout is the one in :mod:`xsynth.protocol`::

    bits 63..56  opcode
    bits 55..48  voice
    bits 47..16  value
    bits 15..0   delay

so ``rs1`` holds the low half and ``rs2`` the high half.
"""

from __future__ import annotations

from amaranth import Cat, Elaboratable, Module, Signal

CUSTOM0 = 0b0001011

FUNCT3_PUSH = 0
FUNCT3_LEVEL = 1
FUNCT7 = 0


def encode_r(funct7: int, rs2: int, rs1: int, funct3: int, rd: int,
             opcode: int) -> int:
    """Assemble a plain R-type instruction, for tests and hand-built programs."""
    return ((funct7 << 25) | (rs2 << 20) | (rs1 << 15) | (funct3 << 12)
            | (rd << 7) | opcode)


def push_instruction(rs1: int, rs2: int) -> int:
    """``xsynth.push`` — push ``{rs2, rs1}`` into the command FIFO."""
    return encode_r(FUNCT7, rs2, rs1, FUNCT3_PUSH, 0, CUSTOM0)


def level_instruction(rd: int) -> int:
    """``xsynth.level`` — read the FIFO level into ``rd``."""
    return encode_r(FUNCT7, 0, 0, FUNCT3_LEVEL, rd, CUSTOM0)


class CommandCoProcessor(Elaboratable):
    """PCPI co-processor that pushes 64-bit commands into the engine FIFO."""

    def __init__(self, fifo, *, domain: str = "sync"):
        self.fifo = fifo
        self.domain = domain

        # PCPI, as PicoRV32 presents it.
        self.valid = Signal()
        self.insn = Signal(32)
        self.rs1 = Signal(32)
        self.rs2 = Signal(32)
        self.wr = Signal()
        self.rd = Signal(32)
        self.wait = Signal()
        self.ready = Signal()

        self.pushes = Signal(32)

    def elaborate(self, platform):
        m = Module()
        fifo = self.fifo

        opcode = self.insn[0:7]
        funct3 = self.insn[12:15]
        funct7 = self.insn[25:32]

        ours = Signal()
        pushing = Signal()
        m.d.comb += [
            ours.eq((opcode == CUSTOM0) & (funct7 == FUNCT7)
                    & ((funct3 == FUNCT3_PUSH) | (funct3 == FUNCT3_LEVEL))),
            pushing.eq((funct3 == FUNCT3_PUSH)),
        ]

        # Only a push can block, and only while the FIFO has no room. Reading
        # the level always completes immediately.
        stalled = Signal()
        m.d.comb += stalled.eq(pushing & fifo.w_full)

        m.d.comb += [
            self.wait.eq(self.valid & ours & stalled),
            self.ready.eq(self.valid & ours & ~stalled),
            self.wr.eq(self.valid & ours & (funct3 == FUNCT3_LEVEL)),
            self.rd.eq(fifo.w_level),
            fifo.w_data.eq(Cat(self.rs1, self.rs2)),
            fifo.w_inc.eq(self.valid & ours & pushing & ~fifo.w_full),
        ]

        return m
