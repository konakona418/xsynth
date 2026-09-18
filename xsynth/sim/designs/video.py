"""The video design in simulation: the colour-bar generator over one frame."""

from __future__ import annotations

from amaranth.sim import Simulator

from xsynth.hdl.video import NUM_BARS, ColorBars

SCREEN_WIDTH = 640
SCREEN_HEIGHT = 480


def run(*, vcd: str | None = None) -> None:
    dut = ColorBars(screen_width=SCREEN_WIDTH, screen_height=SCREEN_HEIGHT)

    seen: set[int] = set()
    marker_pixels = 0

    async def bench(ctx):
        nonlocal marker_pixels
        for y in range(SCREEN_HEIGHT):
            for x in range(SCREEN_WIDTH):
                ctx.set(dut.cx, x)
                ctx.set(dut.cy, y)
                await ctx.tick("pixel")
                rgb = ctx.get(dut.rgb)
                seen.add(rgb)
                if rgb == 0xFFFFFF:
                    marker_pixels += 1

    sim = Simulator(dut)
    sim.add_clock(1 / 25.2e6, domain="pixel")
    sim.add_testbench(bench)
    if vcd:
        with sim.write_vcd(vcd):
            sim.run()
    else:
        sim.run()

    assert len(seen) >= NUM_BARS, f"expected at least {NUM_BARS} distinct colours, saw {len(seen)}"
    assert marker_pixels > 0, "liveness marker was never drawn"
