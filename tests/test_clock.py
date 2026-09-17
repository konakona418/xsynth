"""Clock and video-mode tests."""

import pytest

from xsynth.hdl.video_modes import HD_1280X720, MODES, VGA_640X480

CLKIN_HZ = 27_000_000


@pytest.mark.parametrize("mode", list(MODES.values()), ids=lambda m: m.name)
def test_pll_and_clkdiv_derive_the_mode_clocks(mode):
    pfd = CLKIN_HZ / (mode.pll_idiv_sel + 1)
    vco = pfd * (mode.pll_fbdiv_sel + 1) * mode.pll_odiv_sel
    clkout = vco / mode.pll_odiv_sel
    pixel = clkout / int(mode.clkdiv_mode)

    assert pfd == pytest.approx(mode.pll_pfd_hz)
    assert vco == pytest.approx(mode.pll_vco_hz)
    assert clkout == mode.pixel_clock_x5_hz
    assert pixel == mode.pixel_clock_hz


@pytest.mark.parametrize("mode", list(MODES.values()), ids=lambda m: m.name)
def test_operating_points_are_within_the_gw1nr9_limits(mode):
    assert 3 <= mode.pll_pfd_hz / 1e6 <= 400
    assert 400 <= mode.pll_vco_hz / 1e6 <= 1200
    assert 3.125 <= mode.pixel_clock_x5_hz / 1e6 <= 600
    assert mode.pll_odiv_sel in (2, 4, 8, 16, 32, 48, 64, 80, 96, 112, 128)
    assert int(mode.clkdiv_mode) in (2, 3, 4, 5, 8)


def test_vga_gives_an_exact_48khz_audio_clock():
    assert VGA_640X480.audio_clock_hz == 48_000
    assert VGA_640X480.audio_clock_exact
    assert VGA_640X480.pixel_clock_hz % VGA_640X480.audio_divisor == 0


def test_720p_is_video_only():
    # 74.25 MHz / 750 = 99 kHz, so this mode cannot carry 48 kHz audio exactly.
    assert not HD_1280X720.audio_clock_exact


@pytest.mark.parametrize("mode", list(MODES.values()), ids=lambda m: m.name)
def test_x5_clock_is_five_times_the_pixel_clock(mode):
    assert mode.pixel_clock_x5_hz == 5 * mode.pixel_clock_hz
