# amaranth: UnusedElaboratable=disable
"""Voice: wavetable contents and the command scheduler's sample timing."""

import math

import pytest
from amaranth import Array, Elaboratable, Module, Signal
from amaranth.sim import Simulator

from xsynth.hdl.audio import PEAK, SAMPLE_MASK, to_signed
from xsynth.hdl.fifo import AsyncFifo
from xsynth.hdl.voice import (
    TABLE_BITS,
    TABLE_SIZE,
    WAVES,
    CommandScheduler,
    WavetableVoice,
    waveform,
    wavetable_init,
)
from xsynth.protocol import OP_NOTE_ON, OP_SET_FREQ, Command

PIXEL_HZ = 25_200_000
CONTROL_HZ = 27_000_000
ADDRESS_STEP = 1 << (32 - TABLE_BITS)


def test_every_waveform_has_one_full_period_in_the_signed_range():
    for name in WAVES:
        table = waveform(name)
        assert len(table) == TABLE_SIZE
        assert all(0 <= value <= SAMPLE_MASK for value in table)
        signed = [to_signed(value) for value in table]
        assert min(signed) == -PEAK, name
        # A sawtooth ramps to just below the peak rather than reaching it.
        assert max(signed) >= PEAK - PEAK // 100, name


def test_the_sine_waveform_matches_the_ideal():
    table = waveform("sine")
    for index in range(TABLE_SIZE):
        ideal = round(PEAK * math.sin(2 * math.pi * index / TABLE_SIZE))
        assert to_signed(table[index]) == ideal


def test_the_square_waveform_is_two_levels():
    assert set(waveform("square")) == {PEAK, -PEAK & SAMPLE_MASK}


def test_the_triangle_waveform_peaks_halfway_through():
    table = waveform("triangle")
    assert to_signed(table[0]) == -PEAK
    assert to_signed(table[TABLE_SIZE // 2]) == PEAK


def test_unknown_waveforms_are_rejected():
    with pytest.raises(ValueError):
        waveform("noise")


def test_wavetable_init_concatenates_the_tables():
    init = wavetable_init(WAVES)
    assert len(init) == len(WAVES) * TABLE_SIZE
    assert init[:TABLE_SIZE] == waveform("sine")
    assert init[TABLE_SIZE:2 * TABLE_SIZE] == waveform("saw")


def _sweep(name: str, amp: int, samples: int = TABLE_SIZE + 8) -> list[int]:
    """Step the voice one table entry per sample and collect its output."""
    voice = WavetableVoice()
    collected: list[int] = []

    async def bench(ctx):
        ctx.set(voice.step, ADDRESS_STEP)
        ctx.set(voice.amp, amp)
        ctx.set(voice.wave, WAVES.index(name))
        for _ in range(samples):
            ctx.set(voice.strobe, 1)
            await ctx.tick("pixel")
            ctx.set(voice.strobe, 0)
            await ctx.tick("pixel")
            collected.append(to_signed(ctx.get(voice.sample)))

    sim = Simulator(voice)
    sim.add_clock(1 / PIXEL_HZ, domain="pixel")
    sim.add_testbench(bench)
    sim.run()
    return collected


def test_the_voice_reads_the_selected_table():
    # amp = PEAK is a scale of 32767/32768, so full scale loses one LSB.
    levels = sorted(set(_sweep("square", PEAK)))
    assert len(levels) == 2, "a square wave has only two levels"
    assert levels[1] >= PEAK - 2
    assert levels[0] <= -PEAK + 2

    sine = set(_sweep("sine", PEAK))
    assert len(sine) > 100, "a sine should use many distinct values"


def test_the_amplitude_scales_the_output():
    collected = _sweep("sine", PEAK // 2)
    assert max(collected) == pytest.approx(PEAK // 2, abs=2)
    assert min(collected) == pytest.approx(-(PEAK // 2), abs=2)


def test_a_zero_step_is_silent():
    voice = WavetableVoice()
    collected: list[int] = []

    async def bench(ctx):
        ctx.set(voice.step, 0)
        ctx.set(voice.amp, PEAK)
        ctx.set(voice.wave, WAVES.index("saw"))
        for _ in range(16):
            ctx.set(voice.strobe, 1)
            await ctx.tick("pixel")
            ctx.set(voice.strobe, 0)
            await ctx.tick("pixel")
            collected.append(ctx.get(voice.sample))

    sim = Simulator(voice)
    sim.add_clock(1 / PIXEL_HZ, domain="pixel")
    sim.add_testbench(bench)
    sim.run()
    assert set(collected) == {0}


class _SchedulerHarness(Elaboratable):
    """Preloads the FIFO from the control side and strobes from the audio side."""

    def __init__(self, commands):
        self.words = [command.word for command in commands]
        self.fifo = AsyncFifo(width=64, depth=8)
        self.scheduler = CommandScheduler(self.fifo)

    def elaborate(self, platform):
        m = Module()
        m.submodules.fifo = self.fifo
        m.submodules.scheduler = self.scheduler
        index = Signal(range(len(self.words) + 1), init=0)
        m.d.comb += [
            self.fifo.w_data.eq(Array(self.words)[index]),
            self.fifo.w_inc.eq(index < len(self.words)),
        ]
        with m.If(index < len(self.words)):
            m.d.sync += index.eq(index + 1)
        return m


def test_the_scheduler_applies_commands_at_the_requested_sample():
    harness = _SchedulerHarness([
        Command(OP_NOTE_ON, value=100, delay=0),
        Command(OP_SET_FREQ, value=200, delay=5),
        Command(OP_SET_FREQ, value=300, delay=3),
    ])
    applied: list[tuple[int, int]] = []

    async def strobe(ctx):
        for index in range(48):
            ctx.set(harness.scheduler.strobe, 1)
            await ctx.tick("pixel")
            if ctx.get(harness.scheduler.apply):
                word = ctx.get(harness.scheduler.command)
                applied.append(
                    (index, Command.unpack(word.to_bytes(8, "little")).value)
                )
            ctx.set(harness.scheduler.strobe, 0)
            await ctx.tick("pixel")

    sim = Simulator(harness)
    sim.add_clock(1 / CONTROL_HZ, domain="sync")
    sim.add_clock(1 / PIXEL_HZ, domain="pixel")
    sim.add_testbench(strobe)
    sim.run()

    assert [value for _, value in applied] == [100, 200, 300]
    samples = [index for index, _ in applied]
    assert [b - a for a, b in zip(samples, samples[1:])] == [5, 3]


def test_the_scheduler_stays_idle_with_an_empty_fifo():
    harness = _SchedulerHarness([])
    applies = 0

    async def strobe(ctx):
        nonlocal applies
        for _ in range(32):
            ctx.set(harness.scheduler.strobe, 1)
            await ctx.tick("pixel")
            if ctx.get(harness.scheduler.apply):
                applies += 1
            ctx.set(harness.scheduler.strobe, 0)
            await ctx.tick("pixel")

    sim = Simulator(harness)
    sim.add_clock(1 / CONTROL_HZ, domain="sync")
    sim.add_clock(1 / PIXEL_HZ, domain="pixel")
    sim.add_testbench(strobe)
    sim.run()
    assert applies == 0
