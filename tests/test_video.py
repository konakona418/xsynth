"""Colour-bar generator tests."""

from amaranth.sim import Simulator

from xsynth.hdl.video import NUM_BARS, ColorBars

SCREEN_WIDTH = 640
SCREEN_HEIGHT = 480


def _sample_bar_colours():
    dut = ColorBars(screen_width=SCREEN_WIDTH, screen_height=SCREEN_HEIGHT)
    colours = {}

    async def bench(ctx):
        # Sample the middle of each bar on a line away from the marker.
        for index in range(NUM_BARS):
            x = index * (SCREEN_WIDTH // NUM_BARS) + (SCREEN_WIDTH // NUM_BARS) // 2
            ctx.set(dut.cx, x)
            ctx.set(dut.cy, 10)
            await ctx.tick("pixel")
            colours[index] = ctx.get(dut.rgb)

    sim = Simulator(dut)
    sim.add_clock(1 / 25.2e6, domain="pixel")
    sim.add_testbench(bench)
    sim.run()
    return colours


def test_bars_are_distinct_and_ordered_white_to_black():
    colours = _sample_bar_colours()
    assert colours[0] == 0xBFBFBF  # white
    assert colours[NUM_BARS - 1] == 0x000000  # black
    assert len(set(colours.values())) == NUM_BARS


def test_phase0_simulation_passes():
    from xsynth.sim.phase0 import run

    run()
