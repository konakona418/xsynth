"""Video timing modes supported by the HDMI core.

Each mode pins down everything that has to agree across the Amaranth RTL, the
SystemVerilog wrapper and the toolchain:

* the CEA-861 video ID code understood by hdl-util/hdmi
* the geometry output widths (they grow with the video ID code)
* the pixel clock and its 5x TMDS clock
* the rPLL and CLKDIV settings that generate those clocks

The Xsynth audio plan wants ``pixel_clock / frame_height == 48000`` exactly, so
that the 48 kHz audio clock is an exact division of the pixel clock. That holds
for 640x480 at 25.2 MHz (25.2e6 / 525 = 48000) and is the reason it is the
default. 1280x720 exists as a bring-up mode because some capture devices will
not lock to VGA input.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VideoMode:
    name: str
    video_id_code: int
    bit_width: int
    bit_height: int
    screen_width: int
    screen_height: int
    total_lines: int
    pixel_clock_hz: int
    pixel_clock_x5_hz: int
    pll_idiv_sel: int
    pll_fbdiv_sel: int
    pll_odiv_sel: int
    clkdiv_mode: str

    @property
    def pll_pfd_hz(self) -> float:
        return 27e6 / (self.pll_idiv_sel + 1)

    @property
    def pll_vco_hz(self) -> float:
        return self.pll_pfd_hz * (self.pll_fbdiv_sel + 1) * self.pll_odiv_sel

    @property
    def audio_divisor(self) -> int:
        """Divide the pixel clock by this to get the HDMI audio clock."""
        return self.total_lines

    @property
    def audio_clock_hz(self) -> int:
        return self.pixel_clock_hz // self.audio_divisor

    @property
    def audio_clock_exact(self) -> bool:
        """Whether the pixel clock divides exactly into 48 kHz."""
        return self.pixel_clock_hz % self.audio_divisor == 0 and \
            self.audio_clock_hz == 48000


VGA_640X480 = VideoMode(
    name="640x480",
    video_id_code=1,
    bit_width=10,
    bit_height=10,
    screen_width=640,
    screen_height=480,
    total_lines=525,  # 25.2 MHz / 525 = 48 kHz exactly
    pixel_clock_hz=25_200_000,
    pixel_clock_x5_hz=126_000_000,
    pll_idiv_sel=2,   # IDIV = 3   -> PFD = 9 MHz
    pll_fbdiv_sel=13,  # FBDIV = 14 -> CLKOUT = 126 MHz
    pll_odiv_sel=8,   # VCO = 1008 MHz
    clkdiv_mode="5",  # 126 / 5 = 25.2 MHz
)

HD_1280X720 = VideoMode(
    name="1280x720",
    video_id_code=4,
    bit_width=11,
    bit_height=10,
    screen_width=1280,
    screen_height=720,
    total_lines=750,  # 74.25 MHz / 750 = 99 kHz: video-only bring-up mode
    pixel_clock_hz=74_250_000,
    pixel_clock_x5_hz=371_250_000,
    pll_idiv_sel=3,    # IDIV = 4   -> PFD = 6.75 MHz
    pll_fbdiv_sel=54,  # FBDIV = 55 -> CLKOUT = 371.25 MHz
    pll_odiv_sel=2,    # VCO = 742.5 MHz
    clkdiv_mode="5",   # 371.25 / 5 = 74.25 MHz
)

MODES: dict[str, VideoMode] = {
    VGA_640X480.name: VGA_640X480,
    HD_1280X720.name: HD_1280X720,
}

DEFAULT_MODE = VGA_640X480


def get_mode(name: str) -> VideoMode:
    try:
        return MODES[name]
    except KeyError:
        raise SystemExit(
            f"unknown video mode {name!r}; known modes: {', '.join(MODES)}"
        ) from None
