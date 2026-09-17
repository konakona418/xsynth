"""Phase 2 voice: a wavetable DDS driven by commands from the FIFO.

Phase 1's :class:`~xsynth.hdl.audio.SineDDS` stays as it is (it is verified on
hardware and is the reference for the 32-bit accumulator). This module is the
parameterised version PLAN.md calls for at Phase 4: several wavetables in one
BSRAM, a gate and an amplitude, plus the command scheduler that decides *when*
a command takes effect.

The four tables are naive (not band-limited), so saw and square alias. That is
deliberate for Phase 2, which is about the control path; PLAN.md puts the real
oscillator work in Phase 4.
"""

from __future__ import annotations

import math

from amaranth import Elaboratable, Mux, Module, Signal, unsigned
from amaranth.lib.memory import Memory

from xsynth.hdl.audio import PEAK, PHASE_BITS, SAMPLE_BITS
from xsynth.protocol import (
    DELAY_BITS,
    OP_NOTE_OFF,
    OP_NOTE_ON,
    OP_RESET,
    OP_SET_AMP,
    OP_SET_FREQ,
    OP_SET_WAVE,
)

WAVE_BITS = 2
WAVES = ("sine", "saw", "square", "triangle")
TABLE_BITS = 11
TABLE_SIZE = 1 << TABLE_BITS

SAMPLE_MASK = (1 << SAMPLE_BITS) - 1


def waveform(name: str, *, table_bits: int = TABLE_BITS,
             sample_bits: int = SAMPLE_BITS) -> list[int]:
    """One period of ``name`` as two's-complement bit patterns."""
    size = 1 << table_bits
    peak = (1 << (sample_bits - 1)) - 1
    mask = (1 << sample_bits) - 1

    if name == "sine":
        values = [round(peak * math.sin(2 * math.pi * i / size))
                  for i in range(size)]
    elif name == "saw":
        values = [round(peak * (2 * i / size - 1)) for i in range(size)]
    elif name == "square":
        values = [peak if i < size // 2 else -peak for i in range(size)]
    elif name == "triangle":
        values = [
            round(peak * (4 * i / size - 1)) if i < size // 2
            else round(peak * (3 - 4 * i / size))
            for i in range(size)
        ]
    else:
        raise ValueError(f"unknown waveform {name!r}; known: {', '.join(WAVES)}")

    return [value & mask for value in values]


def wavetable_init(waves=WAVES, *, table_bits: int = TABLE_BITS,
                   sample_bits: int = SAMPLE_BITS) -> list[int]:
    """All tables concatenated, which is how they are addressed in BSRAM."""
    init: list[int] = []
    for name in waves:
        init.extend(waveform(name, table_bits=table_bits,
                             sample_bits=sample_bits))
    return init


class WavetableVoice(Elaboratable):
    """A phase accumulator, a wavetable bank and an amplitude gate.

    ``step``, ``wave`` and ``amp`` are the parameters commands write; ``sample``
    is the signed two's-complement output the HDMI core expects. A zero ``step``
    forces silence, so a stopped voice can never emit a DC offset.
    """

    def __init__(self, *, waves=WAVES, table_bits: int = TABLE_BITS,
                 sample_bits: int = SAMPLE_BITS, phase_bits: int = PHASE_BITS):
        if not waves:
            raise ValueError("at least one wavetable is required")
        if table_bits > phase_bits:
            raise ValueError("table_bits cannot exceed phase_bits")
        if sample_bits > phase_bits:
            raise ValueError("sample_bits cannot exceed phase_bits")

        self.waves = tuple(waves)
        self.table_bits = table_bits
        self.sample_bits = sample_bits
        self.phase_bits = phase_bits

        self.strobe = Signal()
        self.reset_phase = Signal()
        self.step = Signal(phase_bits)
        self.wave = Signal(range(len(self.waves)))
        self.amp = Signal(sample_bits)

        self.phase = Signal(phase_bits)
        self.sample = Signal(sample_bits)

    def elaborate(self, platform):
        m = Module()
        d = m.d.pixel

        table = Memory(
            shape=unsigned(self.sample_bits),
            depth=len(self.waves) << self.table_bits,
            init=wavetable_init(self.waves, table_bits=self.table_bits,
                                sample_bits=self.sample_bits),
        )
        m.submodules.table = table
        read_port = table.read_port(domain="pixel")
        m.d.comb += read_port.addr.eq(
            (self.wave << self.table_bits)
            | self.phase[self.phase_bits - self.table_bits:]
        )

        scaled = (
            read_port.data.as_signed() * self.amp.as_signed()
        ) >> (self.sample_bits - 1)
        d += self.sample.eq(
            Mux(self.step == 0, 0, scaled)[: self.sample_bits]
        )

        with m.If(self.reset_phase):
            d += self.phase.eq(0)
        with m.Elif(self.strobe):
            d += self.phase.eq(self.phase + self.step)

        return m


class CommandScheduler(Elaboratable):
    """Pop 64-bit commands and apply them one at a time, on sample boundaries.

    ``delay`` is the number of samples between this command taking effect and
    the previous one taking effect, so a host can express a whole sequence as
    relative gaps and the engine needs no absolute time base. ``delay`` of 0 and
    1 both mean "the very next sample"; larger values are honoured exactly.

    While the FIFO is empty the scheduler simply idles; it does not fabricate
    commands or report an error.
    """

    def __init__(self, fifo, *, domain: str = "pixel"):
        self.fifo = fifo
        self.domain = domain

        self.strobe = Signal()
        self.apply = Signal()
        self.command = Signal(64)
        self.valid = Signal()

    def elaborate(self, platform):
        m = Module()
        d = m.d[self.domain]
        fifo = self.fifo

        holding = Signal(64)
        delay = Signal(DELAY_BITS)
        valid = Signal()

        d += self.apply.eq(0)

        # delay 1 and delay 0 both mean "the next sample": the counter is loaded
        # one short, because the sample it is loaded on already counts.
        load_delay = Mux(
            fifo.r_data[:DELAY_BITS] == 0, 0, fifo.r_data[:DELAY_BITS] - 1
        )

        with m.If(self.strobe):
            with m.If(valid & (delay != 0)):
                d += delay.eq(delay - 1)
            with m.Else():
                with m.If(valid):
                    d += self.apply.eq(1)
                    d += self.command.eq(holding)
                    d += valid.eq(0)
                with m.If(~fifo.r_empty):
                    d += holding.eq(fifo.r_data)
                    d += delay.eq(load_delay)
                    d += valid.eq(1)

        # The combinational read port means the head is already on r_data, so
        # popping is just a one-cycle pulse alongside the fetch. A command is
        # consumed when it is fetched, or when it is applied and the next one is
        # fetched in the same sample.
        m.d.comb += fifo.r_inc.eq(
            self.strobe & ~fifo.r_empty & (~valid | (delay == 0))
        )
        m.d.comb += self.valid.eq(valid)

        return m


class VoiceControl(Elaboratable):
    """Decode applied commands into one voice's parameters."""

    def __init__(self, *, waves: int = len(WAVES), domain: str = "pixel"):
        self.wave_count = waves
        self.wave_bits = max(1, (waves - 1).bit_length())
        self.domain = domain

        self.apply = Signal()
        self.command = Signal(64)

        self.step = Signal(PHASE_BITS)
        self.wave = Signal(range(waves))
        self.amp = Signal(SAMPLE_BITS)
        self.reset_phase = Signal()

    def elaborate(self, platform):
        m = Module()
        d = m.d[self.domain]

        opcode = self.command[56:64]
        value = self.command[16:48]
        amplitude = Mux(value[:SAMPLE_BITS] > PEAK, PEAK, value[:SAMPLE_BITS])

        d += self.reset_phase.eq(0)

        with m.If(self.apply):
            with m.Switch(opcode):
                with m.Case(OP_NOTE_ON):
                    d += self.step.eq(value)
                    d += self.amp.eq(PEAK)
                with m.Case(OP_NOTE_OFF):
                    d += self.amp.eq(0)
                with m.Case(OP_SET_FREQ):
                    d += self.step.eq(value)
                with m.Case(OP_SET_WAVE):
                    d += self.wave.eq(value[: self.wave_bits])
                with m.Case(OP_SET_AMP):
                    d += self.amp.eq(amplitude)
                with m.Case(OP_RESET):
                    d += self.step.eq(0)
                    d += self.amp.eq(0)
                    d += self.wave.eq(0)
                    d += self.reset_phase.eq(1)

        return m
