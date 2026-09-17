# Xsynth

A hardware/software co-designed FPGA synthesizer for the Sipeed Tang Nano 9K.

A RISC-V control plane (PicoRV32) drives a real-time DSP data plane, with
48 kHz / 16-bit stereo audio carried over HDMI. The two meet at a 64-bit command
word that the firmware pushes into an asynchronous FIFO through a PCPI custom
instruction: the FPGA owns every per-sample calculation, and the soft core owns
every decision about what to play.

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
| 4a | 8-voice engine, ADSR, saturating mix | built, verified on hardware |
| 4b | Firmware voice allocation + sequencing | built, verified on hardware |
| 5 | Sample-accurate sequencer | closed by 4b |
| 6 | Custom LLVM toolchain | dropped by decision, see PLAN |

Phase 4 was planned with a filter; it was dropped in favour of band-limited
wavetables, which fix the aliasing at its source rather than after it — that work
is deferred and not scheduled against any phase. Phase 5's other two work items —
a monotonic sample counter and applying an event on its target sample — were
finished as a side effect of Phase 2, so absolute timestamps in 4b were all that
was left.

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

# Play a score, and hear what the HDMI sink received
uv run xsynth play tune.txt
uv run xsynth listen 5
```

The engine has eight voices and the firmware allocates them. A note names no
voice unless it is told to, and a note-off identifies its note by the pitch it
was started at, so the host never has to learn which voice it got:

```bash
uv run xsynth host envelope --attack 0.01 --decay 0.2 --sustain 0.5 --release 0.4
uv run xsynth host master 0.25
uv run xsynth host note-on --note 60 --wave saw
uv run xsynth host note-on --note 64
uv run xsynth host note-off --note 60
```

`--voice N` pins a voice instead of asking, and `freq`, `wave` and `amp` want
either that or `--all`, because on those "no voice" would have to mean one or
the other and guessing wrong silently changes a note you did not mean to touch.

For anything ahead of time, `status` prints the 48 kHz sample counter and `--at`
places a command at an absolute sample:

```bash
uv run xsynth host status                      # note the samples line
uv run xsynth host note-on --note 60 --at 1500000
uv run xsynth host note-off --note 60 --at 1524000
uv run xsynth host clear-schedule              # drop what is pending
```

`--delay N` measures from the previous command instead, and the two compose:
an anchor plus accumulating delays is how a whole melody goes out in one burst
without the host tracking which voice anything landed on. `anchor` takes the
current sample count and makes everything after it relative to that, for a host
that would rather not do the arithmetic.

A whole piece goes in a file, one note a line, and `play` streams it into the
firmware's schedule. The board keeps the time from there:

```bash
cat > tune.txt <<'EOF'
; the header is the client's own parameter names
wave saw
attack 0.005
decay 0.05
sustain 0.0
release 0.02
master 0.4

0.00  C4  0.20
0.25  E4  0.20
0.50  G4  0.20
0.75  C5  0.80
EOF
uv run xsynth play tune.txt
```

Times are seconds, so what is written is what a recording is measured against.
`;` starts a comment (`#` cannot: `A#3` is a note), and a pitch is a name
(`C4`, `A#3`, `Bb3`) or a MIDI number. There is no velocity column, and that is
not an oversight: `SET_AMP` addresses a voice, and with the firmware allocating
voices a score cannot know which one a note will land on. See
`xsynth/host/score.py`.

The firmware holds 256 scheduled events and drops what does not fit, silently,
so `play` streams into the ring rather than filling it once and walking away --
which is why that capacity lives in `xsynth/protocol.py`, where the host can
read it, instead of being a number only the firmware knows.

To hear what came out of the HDMI sink:

```bash
uv run xsynth listen 5                 # record five seconds, play it back
uv run xsynth listen 5 --output take.wav
```

It finds the capture card, unmutes it, records under `timeout`, and plays the
file back. The unmuting and the `timeout` are the point: `parecord -d` does
nothing, and the source ships muted, so a hand-rolled capture is a recipe with
traps in it.

The card also **loses the front of a capture** it was not already streaming
for -- about 1.2 seconds of it, silently, which is exactly the part of a
recording anyone was listening for. `listen` throws six seconds away before the
one that counts and prints `recording` when it actually starts, so nothing
played after that line is at risk. One second of warm-up is not enough; six is.
`--warmup 0` turns it off.

When in doubt, listen live instead:

```bash
ffplay -f pulse -i <source> -showmode 2      # spectrum view, no file in between
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
control UART, which on Linux are `/dev/ttyUSB0` and `/dev/ttyUSB1`. The JTAG one
announces itself as such, so the client skips it and finds the UART on its own;
pass `--port` to override.

To check what the HDMI sink actually received, record it and measure it:

```bash
uv run xsynth listen 5 --output tone.wav --no-play   # or parecord by hand
uv run xsynth analyse tone.wav
```

Phase 1 accepts `--tone <hz>` (default 440); phases 2 and 3 accept `--baud
<rate>` (default 115200). All accept `--pattern bars|cycle|<hex>` and
`--video-mode 640x480|1280x720`.

Build artifacts land in `build/`. `build/top.tim` is the nextpnr timing and
utilisation report. A phase 3 build takes roughly six minutes: the CPU doubles
the LUT count and nextpnr's placer is superlinear in density.

## From a blank board to music

Four steps. The first two put a design on the FPGA, the third puts a program in
the soft core, and the fourth plays something.

**1. Build and program the FPGA.** `--no-flash` writes the configuration to
SRAM, which is volatile and survives until the board loses power; without it the
bitstream goes to SPI flash and the board configures itself at power-up:

```bash
uv run xsynth build --phase 3 --no-flash    # SRAM: gone at power-off
uv run xsynth build --phase 3               # flash: boots itself
```

`--no-flash` is the right default while the design is changing, because a power
cycle is a cheaper reset than a flash erase. Both take about six minutes,
because the soft core roughly doubles the LUT count.

**2. Check that it is alive.** The debugger presents two USB serial ports and
the client picks the UART out of them; `status` should report `locked True` and
`version 4`:

```bash
uv run xsynth host status
```

**3. Upload the firmware.** This is a separate step, and the reason for it is
worth knowing: **the firmware is not in the bitstream.** The soft core's program
memory is BRAM, and BRAM comes up empty, so a freshly configured board has a CPU
with nothing to run. The image is built with clang and written into that memory
over the same UART everything else uses:

```bash
uv run xsynth host load            # builds the bundled firmware and runs it
uv run xsynth host load image.bin  # or a flat binary of your own
```

`cpu_stat` reading `0x5853594e` ("XSYN") means the firmware is the one talking.
**This has to be repeated after every power cycle**, whether or not the
bitstream came from flash -- flash holds the configuration, not the program.

**4. Play.** A score is a text file, one note to a line; `scores/` has a few and
says where they came from:

```bash
uv run xsynth play scores/twinkle.txt
```

Then listen to what the HDMI sink actually received, rather than trusting the
design:

```bash
uv run xsynth listen --list                 # the capture sources, with handles
uv run xsynth listen 30 --source <handle>   # record 30 s and play it back
```

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

`voice = 0xFF` means "firmware, you decide". On `NOTE_ON` that is an allocation;
on `NOTE_OFF` the `value` field carries the note's phase increment and the
firmware releases the oldest voice matching it; on the other three it means
*every* voice, since a note can be placed by its increment but a frequency
cannot — "which voice" and "the new value" would both want the value field.

Two opcodes are addressed to the firmware rather than the engine, and never
reach the command FIFO. `OP_SCHEDULE_AT` re-anchors the firmware's time
accumulator, so the commands after it accumulate their `delay` fields into
absolute sample times; that is how a host says "at sample 1500000" without a
second packet type, and how it reaches past the 16-bit delay's 1.365 seconds.
`OP_CLEAR_SCHEDULE` drops the events waiting to play and goes back to immediate —
the notes already sounding are left alone, and so is the allocator's knowledge
of them, because a later note-off still has to find its voice.

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
  host/            serial client, score player, capture-card recorder
  platform/        board definitions, Gowin primitives, toolchain patches
  sim/             simulation benches (Amaranth, and iverilog for the CPU)
  sv/              Xsynth-authored SystemVerilog glue
  sw/              RISC-V firmware: main.c is the wiring, control.{h,c} is
                   the allocator and the schedule and touches no register
  third_party/     vendored HDL (hdl-util/hdmi, YosysHQ/picorv32)
scores/            example scores, and where they came from
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
