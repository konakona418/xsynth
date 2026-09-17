"""Clock generation and reset for the Tang Nano 9K.

The clocks are derived from the board's 27 MHz oscillator using the same scheme
as the known-good Tang Nano 9K HDMI designs (e.g. the SVO core):

* one Gowin ``rPLL`` produces the 5x pixel clock (``CLKOUT``)
* the dedicated ``CLKDIV`` primitive divides that by 5 to give the pixel clock

For 640x480 the pixel clock is 25.2 MHz, chosen because
``25200000 / 525 = 48000`` exactly, so the HDMI audio clock is an exact integer
division of the pixel clock. The second PLL is left free.

``CLKDIV`` is used rather than the PLL's ``CLKOUTD`` divider because that is
what the proven designs do, and it guarantees the pixel clock is a clean
division of the 5x clock on a real clock net.

``clk_audio`` is a real clock produced by dividing the pixel clock by the
video mode's total line count (525 for VGA), i.e. 48 kHz. Only the rising edge
is used by the HDMI core, so the duty cycle does not matter. ``audio_strobe``
pulses on the same pixel edge that raises ``clk_audio``, which is what the
``pixel``-domain DDS advances on; that keeps the design to a single clock.
"""

from __future__ import annotations

from amaranth import (
    ClockDomain,
    ClockSignal,
    Const,
    Elaboratable,
    Instance,
    Module,
    Signal,
)

from xsynth.hdl.video_modes import DEFAULT_MODE, VideoMode


class XsynthClocks(Elaboratable):
    """Instantiate the rPLL and CLKDIV for a video mode.

    Exposes the raw 27 MHz clock too, so that logic can keep running (and
    reporting status) even if the PLL never locks.
    """

    def __init__(self, mode: VideoMode = DEFAULT_MODE):
        self.mode = mode
        self.clk27 = Signal()
        self.clk_pixel = Signal()
        self.clk_pixel_x5 = Signal()
        self.clk_audio = Signal()
        self.audio_strobe = Signal()
        self.locked = Signal()

    def elaborate(self, platform):
        m = Module()
        mode = self.mode

        clk_in = platform.request(platform.default_clk, dir="i")
        m.d.comb += self.clk27.eq(clk_in.i)

        m.submodules.pll = Instance(
            "rPLL",
            p_FCLKIN="27.0",
            p_IDIV_SEL=mode.pll_idiv_sel,
            p_FBDIV_SEL=mode.pll_fbdiv_sel,
            p_ODIV_SEL=mode.pll_odiv_sel,
            p_DYN_IDIV_SEL="FALSE",
            p_DYN_FBDIV_SEL="FALSE",
            p_DYN_ODIV_SEL="FALSE",
            p_CLKFB_SEL="INTERNAL",
            p_CLKOUTD_SRC="CLKOUT",
            p_CLKOUTD3_SRC="CLKOUT",
            i_CLKIN=self.clk27,
            i_CLKFB=Const(0),
            i_RESET=Const(0),
            i_RESET_P=Const(0),
            i_FBDSEL=Const(0, 6),
            i_IDSEL=Const(0, 6),
            i_ODSEL=Const(0, 6),
            i_PSDA=Const(0, 4),
            i_DUTYDA=Const(0, 4),
            i_FDLY=Const(0, 4),
            o_CLKOUT=self.clk_pixel_x5,
            o_LOCK=self.locked,
        )

        m.submodules.clkdiv = Instance(
            "CLKDIV",
            p_DIV_MODE=mode.clkdiv_mode,
            p_GSREN="false",
            i_HCLKIN=self.clk_pixel_x5,
            i_RESETN=self.locked,
            i_CALIB=Const(0),
            o_CLKOUT=self.clk_pixel,
        )

        divisor = mode.audio_divisor
        counter = Signal(range(divisor), init=0)
        with m.If(counter == divisor - 1):
            m.d.pixel += counter.eq(0)
        with m.Else():
            m.d.pixel += counter.eq(counter + 1)

        # Registered, so ``clk_audio`` is a clean square wave with no decoder
        # glitches: the upstream HDMI core uses it as a real clock, and the
        # known-good Tang Nano 9K reference generates it the same way (a fabric
        # divider off the pixel clock rather than a dedicated clock net).
        m.d.pixel += self.clk_audio.eq(counter < divisor // 2)

        # One pulse per sample, on the pixel edge that raises ``clk_audio``.
        # The DDS advances here; the HDMI core samples the previous value.
        m.d.comb += self.audio_strobe.eq(counter == divisor - 1)

        return m


class PowerOnReset(Elaboratable):
    """Assert ``reset`` until the PLL has been locked for a while.

    Deliberately built only from ``init=0`` state so that it needs no reset of
    its own (the pixel domains are ``reset_less``); at power-up ``locked`` is
    low, so ``reset`` is high.
    """

    HOLD_CYCLES = 63

    def __init__(self, locked):
        self.locked = locked
        self.reset = Signal()

    def elaborate(self, platform):
        m = Module()

        sync0 = Signal()
        sync1 = Signal()
        m.d.pixel += sync0.eq(self.locked)
        m.d.pixel += sync1.eq(sync0)

        counter = Signal(range(self.HOLD_CYCLES + 1), init=0)
        with m.If(~sync1):
            m.d.pixel += counter.eq(0)
        with m.Elif(counter != self.HOLD_CYCLES):
            m.d.pixel += counter.eq(counter + 1)

        m.d.comb += self.reset.eq(~sync1 | (counter != self.HOLD_CYCLES))

        return m


class ClockDomains(Elaboratable):
    """Create the ``pixel``, ``pixel_x5`` and ``sync`` clock domains.

    ``pixel``/``pixel_x5`` come from the PLL and are ``reset_less``: the only
    reset in the design is the one the HDMI core takes, generated explicitly by
    :class:`PowerOnReset`, which must not reset that generator itself.

    ``sync`` runs on the raw 27 MHz oscillator so that logic can keep working
    (and reporting status) even if the PLL never locks.
    """

    def __init__(self, clocks: XsynthClocks):
        self.clocks = clocks

    def elaborate(self, platform):
        m = Module()
        m.domains.pixel = ClockDomain("pixel", reset_less=True)
        m.domains.pixel_x5 = ClockDomain("pixel_x5", reset_less=True)
        m.domains.sync = ClockDomain("sync", reset_less=True)
        m.d.comb += [
            ClockSignal("pixel").eq(self.clocks.clk_pixel),
            ClockSignal("pixel_x5").eq(self.clocks.clk_pixel_x5),
            ClockSignal("sync").eq(self.clocks.clk27),
        ]
        return m
