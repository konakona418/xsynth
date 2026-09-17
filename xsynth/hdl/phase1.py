"""Phase 1: video plus a real 48 kHz audio path.

Acceptance gate (see PLAN.md): the Phase 0 picture **and** an audible 440 Hz
tone. Unlike Phase 0 this builds a true HDMI signal (``DVI_OUTPUT=0``), so the
data-island machinery is now in the critical path.

The tone is a single :class:`~xsynth.hdl.audio.SineDDS` voice advanced once per
48 kHz sample. The sample strobe is produced by the same pixel-clock divider
that generates ``clk_audio``, so everything still runs on the pixel clock.
"""

from __future__ import annotations

from amaranth import Elaboratable, Module, Signal

from xsynth.hdl.audio import DEFAULT_TONE_HZ, SAMPLE_BITS, SineDDS, phase_step
from xsynth.hdl.clock import ClockDomains, PowerOnReset, XsynthClocks
from xsynth.hdl.hdmi import HDMIOutput
from xsynth.hdl.phase0 import StatusLeds
from xsynth.hdl.video import make_pattern
from xsynth.hdl.video_modes import DEFAULT_MODE, VideoMode


class AudioLeds(Elaboratable):
    """LED3 blinks while audio samples are being produced.

    The sample strobe is far too fast to see, so it drives a counter and the
    most significant bit is shown: about 0.7 Hz at 48 kHz.
    """

    def __init__(self, strobe, *, bit: int = 16):
        self.strobe = strobe
        self.bit = bit

    def elaborate(self, platform):
        m = Module()
        counter = Signal(self.bit + 1, init=0)
        with m.If(self.strobe):
            m.d.pixel += counter.eq(counter + 1)

        led = platform.request("led", 3)
        m.d.comb += led.o.eq(counter[self.bit])
        return m


class Phase1(Elaboratable):
    def __init__(self, mode: VideoMode = DEFAULT_MODE, *, audio_bits: int = 16,
                 pattern: str = "bars", tone_hz: float = DEFAULT_TONE_HZ):
        if audio_bits != SAMPLE_BITS:
            raise ValueError(
                f"the DDS produces {SAMPLE_BITS}-bit samples but the HDMI core "
                f"was configured for {audio_bits}-bit audio"
            )
        self.mode = mode
        self.audio_bits = audio_bits
        self.pattern = pattern
        self.tone_hz = tone_hz

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
        m.submodules.dds = dds = SineDDS(
            phase_step(self.tone_hz, mode.audio_clock_hz)
        )

        m.d.comb += [
            hdmi.clk_pixel.eq(clocks.clk_pixel),
            hdmi.clk_pixel_x5.eq(clocks.clk_pixel_x5),
            hdmi.clk_audio.eq(clocks.clk_audio),
            hdmi.reset.eq(reset.reset),
            pattern.cx.eq(hdmi.cx),
            pattern.cy.eq(hdmi.cy),
            hdmi.rgb.eq(pattern.rgb),
            dds.strobe.eq(clocks.audio_strobe),
            # A mono tone for now; Phase 2 splits the voices across channels.
            hdmi.audio_left.eq(dds.sample),
            hdmi.audio_right.eq(dds.sample),
            hdmi_pins.d.o.eq(hdmi.tmds),
            hdmi_pins.clk.o.eq(hdmi.tmds_clock),
        ]

        m.submodules.leds = StatusLeds(clocks)
        m.submodules.audio_leds = AudioLeds(clocks.audio_strobe)

        return m
