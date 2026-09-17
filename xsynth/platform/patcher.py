"""Toolchain patches for the Apicula (open-source Gowin) flow.

Two things need adapting:

1. ``nextpnr-himbaechel-gowin`` no longer accepts ``--family``/``--cst``; the
   values must be passed through ``--vopt``.

2. Amaranth reads added SystemVerilog with ``read_verilog -sv``, which cannot
   parse the unpacked-array ports used by hdl-util/hdmi. We replace the Yosys
   script's ``read_rtlil {{name}}.il`` line with a pre-load of the Gowin cell
   library plus a ``read_slang`` invocation over the patched sources, so the
   HDMI core is elaborated by slang and handed to synthesis as RTLIL.

``openFPGALoader`` is also patched: it will not program to flash by default,
and its absence should produce a clear error rather than a crash.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from xsynth.platform.gowin_io import install_elvds_buffers
from xsynth.platform.hdmi_patch import build_files, build_filenames, slang_defines

_GOWIN_PATCHED = False
_ORIGINAL_YOSYS_TEMPLATE: str | None = None

# Amaranth looks up tools by generic name (``yosys``, ``nextpnr-gowin``,
# ``gowin_pack``). The YoWASP distributions ship under different names, so point
# Amaranth at them unless the user has already configured an environment.
_DEFAULT_TOOLS = {
    "YOSYS": "yowasp-yosys",
    "NEXTPNR_GOWIN": "yowasp-nextpnr-himbaechel-gowin",
    "GOWIN_PACK": "gowin_pack",
}


def configure_toolchain_environment() -> None:
    """Point Amaranth at the YoWASP toolchain if the user has not overridden it."""
    for variable, tool in _DEFAULT_TOOLS.items():
        os.environ.setdefault(variable, tool)


def patch_gowin_platform() -> None:
    """Install the Apicula command templates."""
    global _GOWIN_PATCHED

    from amaranth.vendor._gowin import GowinPlatform

    if _GOWIN_PATCHED:
        return

    GowinPlatform._apicula_command_templates = [
        r"""
        {{invoke_tool("yosys")}}
            {{quiet("-q")}}
            {{get_override("yosys_opts")|options}}
            -l {{name}}.rpt
            {{name}}.ys
        """,
        r"""
        {{invoke_tool("nextpnr-gowin")}}
            {{quiet("--quiet")}}
            {{get_override("nextpnr_opts")|options}}
            --log {{name}}.tim
            --device {{platform.part}}
            --json {{name}}.syn.json
            --write {{name}}.pnr.json
            --sdc xsynth_timing.sdc
            --vopt family={{platform._chipdb_device}}
            --vopt cst={{name}}.cst
        """,
        r"""
        {{invoke_tool("gowin_pack")}}
            -d {{platform._chipdb_device}}
            -o {{name}}.fs
            {{get_override("gowin_pack_opts")|options}}
            {{name}}.pnr.json
        """,
    ]
    _GOWIN_PATCHED = True


def _patch_yosys_template(defines: list[str]) -> None:
    """Inject a slang-based read of the HDMI sources into the Yosys script.

    Always re-derived from the pristine template so that building a different
    configuration in the same process cannot accumulate stale defines.
    """
    global _ORIGINAL_YOSYS_TEMPLATE

    from amaranth.vendor._gowin import GowinPlatform

    templates = GowinPlatform._apicula_file_templates
    if _ORIGINAL_YOSYS_TEMPLATE is None:
        _ORIGINAL_YOSYS_TEMPLATE = templates["{{name}}.ys"]

    old = "read_rtlil {{name}}.il"
    if old not in _ORIGINAL_YOSYS_TEMPLATE:
        raise RuntimeError(
            "Amaranth's Gowin Yosys template changed; update "
            "xsynth/platform/patcher.py."
        )

    define_flags = " ".join(f"-D {define}" for define in defines)
    slang = " ".join(build_filenames())
    injected = (
        "read_verilog -specify -lib +/gowin/cells_sim.v\n"
        # ELVDS_TBUF/ELVDS_IBUF live in the family-specific extra cell library,
        # which synth_gowin only reads after the design; pre-load it so the
        # Amaranth netlist can instantiate them.
        "read_verilog -specify -lib +/gowin/cells_xtra_gw1n.v\n"
        f"read_slang -j 1 --no-synthesis-define {define_flags} {slang}\n"
        "read_rtlil {{name}}.il"
    )
    templates["{{name}}.ys"] = _ORIGINAL_YOSYS_TEMPLATE.replace(old, injected)


# Amaranth's Apicula flow never passes clock constraints to nextpnr, which then
# falls back to its 12 MHz default and reports a meaningless "PASS". Constrain
# every clock explicitly, including the raw 27 MHz oscillator that the control
# domain runs on.
CONTROL_CLOCK_HZ = 27_000_000


def _timing_sdc(video_mode, dvi_output: bool = False) -> str:
    control_period_ns = 1e9 / CONTROL_CLOCK_HZ
    pixel_period_ns = 1e9 / video_mode.pixel_clock_hz
    x5_period_ns = 1e9 / video_mode.pixel_clock_x5_hz
    constraints = [
        f"create_clock -name clk -period {control_period_ns:.6f} "
        "[get_nets clk]",
        f"create_clock -name clk_pixel -period {pixel_period_ns:.6f} "
        "[get_nets clk_pixel]",
        f"create_clock -name clk_pixel_x5 -period {x5_period_ns:.6f} "
        "[get_nets clk_pixel_x5]",
    ]
    if not dvi_output:
        # DVI mode has no data islands, so the HDMI core does not consume
        # clk_audio and the net is optimised away; constraining it would warn.
        audio_period_ns = 1e9 / video_mode.audio_clock_hz
        constraints.append(
            f"create_clock -name clk_audio -period {audio_period_ns:.6f} "
            "[get_nets clk_audio]"
        )
    return "\n".join(constraints) + "\n"


def install_hdmi_sources(platform, *, video_mode, dvi_output: bool = True,
                         audio_rate: int = 48000, audio_bits: int = 16) -> None:
    """Configure the HDMI build and attach the patched sources to the plan.

    They use a ``.svp`` suffix so Amaranth's ``read_verilog -sv`` loop ignores
    them; the injected ``read_slang`` line reads them by name instead.
    """
    defines = slang_defines(
        video_mode=video_mode, dvi_output=dvi_output,
        audio_rate=audio_rate, audio_bits=audio_bits,
    )
    _patch_yosys_template(defines)
    platform.add_file("xsynth_timing.sdc",
                      _timing_sdc(video_mode, dvi_output))
    for filename, content in build_files().items():
        platform.add_file(filename, content)


def patch_tang_nano_9k_hdmi_resource() -> None:
    """Match the known-good Tang Nano 9K hdl-util HDMI board constraints.

    The working reference (muzhiyun/Tang_9K_HDMI) declares its differential
    pairs with ``PULL_MODE=NONE DRIVE=8`` and leaves the I/O standard at the
    board default ``LVCMOS33``; the ``ELVDS_OBUF`` primitive selects the
    emulated-LVDS output mode, not the ``IO_TYPE`` attribute.
    """
    from amaranth_boards.tang_nano_9k import TangNano9kPlatform

    for resource in TangNano9kPlatform.resources:
        if resource.name == "hdmi":
            # Do NOT override IO_TYPE: gowin_pack defaults an ELVDS buffer to
            # LVCMOS33D, and a CST IO_TYPE would override that. The working
            # reference sets no IO_TYPE on these pins either.
            resource.attrs.pop("IO_TYPE", None)
            resource.attrs["DRIVE"] = 8
            resource.attrs["PULL_MODE"] = "NONE"
            return
    raise RuntimeError("Tang Nano 9K board file no longer defines an 'hdmi' resource")


def patch_tang_nano_9k_platform(program_to_flash: bool = True) -> None:
    """Fix ``openFPGALoader`` invocation and error reporting.

    :param program_to_flash: program flash (persistent) or SRAM (volatile).
    """
    from amaranth_boards.tang_nano_9k import TangNano9kPlatform

    loader_path = os.environ.get("OPENFPGALOADER_PATH", "openFPGALoader")
    if not shutil.which(loader_path):
        raise RuntimeError(f"openFPGALoader binary not found at {loader_path}!")

    if sys.platform.startswith("win"):
        os.environ["PYTHONNOUSERSITE"] = "1"
        dll_path = os.environ.get("OPENFPGALOADER_DYNLIB_PATH", "")
        if dll_path:
            os.environ["PATH"] = dll_path + os.pathsep + os.environ["PATH"]

    def patched_toolchain_program(self, products, name):
        with products.extract("{}.fs".format(name)) as bitstream_filename:
            if program_to_flash:
                cmd = [loader_path, "-b", "tangnano9k", "-f", bitstream_filename]
            else:
                cmd = [loader_path, "-b", "tangnano9k", bitstream_filename]
            try:
                subprocess.check_call(cmd, env=os.environ)
            except subprocess.CalledProcessError as e:
                if e.returncode == 3221225781 and sys.platform.startswith("win"):
                    raise RuntimeError(
                        "Failed to load openFPGALoader DLL! Make sure the DLL is "
                        "in the PATH or specify its location using the "
                        "OPENFPGALOADER_DYNLIB_PATH environment variable."
                    ) from e
                raise

    TangNano9kPlatform.toolchain_program = patched_toolchain_program


def patch_all(program_to_flash: bool = True) -> None:
    configure_toolchain_environment()
    install_elvds_buffers()
    patch_gowin_platform()
    patch_tang_nano_9k_hdmi_resource()
    patch_tang_nano_9k_platform(program_to_flash)
