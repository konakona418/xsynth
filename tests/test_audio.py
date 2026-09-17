# amaranth: UnusedElaboratable=disable
"""Audio: sine table and DDS tests.

The pragma above is needed because the constructor-rejection tests deliberately
build an invalid ``SineDDS`` and throw it away; Amaranth's unused-elaboratable
tracking would otherwise flag every one of those.
"""

import math

import pytest
from amaranth.sim import Simulator

from xsynth.hdl.audio import (
    PEAK,
    SAMPLE_MASK,
    TABLE_SIZE,
    SineDDS,
    phase_step,
    sine_table,
    to_signed,
)
from xsynth.hdl.video_modes import VGA_640X480

SAMPLE_RATE = 48_000
TONE_HZ = 440.0


def test_table_has_one_full_period():
    table = sine_table()
    assert len(table) == TABLE_SIZE
    assert all(0 <= value <= SAMPLE_MASK for value in table)

    # A signed sine: starts and ends at zero, peaks a quarter of the way in.
    assert table[0] == 0
    assert table[TABLE_SIZE // 4] == PEAK
    assert table[TABLE_SIZE // 2] == 0
    assert table[3 * TABLE_SIZE // 4] == -PEAK & SAMPLE_MASK


def test_table_is_antisymmetric_about_zero():
    table = sine_table()
    for i in range(TABLE_SIZE):
        opposite = table[(i + TABLE_SIZE // 2) % TABLE_SIZE]
        assert (table[i] + opposite) & SAMPLE_MASK == 0


def test_table_matches_the_ideal_sine():
    table = sine_table()
    for i in range(TABLE_SIZE):
        ideal = round(PEAK * math.sin(2 * math.pi * i / TABLE_SIZE))
        assert to_signed(table[i]) == ideal


def test_table_uses_the_signed_range():
    table = sine_table()
    signed = [to_signed(value) for value in table]
    assert min(signed) == -PEAK
    assert max(signed) == PEAK


def test_440_hz_phase_step_is_accurate():
    step = phase_step(TONE_HZ, SAMPLE_RATE)
    produced = step * SAMPLE_RATE / (1 << 32)
    assert abs(produced - TONE_HZ) < 0.001


def test_phase_step_rejects_impossible_tones():
    with pytest.raises(ValueError):
        phase_step(-1, SAMPLE_RATE)
    with pytest.raises(ValueError):
        phase_step(SAMPLE_RATE, SAMPLE_RATE)
    with pytest.raises(ValueError):
        phase_step(2 * SAMPLE_RATE, SAMPLE_RATE)


def test_dds_rejects_a_zero_step():
    with pytest.raises(ValueError):
        SineDDS(0)


def test_dds_produces_a_full_scale_sine_at_the_requested_frequency():
    dut = SineDDS(phase_step(TONE_HZ, SAMPLE_RATE))
    samples: list[int] = []

    async def bench(ctx):
        for _ in range(4800):
            ctx.set(dut.strobe, 1)
            await ctx.tick("pixel")
            samples.append(to_signed(ctx.get(dut.sample)))

    sim = Simulator(dut)
    sim.add_clock(1 / 25.2e6, domain="pixel")
    sim.add_testbench(bench)
    sim.run()

    assert min(samples) < -PEAK // 2
    assert max(samples) > PEAK // 2

    crossings = [
        i for i in range(1, len(samples)) if samples[i - 1] < 0 <= samples[i]
    ]
    periods = crossings[-1] - crossings[0]
    measured = (len(crossings) - 1) * SAMPLE_RATE / periods
    assert abs(measured - TONE_HZ) < 1.0


def test_vga_sample_rate_is_exactly_48khz():
    assert VGA_640X480.audio_clock_hz == SAMPLE_RATE
