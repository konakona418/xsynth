"""Phase composition, simulation and build entry points."""

from __future__ import annotations

from amaranth import Elaboratable

from xsynth.hdl.audio import DEFAULT_TONE_HZ
from xsynth.hdl.video_modes import DEFAULT_MODE, get_mode
from xsynth.platform.boards import get_board
from xsynth.platform.patcher import install_hdmi_sources, patch_all


def hdmi_config_for_phase(phase: int, mode_name: str = DEFAULT_MODE.name,
                          dvi_output: bool | None = None) -> dict:
    """HDMI core configuration for a phase.

    This is the single source of truth: it configures the SystemVerilog wrapper
    (via preprocessor defines), the Amaranth ``HDMIOutput`` black box that
    instantiates it, the clock generation and the timing constraints.

    ``dvi_output`` overrides the per-phase default (Phase 0 defaults to DVI,
    later phases to full HDMI with data islands).
    """
    mode = get_mode(mode_name)
    if dvi_output is None:
        # Phase 0 is video only: the data-island machinery is not in the
        # critical path yet. Phase 1 and later send audio, so they need a full
        # HDMI signal.
        dvi_output = phase == 0
    return {
        "video_mode": mode,
        "dvi_output": dvi_output,
        "audio_rate": 48000,
        "audio_bits": 16,
    }


def design_for_phase(phase: int, *, mode_name: str = DEFAULT_MODE.name,
                     pattern: str = "bars",
                     tone_hz: float = DEFAULT_TONE_HZ,
                     baud: int | None = None) -> Elaboratable:
    config = hdmi_config_for_phase(phase, mode_name)
    if phase == 0:
        from xsynth.hdl.phase0 import Phase0

        return Phase0(
            config["video_mode"],
            audio_bits=config["audio_bits"],
            pattern=pattern,
        )
    if phase == 1:
        from xsynth.hdl.phase1 import Phase1

        return Phase1(
            config["video_mode"],
            audio_bits=config["audio_bits"],
            pattern=pattern,
            tone_hz=tone_hz,
        )
    if phase == 2:
        from xsynth.hdl.phase2 import DEFAULT_BAUD, Phase2

        return Phase2(
            config["video_mode"],
            audio_bits=config["audio_bits"],
            pattern=pattern,
            baud=baud if baud is not None else DEFAULT_BAUD,
        )
    if phase == 3:
        from xsynth.hdl.phase2 import DEFAULT_BAUD
        from xsynth.hdl.phase3 import Phase3

        return Phase3(
            config["video_mode"],
            audio_bits=config["audio_bits"],
            pattern=pattern,
            baud=baud if baud is not None else DEFAULT_BAUD,
        )
    raise SystemExit(f"phase {phase} is not implemented yet")


def build(phase: int, board_name: str, *, program: bool = True,
          program_to_flash: bool = True, build_dir: str = "build",
          pattern: str = "bars", mode_name: str = DEFAULT_MODE.name,
          dvi_output: bool | None = None,
          tone_hz: float = DEFAULT_TONE_HZ,
          baud: int | None = None) -> None:
    platform = get_board(board_name)
    patch_all(program_to_flash=program_to_flash)
    if phase >= 3:
        from xsynth.hdl.soc import install_cpu_sources

        install_cpu_sources(platform)
    install_hdmi_sources(
        platform, **hdmi_config_for_phase(phase, mode_name, dvi_output)
    )
    design = design_for_phase(
        phase, mode_name=mode_name, pattern=pattern, tone_hz=tone_hz, baud=baud
    )
    platform.build(design, build_dir=build_dir, do_program=program)


def simulate(phase: int, board_name: str, *, vcd: str | None = None) -> None:
    if phase == 0:
        from xsynth.sim.phase0 import run as run_phase0

        run_phase0(vcd=vcd)
    elif phase == 1:
        from xsynth.sim.phase1 import run as run_phase1

        run_phase1(vcd=vcd)
    elif phase == 2:
        from xsynth.sim.phase2 import run as run_phase2

        run_phase2(vcd=vcd)
    else:
        raise SystemExit(f"simulation for phase {phase} is not implemented yet")
