"""Audio generation.

One real DDS voice: a 32-bit phase accumulator advances once per
48 kHz sample and addresses a 4096-entry, 16-bit sine table held in BSRAM.

**The sample is signed two's complement.** HDMI L-PCM is signed, and
hdl-util/hdmi zero-extends the 16-bit input to 24 bits and left-justifies it, so
the bit pattern has to be the signed value: feeding an offset-binary (unsigned,
centred on ``0x8000``) sample makes the sink read a square-ish, hugely offset
waveform. ``sample`` is an unsigned ``Signal`` holding that two's-complement
pattern; :func:`to_signed` recovers the numeric value for tests and analysis.

The accumulator runs in the ``pixel`` domain and is advanced by a strobe rather
than by its own clock, so there is exactly one clock in the design. The strobe
is produced alongside ``clk_audio`` (see :mod:`xsynth.hdl.clock`).
"""

from __future__ import annotations

import math

from amaranth import Elaboratable, Module, Signal, unsigned
from amaranth.lib.memory import Memory

PHASE_BITS = 32
TABLE_BITS = 12
SAMPLE_BITS = 16

TABLE_SIZE = 1 << TABLE_BITS
SAMPLE_MASK = (1 << SAMPLE_BITS) - 1
PEAK = (1 << (SAMPLE_BITS - 1)) - 1  # most positive signed sample

DEFAULT_TONE_HZ = 440


def to_signed(value: int, bits: int = SAMPLE_BITS) -> int:
    """Reinterpret an unsigned bit pattern as two's complement."""
    return value - (1 << bits) if value >= (1 << (bits - 1)) else value


def phase_step(hz: float, sample_rate: int, phase_bits: int = PHASE_BITS) -> int:
    """Phase increment that makes the accumulator advance ``hz`` per second."""
    if hz < 0:
        raise ValueError("tone frequency must not be negative")
    step = round(hz * (1 << phase_bits) / sample_rate)
    if step >= (1 << phase_bits):
        raise ValueError(
            f"tone {hz} Hz is at or above the {sample_rate} Hz sample rate"
        )
    return step


def sine_table(table_bits: int = TABLE_BITS,
               sample_bits: int = SAMPLE_BITS) -> list[int]:
    """One full period of a signed sine, as two's-complement bit patterns.

    ``round`` of the ideal value is within half an LSB, so the table itself
    contributes about -90 dBc of error: far below the phase-accumulator
    truncation noise.
    """
    size = 1 << table_bits
    peak = (1 << (sample_bits - 1)) - 1
    mask = (1 << sample_bits) - 1
    return [round(peak * math.sin(2 * math.pi * i / size)) & mask
            for i in range(size)]


class SineDDS(Elaboratable):
    """A phase accumulator plus a sine lookup table.

    ``strobe`` marks a sample boundary. The phase is advanced on that edge; the
    table read is a synchronous BSRAM port, so ``sample`` follows one pixel
    clock later. That is harmless: the phase is stable for a whole sample
    period (525 pixel clocks for VGA), so the output is stable well before the
    HDMI core latches it on the next ``clk_audio`` edge.
    """

    def __init__(self, step: int, *, phase_bits: int = PHASE_BITS,
                 table_bits: int = TABLE_BITS, sample_bits: int = SAMPLE_BITS):
        if not 0 < step < (1 << phase_bits):
            raise ValueError("step must be a non-zero phase increment")
        if table_bits > phase_bits:
            raise ValueError("table_bits cannot exceed phase_bits")

        self.step = step
        self.phase_bits = phase_bits
        self.table_bits = table_bits
        self.sample_bits = sample_bits

        self.strobe = Signal()
        self.phase = Signal(phase_bits)
        self.sample = Signal(sample_bits)

    def elaborate(self, platform):
        m = Module()

        table = Memory(
            shape=unsigned(self.sample_bits),
            depth=1 << self.table_bits,
            init=sine_table(self.table_bits, self.sample_bits),
        )
        m.submodules.table = table
        read_port = table.read_port(domain="pixel")
        m.d.comb += read_port.addr.eq(
            self.phase[self.phase_bits - self.table_bits:]
        )
        m.d.pixel += self.sample.eq(read_port.data)

        with m.If(self.strobe):
            m.d.pixel += self.phase.eq(self.phase + self.step)

        return m
