"""Design composition, simulation and build entry points.

A *design* is one of the bring-up milestones, named for the layer it adds:

``video``
    The HDMI link: clocks, the colour-bar pattern and DVI output.
``audio``
    Plus the audio path: 48 kHz sample packets and a fixed test tone.
``control``
    Plus the command path: UART framing, the command FIFO and one voice.
``firmware``
    Plus the control plane: the PicoRV32 soft core owns the command path.

Each design contains the ones before it, and ``firmware`` is the full system.
They exist so a bring-up failure can be isolated to a layer.
"""

from __future__ import annotations

from amaranth import Elaboratable

from xsynth.hdl.audio import DEFAULT_TONE_HZ
from xsynth.hdl.video_modes import DEFAULT_MODE, get_mode
from xsynth.platform.boards import get_board
from xsynth.platform.patcher import install_hdmi_sources, patch_all

DESIGNS = ("video", "audio", "control", "firmware")

#: The design that has everything, and the default for build and simulate.
FULL_DESIGN = "firmware"


def _checked(design: str) -> str:
    if design not in DESIGNS:
        raise SystemExit(
            f"unknown design {design!r}; expected one of {', '.join(DESIGNS)}"
        )
    return design


def hdmi_config(design: str, mode_name: str = DEFAULT_MODE.name,
                dvi_output: bool | None = None) -> dict:
    """HDMI core configuration for a design.

    This is the single source of truth: it configures the SystemVerilog wrapper
    (via preprocessor defines), the Amaranth ``HDMIOutput`` black box that
    instantiates it, the clock generation and the timing constraints.

    ``dvi_output`` overrides the default, which is DVI for ``video`` (the
    data-island machinery is not in its critical path) and full HDMI for
    everything later, since those send audio.
    """
    mode = get_mode(mode_name)
    if dvi_output is None:
        dvi_output = design == "video"
    return {
        "video_mode": mode,
        "dvi_output": dvi_output,
        "audio_rate": 48000,
        "audio_bits": 16,
    }


def design_for(design: str, *, mode_name: str = DEFAULT_MODE.name,
               pattern: str = "bars",
               tone_hz: float = DEFAULT_TONE_HZ,
               baud: int | None = None) -> Elaboratable:
    _checked(design)
    config = hdmi_config(design, mode_name)
    if design == "video":
        from xsynth.hdl.designs.video import Video

        return Video(
            config["video_mode"],
            audio_bits=config["audio_bits"],
            pattern=pattern,
        )
    if design == "audio":
        from xsynth.hdl.designs.audio import Audio

        return Audio(
            config["video_mode"],
            audio_bits=config["audio_bits"],
            pattern=pattern,
            tone_hz=tone_hz,
        )
    if design == "control":
        from xsynth.hdl.designs.control import DEFAULT_BAUD, Control

        return Control(
            config["video_mode"],
            audio_bits=config["audio_bits"],
            pattern=pattern,
            baud=baud if baud is not None else DEFAULT_BAUD,
        )
    from xsynth.hdl.designs.control import DEFAULT_BAUD
    from xsynth.hdl.designs.firmware import Firmware

    return Firmware(
        config["video_mode"],
        audio_bits=config["audio_bits"],
        pattern=pattern,
        baud=baud if baud is not None else DEFAULT_BAUD,
    )


def build(design: str, board_name: str, *, program: bool = True,
          program_to_flash: bool = True, build_dir: str = "build",
          pattern: str = "bars", mode_name: str = DEFAULT_MODE.name,
          dvi_output: bool | None = None,
          tone_hz: float = DEFAULT_TONE_HZ,
          baud: int | None = None) -> None:
    _checked(design)
    platform = get_board(board_name)
    patch_all(program_to_flash=program_to_flash)
    if design == "firmware":
        from xsynth.hdl.soc import install_cpu_sources

        install_cpu_sources(platform)
    install_hdmi_sources(
        platform, **hdmi_config(design, mode_name, dvi_output)
    )
    top = design_for(
        design, mode_name=mode_name, pattern=pattern, tone_hz=tone_hz, baud=baud
    )
    platform.build(top, build_dir=build_dir, do_program=program)


def simulate(design: str, board_name: str, *, vcd: str | None = None) -> None:
    _checked(design)
    if design == "video":
        from xsynth.sim.designs.video import run
    elif design == "audio":
        from xsynth.sim.designs.audio import run
    elif design == "control":
        from xsynth.sim.designs.control import run
    else:
        from xsynth.sim.designs.firmware import run
    run(vcd=vcd)
