"""Phase 0: clock generation + DVI video output.

Acceptance gate (see PLAN.md): a stable 640x480 @ 60.00 Hz picture on the
monitor. Audio is disabled (``DVI_OUTPUT=1``) so that the HDMI data-island
machinery is not yet in the critical path.
"""

from __future__ import annotations

from amaranth import Elaboratable, Module, Signal

from xsynth.hdl.clock import ClockDomains, PowerOnReset, XsynthClocks
from xsynth.hdl.hdmi import HDMIOutput
from xsynth.hdl.video import make_pattern
from xsynth.hdl.video_modes import DEFAULT_MODE, VideoMode


class StatusLeds(Elaboratable):
    """Board bring-up indicators.

    * LED0 blinks off the raw 27 MHz clock: proves the design is configured.
    * LED1 is on iff the PLL reports lock.
    * LED2 blinks off the PLL pixel clock: proves the PLL is producing a clock
      and the fabric is clocked by it.
    """

    def __init__(self, clocks: XsynthClocks):
        self.clocks = clocks

    def elaborate(self, platform):
        m = Module()

        heartbeat27 = Signal(24, init=0)
        m.d.sync += heartbeat27.eq(heartbeat27 + 1)

        heartbeat_pixel = Signal(24, init=0)
        m.d.pixel += heartbeat_pixel.eq(heartbeat_pixel + 1)

        led0 = platform.request("led", 0)
        led1 = platform.request("led", 1)
        led2 = platform.request("led", 2)
        m.d.comb += [
            led0.o.eq(heartbeat27[-1]),
            led1.o.eq(self.clocks.locked),
            led2.o.eq(heartbeat_pixel[-1]),
        ]

        return m


class Phase0(Elaboratable):
    def __init__(self, mode: VideoMode = DEFAULT_MODE, *, audio_bits: int = 16,
                 pattern: str = "bars"):
        self.mode = mode
        self.audio_bits = audio_bits
        self.pattern = pattern

    def elaborate(self, platform):
        m = Module()
        mode = self.mode

        clocks = XsynthClocks(mode)
        m.submodules.clocks = clocks
        m.submodules.domains = ClockDomains(clocks)
        m.submodules.reset = reset = PowerOnReset(clocks.locked)

        hdmi_pins = platform.request("hdmi", dir={"clk": "o", "d": "o"})

        m.submodules.hdmi = hdmi = HDMIOutput(mode, audio_bits=self.audio_bits)
        m.submodules.pattern = pattern = make_pattern(
            self.pattern,
            screen_width=mode.screen_width,
            screen_height=mode.screen_height,
        )

        m.d.comb += [
            hdmi.clk_pixel.eq(clocks.clk_pixel),
            hdmi.clk_pixel_x5.eq(clocks.clk_pixel_x5),
            hdmi.clk_audio.eq(clocks.clk_audio),
            hdmi.reset.eq(reset.reset),
            pattern.cx.eq(hdmi.cx),
            pattern.cy.eq(hdmi.cy),
            hdmi.rgb.eq(pattern.rgb),
            hdmi_pins.d.o.eq(hdmi.tmds),
            hdmi_pins.clk.o.eq(hdmi.tmds_clock),
        ]

        m.submodules.leds = StatusLeds(clocks)

        return m
