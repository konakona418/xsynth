"""Amaranth wrapper around the vendored, slang-compiled ``xsynth_hdmi`` core."""

from __future__ import annotations

from amaranth import Elaboratable, Instance, Module, Signal

from xsynth.hdl.video_modes import DEFAULT_MODE, VideoMode


class HDMIOutput(Elaboratable):
    """Drive the ``xsynth_hdmi`` SystemVerilog core.

    ``xsynth_hdmi`` is compiled from SystemVerilog by the slang frontend (see
    :mod:`xsynth.platform.hdmi_patch`); Amaranth only ever sees it as a black
    box.

    Its configuration (video mode, DVI vs HDMI, audio rate, audio width) is
    baked in at synthesis time through preprocessor defines, because the slang
    frontend strips parameters from top-level modules. The geometry signal
    widths must match the video ID code, which is why both come from the same
    :class:`~xsynth.hdl.video_modes.VideoMode`.
    """

    def __init__(self, mode: VideoMode = DEFAULT_MODE, *, audio_bits: int = 16):
        if audio_bits not in (16, 20, 24):
            raise ValueError("audio_bits must be 16, 20 or 24")

        self.mode = mode
        self.audio_bits = audio_bits

        self.clk_pixel = Signal()
        self.clk_pixel_x5 = Signal()
        self.clk_audio = Signal()
        self.reset = Signal()
        self.rgb = Signal(24)
        self.audio_left = Signal(audio_bits)
        self.audio_right = Signal(audio_bits)

        self.tmds = Signal(3)
        self.tmds_clock = Signal()

        self.cx = Signal(mode.bit_width)
        self.cy = Signal(mode.bit_height)
        self.frame_width = Signal(mode.bit_width)
        self.frame_height = Signal(mode.bit_height)
        self.screen_width = Signal(mode.bit_width)
        self.screen_height = Signal(mode.bit_height)

    def elaborate(self, platform):
        m = Module()

        m.submodules.core = Instance(
            "xsynth_hdmi",
            i_clk_pixel=self.clk_pixel,
            i_clk_pixel_x5=self.clk_pixel_x5,
            i_clk_audio=self.clk_audio,
            i_reset=self.reset,
            i_rgb=self.rgb,
            i_audio_left=self.audio_left,
            i_audio_right=self.audio_right,
            o_tmds=self.tmds,
            o_tmds_clock=self.tmds_clock,
            o_cx=self.cx,
            o_cy=self.cy,
            o_frame_width=self.frame_width,
            o_frame_height=self.frame_height,
            o_screen_width=self.screen_width,
            o_screen_height=self.screen_height,
        )

        return m
