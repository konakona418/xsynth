"""Tests for the vendored hdl-util patch layer."""

import pytest

from xsynth.hdl.video_modes import HD_1280X720, VGA_640X480
from xsynth.platform import hdmi_patch
from xsynth.platform.hdmi_patch import (
    HDMI_SOURCES,
    WRAPPER_SOURCES,
    build_files,
    build_filenames,
    slang_defines,
)


def test_every_patch_still_applies_exactly_once():
    # build_files() raises if an upstream patch no longer matches exactly once.
    files = build_files()
    assert set(files) == set(build_filenames())


def test_patched_sources_contain_no_blocking_mixing():
    files = build_files()
    packet_picker = files["packet_picker.sv.svp"]
    assert "frame_counter = frame_counter" not in packet_picker

    hdmi = files["hdmi.sv.svp"]
    assert "control_data <= 6'd0;" in hdmi
    # The declaration keeps its initializer; only the reset branch is patched.
    assert "                control_data = 6'd0;" not in hdmi


def test_generated_sources_use_a_non_sv_suffix():
    # A ".sv" suffix would make Amaranth feed them to read_verilog -sv, which
    # cannot parse unpacked-array ports.
    for name in build_filenames():
        assert not name.endswith(".sv")


def test_build_filenames_order_puts_the_wrapper_last():
    names = build_filenames()
    assert names[-1] == WRAPPER_SOURCES[-1] + hdmi_patch.GENERATED_SUFFIX
    assert len(names) == len(HDMI_SOURCES) + len(WRAPPER_SOURCES)


def test_slang_defines_cover_wrapper_configuration():
    defines = slang_defines(
        video_mode=VGA_640X480, dvi_output=False, audio_rate=48000, audio_bits=16
    )
    assert "GW_IDE" in defines
    assert "XSYNTH_VIDEO_ID_CODE=1" in defines
    assert "XSYNTH_BIT_WIDTH=10" in defines
    assert "XSYNTH_DVI_OUTPUT=0" in defines
    assert "XSYNTH_AUDIO_RATE=48000" in defines
    assert "XSYNTH_AUDIO_BITS=16" in defines


def test_slang_defines_follow_the_video_mode():
    defines = slang_defines(
        video_mode=HD_1280X720, dvi_output=True, audio_rate=48000, audio_bits=16
    )
    assert "XSYNTH_VIDEO_ID_CODE=4" in defines
    assert "XSYNTH_BIT_WIDTH=11" in defines


def test_missing_patch_raises(monkeypatch):
    monkeypatch.setattr(
        hdmi_patch, "_PATCHES", {"hdmi.sv": [("bogus", "not-present", "x")]}
    )
    with pytest.raises(RuntimeError):
        hdmi_patch.build_files()
