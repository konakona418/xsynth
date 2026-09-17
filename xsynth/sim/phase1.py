"""Phase 1 simulation: the DDS over 0.1 s of audio.

Drives the strobe at exactly the hardware rate (one pulse per 525 pixel clocks)
and checks the tone the accumulator actually produces, rather than the tone we
asked for.
"""

from __future__ import annotations

from amaranth.sim import Simulator

from xsynth.hdl.audio import (
    DEFAULT_TONE_HZ,
    PEAK,
    SineDDS,
    phase_step,
    to_signed,
)

PIXEL_HZ = 25_200_000
SAMPLE_RATE = 48_000
PIXELS_PER_SAMPLE = PIXEL_HZ // SAMPLE_RATE  # 525
NUM_SAMPLES = 4800  # 0.1 s


def measure_frequency(samples: list[int], sample_rate: int) -> float:
    """Tone frequency from the rising zero crossings of a signed signal.

    Each crossing is interpolated between the two samples that straddle zero.
    Counting whole samples instead quantises the answer to the sample spacing,
    which over a window of ten periods is a tenth of a percent -- the same size
    as the errors these tests exist to catch.
    """
    crossings = [
        i - 1 + (-samples[i - 1] / (samples[i] - samples[i - 1]))
        for i in range(1, len(samples))
        if samples[i - 1] < 0 <= samples[i]
    ]
    assert len(crossings) >= 3, "too few zero crossings to measure a frequency"
    periods = crossings[-1] - crossings[0]
    return (len(crossings) - 1) * sample_rate / periods


def run(*, vcd: str | None = None) -> None:
    dut = SineDDS(phase_step(DEFAULT_TONE_HZ, SAMPLE_RATE))

    samples: list[int] = []

    async def bench(ctx):
        for _ in range(NUM_SAMPLES):
            ctx.set(dut.strobe, 1)
            await ctx.tick("pixel")
            ctx.set(dut.strobe, 0)
            for _ in range(PIXELS_PER_SAMPLE - 1):
                await ctx.tick("pixel")
            samples.append(to_signed(ctx.get(dut.sample)))

    sim = Simulator(dut)
    sim.add_clock(1 / PIXEL_HZ, domain="pixel")
    sim.add_testbench(bench)
    if vcd:
        with sim.write_vcd(vcd):
            sim.run()
    else:
        sim.run()

    assert min(samples) < -PEAK // 2, "tone never swung low"
    assert max(samples) > PEAK // 2, "tone never swung high"

    measured = measure_frequency(samples, SAMPLE_RATE)
    assert abs(measured - DEFAULT_TONE_HZ) < 1.0, (
        f"measured {measured:.2f} Hz, expected {DEFAULT_TONE_HZ} Hz"
    )
