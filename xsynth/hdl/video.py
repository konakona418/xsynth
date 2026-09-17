"""Video test pattern generation for bring-up and diagnostics.

The HDMI core reports the pixel coordinate it wants on ``cx``/``cy`` and the
geometry it is generating. We only need to present ``rgb`` one pixel early (the
core registers it), which is exactly the synchronous contract the core expects.
"""

from __future__ import annotations

from amaranth import Elaboratable, Module, Signal

# 75% SMPTE-style bars: white, yellow, cyan, green, magenta, red, blue, black.
_BARS = (
    0xBFBFBF,
    0xBFBF00,
    0x00BFBF,
    0x00BF00,
    0xBF00BF,
    0xBF0000,
    0x0000BF,
    0x000000,
)

NUM_BARS = len(_BARS)


class SolidColor(Elaboratable):
    """A flat colour field.

    Exposes the same ``cx``/``cy``/``rgb`` interface as :class:`ColorBars` so
    the top level can wire any pattern identically. ``cx``/``cy`` are unused.
    """

    def __init__(self, rgb: int = 0xFFFFFF, *, screen_width=640, screen_height=480):
        self.value = rgb
        self.cx = Signal(range(screen_width))
        self.cy = Signal(range(screen_height))
        self.rgb = Signal(24)

    def elaborate(self, platform):
        m = Module()
        m.d.comb += self.rgb.eq(self.value)
        return m


class ColorCycle(Elaboratable):
    """Cycle through solid colours, one per second.

    Used for link bring-up: if the capture device sees the colours change, the
    whole TMDS path works and any remaining problem is in the pattern logic.
    """

    def __init__(self, colours=(0xFFFFFF, 0xFF0000, 0x00FF00, 0x0000FF),
                 frames_per_colour=60, *, screen_width=640, screen_height=480):
        self.colours = tuple(colours)
        self.frames_per_colour = frames_per_colour
        self.screen_width = screen_width
        self.screen_height = screen_height

        self.cx = Signal(range(screen_width))
        self.cy = Signal(range(screen_height))
        self.rgb = Signal(24)

    def elaborate(self, platform):
        m = Module()

        frames = Signal(range(self.frames_per_colour))
        index = Signal(range(len(self.colours)))

        at_frame_end = (
            (self.cx == self.screen_width - 1)
            & (self.cy == self.screen_height - 1)
        )
        with m.If(at_frame_end):
            with m.If(frames == self.frames_per_colour - 1):
                m.d.pixel += frames.eq(0)
                with m.If(index == len(self.colours) - 1):
                    m.d.pixel += index.eq(0)
                with m.Else():
                    m.d.pixel += index.eq(index + 1)
            with m.Else():
                m.d.pixel += frames.eq(frames + 1)

        with m.Switch(index):
            for i, colour in enumerate(self.colours):
                with m.Case(i):
                    m.d.comb += self.rgb.eq(colour)

        return m


def make_pattern(pattern: str, *, screen_width: int = 640,
                 screen_height: int = 480) -> Elaboratable:
    """Build a video pattern by name: ``bars``, ``cycle``, or a hex colour."""
    if pattern == "bars":
        return ColorBars(screen_width=screen_width, screen_height=screen_height)
    if pattern == "cycle":
        return ColorCycle(screen_width=screen_width, screen_height=screen_height)
    try:
        value = int(pattern, 16)
    except ValueError:
        raise ValueError(
            f"unknown pattern {pattern!r}; expected 'bars', 'cycle' or a hex colour"
        ) from None
    if not 0 <= value <= 0xFFFFFF:
        raise ValueError(f"colour {pattern!r} is not a 24-bit RGB value")
    return SolidColor(
        value, screen_width=screen_width, screen_height=screen_height
    )


class ColorBars(Elaboratable):
    """Eight vertical colour bars with a sweeping liveness marker.

    ``cx``/``cy`` are driven by the HDMI core. ``sweep`` moves a white block
    across the screen once per frame so that a frozen picture is immediately
    obvious on the monitor.
    """

    def __init__(self, *, screen_width=640, screen_height=480):
        self.screen_width = screen_width
        self.screen_height = screen_height

        self.cx = Signal(range(screen_width))
        self.cy = Signal(range(screen_height))
        self.rgb = Signal(24)

    def elaborate(self, platform):
        m = Module()

        bar_width = (self.screen_width + NUM_BARS - 1) // NUM_BARS
        bar_index = Signal(range(NUM_BARS))
        m.d.comb += bar_index.eq(self.cx // bar_width)

        bar_rgb = Signal(24)
        with m.Switch(bar_index):
            for index, value in enumerate(_BARS):
                with m.Case(index):
                    m.d.comb += bar_rgb.eq(value)

        # Liveness marker: one pixel per frame, wrapped horizontally.
        sweep = Signal(range(self.screen_width), init=0)
        at_frame_end = (
            (self.cx == self.screen_width - 1)
            & (self.cy == self.screen_height - 1)
        )
        with m.If(at_frame_end):
            with m.If(sweep == self.screen_width - 1):
                m.d.pixel += sweep.eq(0)
            with m.Else():
                m.d.pixel += sweep.eq(sweep + 1)

        marker = (
            (self.cx >= sweep)
            & (self.cx < sweep + 8)
            & (self.cy >= self.screen_height // 2)
            & (self.cy < self.screen_height // 2 + 8)
        )

        with m.If(marker):
            m.d.comb += self.rgb.eq(0xFFFFFF)
        with m.Else():
            m.d.comb += self.rgb.eq(bar_rgb)

        return m
