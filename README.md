# Xsynth

A hardware/software co-designed FPGA synthesizer for the Sipeed Tang Nano 9K.

The long-term goal is a RISC-V control plane (PicoRV32 + a custom `Xsynth`
instruction set) driving a real-time DSP data plane, with 48 kHz / 16-bit
stereo audio carried over HDMI. See [PLAN.md](PLAN.md) for the architecture and
the phase-by-phase plan.

## Status

Phases 0, 1, 2, 3a and 3b are implemented and verified on hardware: full HDMI
video (640x480@60), 48 kHz / 16-bit stereo audio, live control of the engine
over the USB UART, and a PicoRV32 soft core that runs programs the host
uploads — including the one that owns the command path.

| Phase | Deliverable | State |
| --- | --- | --- |
| 0 | 640x480@60 video, PLL bring-up | built, verified on hardware |
| 1 | 48 kHz HDMI audio + 440 Hz test tone | built, verified on hardware |
| 2 | UART -> command FIFO -> wavetable voice | built, verified on hardware |
| 3a | PicoRV32 SoC + program upload | built, verified on hardware |
| 3b | PCPI, firmware owns the command path | built, verified on hardware |
| 4a | 8-voice engine, ADSR, saturating mix | built and simulated |
| 4b | Firmware voice allocation + sequencing | not started |
| 5 | Sample-accurate sequencer | not started |
| 6 | Xsynth ISA + LLVM fork | not started |

Phase 4 was planned with a filter; it was dropped in favour of band-limited
wavetables, which fix the aliasing at its source rather than after it. PLAN.md
records the reasoning.

## Toolchain

The open-source flow is used, via the [YoWASP](https://yowasp.org/) WebAssembly
builds of Yosys and nextpnr, plus Apicula for Gowin bitstream packing:

```bash
uv sync
```

`uv run xsynth` points Amaranth at the YoWASP tools automatically. Only
`openFPGALoader` must be installed separately (YoWASP cannot ship it, because it
needs USB access):

```bash
# e.g. from OSS CAD Suite, or your distribution
openFPGALoader --detect
```

If `openFPGALoader` is not on `PATH`, set `OPENFPGALOADER_PATH`. On Windows,
`OPENFPGALOADER_DYNLIB_PATH` may also be needed.

## Usage

```bash
# Simulate a phase (fast, no toolchain needed)
uv run xsynth sim --phase 2

# Elaborate, synthesize, place & route, and pack a bitstream
uv run xsynth build --phase 2 --no-program

# Build and program the FPGA (SRAM; use without --no-flash for persistent flash)
uv run xsynth build --phase 2 --no-flash

# Once programmed, drive it over the USB UART
uv run xsynth host status
uv run xsynth host note-on --hz 440 --wave saw
uv run xsynth host note-off
```

The engine has eight voices. A host can name one, or leave it to the firmware
once the allocator exists; until then, `--voice` picks it:

```bash
uv run xsynth host envelope --attack 0.01 --decay 0.2 --sustain 0.5 --release 0.4
uv run xsynth host master 0.25
uv run xsynth host note-on --hz 440 --voice 0
uv run xsynth host note-on --hz 554 --voice 1
uv run xsynth host note-off --voice 0
```

Phase 3 also runs a soft core. The firmware is built with clang (the LLVM
`riscv32` target) and uploaded over the same UART:

```bash
uv run xsynth host load            # build the bundled firmware, upload, run
uv run xsynth host load image.bin  # or upload a flat binary of your own
uv run xsynth host run --stop      # put the CPU back into reset
```

`xsynth/sw/` holds the firmware sources; `xsynth/firmware.py` builds them. The C
register header is generated from the hardware's memory map, so the two cannot
drift apart.

From phase 3b the firmware is what owns the command path. The hardware still
does the wire — UART, framing and CRC — and hands each validated frame to the
core through a mailbox; the firmware parses it and pushes the commands into the
engine FIFO with a custom instruction (`xsynth.push`, claimed through PCPI).
Because that instruction stalls the CPU while the FIFO is full, a busy engine
slows the sequencer down instead of losing notes. `host load` reports the
firmware's identity through `cpu_stat`, which reads `0x5853594e` ("XSYN").

The board's onboard debugger presents two USB serial interfaces: JTAG and the
control UART. On Linux the UART is usually `/dev/ttyUSB1`; pass `--port` to
choose explicitly, or let the client pick the only USB serial port.

To check what the HDMI sink actually received, record it and measure it:

```bash
uv run xsynth analyse tone.wav
```

Phase 1 accepts `--tone <hz>` (default 440); phases 2 and 3 accept `--baud
<rate>` (default 115200). All accept `--pattern bars|cycle|<hex>` and
`--video-mode 640x480|1280x720`.

Build artifacts land in `build/`. `build/top.tim` is the nextpnr timing and
utilisation report. A phase 3 build takes roughly six minutes: the CPU doubles
the LUT count and nextpnr's placer is superlinear in density.

## The control protocol

`xsynth/protocol.py` is the single definition, shared by the FPGA and the host
tools. On the wire::

    +------+------+-----+---------------+--------+--------+
    | 0xAA | 0x55 | LEN | PAYLOAD[LEN]  | CRC_LO | CRC_HI |
    +------+------+-----+---------------+--------+--------+

The first payload byte is the packet type. Commands are fixed 64-bit words, so
the same encoding can be carried by the UART, the command FIFO and, later, the
PicoRV32 PCPI port::

    bits 63..56  opcode
    bits 55..48  voice
    bits 47..16  value
    bits 15..0   delay, in 48 kHz samples, relative to the previous command

The delay is relative, so the engine needs no absolute time base and a host can
send a whole sequence in one burst. The frame decoder validates the checksum
before anything reaches the FIFO, so a corrupted frame cannot disturb a later
one.

A command's `voice` field selects one of the eight voices for the opcodes that
name one (`NOTE_ON`, `NOTE_OFF`, `SET_FREQ`, `SET_WAVE`, `SET_AMP`). The
envelope settings are global — one set of rates shared by every voice, as on
almost every synth — while the envelope's level and stage are per voice.
`SET_AMP` is the note's own level, which scales how far its attack travels.

`OP_SET_ATTACK`, `OP_SET_DECAY` and `OP_SET_RELEASE` carry a 24-bit per-sample
increment, not a time; `XsynthClient.envelope_rate` converts seconds into one,
because that arithmetic belongs where floating point exists. `OP_SET_MASTER`
scales the whole mix, which is how a host keeps an eight-voice chord inside the
rails.

Besides `COMMANDS`, `PING` and `STATUS`, phase 3 adds `LOAD` (a target address
and a block of words, for uploading a program) and `RUN` (the CPU's run
control). The status reply carries the CPU's flags, the firmware's scratch
register and its free-running counter alongside the engine's own state.

The soft core sees a flat 32-bit space: program and data memory at address 0,
and registers at `0x1000_0000` — a status word, a free-running counter, a run
control, the 48 kHz sample counter, the mailbox (`RX_DATA`, `RX_STATUS`) and the
firmware's command count. The C header the firmware compiles against is
generated from those constants.

## Layout

```
xsynth/
  cli.py           command line entry point
  design.py        phase composition, build/simulate drivers
  firmware.py      builds the RISC-V firmware with clang/ld.lld
  protocol.py      the control protocol, shared with the host tools
  hdl/             Amaranth RTL
  host/            serial client for the control protocol
  platform/        board definitions, Gowin primitives, toolchain patches
  sim/             simulation benches (Amaranth, and iverilog for the CPU)
  sv/              Xsynth-authored SystemVerilog glue
  sw/              RISC-V firmware sources
  third_party/     vendored HDL (hdl-util/hdmi, YosysHQ/picorv32)
tests/             pytest suite
```

## Notes on the Gowin flow

A few things about this toolchain are unusual and are worth knowing before
changing the build:

* **The HDMI core is compiled with `read_slang`, not `read_verilog -sv`.**
  hdl-util/hdmi uses unpacked-array ports, which Yosys' Verilog frontend
  rejects. Amaranth's generated Yosys script is patched to pre-load the Gowin
  cell library and then run `read_slang` over the vendored sources. `-j 1` is
  required: the threaded parser crashes under WebAssembly.
* **The vendored sources are patched at build time, never edited.**
  `xsynth/platform/hdmi_patch.py` applies two fixes for blocking/non-blocking
  assignment mixing that vendor tools tolerate but slang rejects, and asserts
  each patch still matches exactly once.
* **HDMI pins are emulated LVDS, and `IO_TYPE` must not be set on them.**
  Amaranth's automatic differential buffer emits true-LVDS `TLVDS_TBUF`, which
  gowin_pack rejects on the Tang Nano 9K. Xsynth overrides the platform's I/O
  buffer to emit `ELVDS_TBUF` instead (`xsynth/platform/gowin_io.py`), which
  drives the output enable correctly. Critically, the HDMI pins must **not**
  carry an `IO_TYPE` attribute: gowin_pack defaults an `ELVDS_*` buffer to
  `LVCMOS33D`, and any `IO_TYPE` in the CST overrides that, silently leaving the
  IOB configured as a plain single-ended output. That one attribute was the
  difference between a blank screen and a picture.
* **One PLL plus one `CLKDIV` generate the clocks.** `rPLL` `CLKOUT` is 126 MHz
  and a dedicated `CLKDIV /5` gives 25.2 MHz. 25.2 MHz is chosen because
  `25200000 / 525 = 48000` exactly, so the HDMI audio clock is an exact
  division of the pixel clock. `clk_audio` itself is a fabric divider off the
  pixel clock (a pixel-domain register), which is what the known-good Tang Nano
  9K reference does; nextpnr routes and times it without complaint.
* **HDMI audio samples are signed.** hdl-util/hdmi zero-extends the 16-bit input
  to 24 bits and left-justifies it, so the input must be the two's-complement
  bit pattern. An offset-binary (unsigned) sample is read by the sink as a huge
  DC offset plus a squarish wave: the fundamental frequency is still correct,
  which makes it look like a sample-rate bug when it is not.
* **Clock constraints are passed via an SDC.** Amaranth's Apicula flow does not
  forward clock constraints to nextpnr, which would silently fall back to its
  12 MHz default. `xsynth/platform/patcher.py` adds `xsynth_timing.sdc`, with a
  constraint for every clock: the raw 27 MHz control oscillator, the pixel
  clock, its 5x clock and the fabric-generated `clk_audio`.
* **The control and audio domains meet only through an asynchronous FIFO.** The
  27 MHz control clock and the 25.2 MHz pixel clock have no fixed phase
  relationship, so commands cross in a Gray-coded dual-clock FIFO whose read
  port is combinational. That makes it first-word-fall-through, which is what
  lets the scheduler consume a command on the exact sample edge it is due.
