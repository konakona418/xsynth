# Handoff / current state

_Last updated: Phase 4a built and simulated, not yet verified on hardware._

## Where we are

**Phases 0, 1, 2, 3a and 3b are all done and verified on hardware; Phase 4a is
built and covered by simulation.** The Tang Nano 9K sends full HDMI
(640x480@60 colour bars plus an eight-voice, 48 kHz wavetable engine), a host
drives that engine live over the USB UART, and a PicoRV32 soft core runs
programs the host uploads — including the one that owns the command path.

```
uv run xsynth sim   --phase 2                  # Amaranth simulation, no toolchain
uv run xsynth build --phase 3 --no-program     # synthesize + PnR + pack only
uv run xsynth build --phase 3 --no-flash       # build + program SRAM (volatile)
uv run xsynth host  status                     # talk to a programmed board
uv run xsynth host  load                       # build, upload and run the firmware
uv run pytest -q                               # the whole suite, across the cores
```

A phase 3 build takes about 6 minutes; see the phase 3a section for why.

**Builds are expensive, simulations are not, so lean on the tests.** The
toolchain is entirely YoWASP (WebAssembly) and nothing native is installed;
`yosys` exists in `extra` but `nextpnr-himbaechel-gowin` and `yosys-slang` are
in neither the repos nor the AUR, so going native means building nextpnr from
source, and yosys and nextpnr cannot be mixed across the two because they talk
RTLIL JSON. The standing decision is to stay on YoWASP and therefore to reach
for a build only when the simulation cannot answer the question.

The default build is 640x480, colour bars. Useful flags:
`--pattern bars|cycle|<hex>`, `--video-mode 640x480|1280x720`, `--tone <hz>`
(phase 1), `--baud <rate>` (phase 2), `--hdmi` (force full HDMI; phase 0 defaults
to DVI, phase 1+ to HDMI).

Host actions: `ping`, `status`, `reset`, `load [image] [--no-run]`,
`run [--stop]`, `note-on [--hz|--note] [--wave] [--voice]`,
`note-off [--voice]`, `freq <hz> [--voice]`, `wave <name> [--voice]`,
`amp <0..1> [--voice]`, `envelope [--attack|--decay|--sustain|--release]`,
`master <0..1>`.

### The control UART is /dev/ttyUSB1

The board's onboard debugger enumerates as a **Sipeed "JTAG Debugger"
(`0403:6010`, an FT2232 clone)** with two interfaces: interface 0 is JTAG (used
by `openFPGALoader`) and interface 1 is the UART wired to FPGA pins 17/18, which
lands on **`/dev/ttyUSB1`**. `/dev/ttyUSB0` is the JTAG channel.

```bash
uv run xsynth host --port /dev/ttyUSB1 status
uv run xsynth host --port /dev/ttyUSB1 note-on --hz 440 --wave saw
```

Verified on hardware:

* `ping` -> pong, `status` -> version 2, `locked True`, `errors none`, and the
  48 kHz sample counter advancing.
* `note-on`, `freq`, `wave`, `amp`, `note-off` all take effect live.
* Capture-card audio: a 440 Hz sine is a **single** peak with every harmonic at
  0.000 and a steady-state RMS of 23167 (the full-scale ideal); a 300 Hz square
  has 900/1500/2100 Hz at 0.349/0.183/0.151, i.e. 1/3, 1/5, 1/7 with the evens
  at zero.
* Video stays live throughout (successive captures differ; colour bars with the
  moving marker).

## The one thing that mattered

The HDMI pins must **not** carry an `IO_TYPE` attribute.

`gowin_pack` defaults an `ELVDS_*` buffer to `LVCMOS33D`
(`_default_iostd['ELVDS_TBUF']`), but any `IO_TYPE` in the CST overrides that.
The Tang Nano 9K board file ships `IO_TYPE="LVCMOS33"` on the `hdmi` resource,
so the IOB was silently configured as a plain single-ended output and the
emulated-LVDS differential mode was never enabled. Nothing else was wrong:
netlist, output-enable, clock routing and OSER10 placement all checked out.

`xsynth/platform/patcher.py::patch_tang_nano_9k_hdmi_resource` now **pops**
`IO_TYPE` and only sets `DRIVE=8` / `PULL_MODE=NONE`.

## Verified facts about the hardware/toolchain

* **PLL**: one `rPLL` gives 126 MHz (`IDIV_SEL=2, FBDIV_SEL=13, ODIV_SEL=8`,
  VCO 1008 MHz, PFD 9 MHz); a `CLKDIV /5` gives 25.2 MHz. 25.2 MHz is chosen
  because `25200000 / 525 = 48000` exactly, so the HDMI audio clock is an exact
  division of the pixel clock. The second PLL is free.
* **`clk_audio` is a fabric clock**, not a dedicated net: a pixel-domain
  register dividing by 525. nextpnr routes it and times it fine (322 MHz), and
  the known-good 9K reference does the same thing. It does need its own
  `create_clock` in the SDC or nextpnr reports a meaningless 12 MHz PASS.
* **BSRAM** is inferred from `amaranth.lib.memory.Memory` and `synth_gowin`
  imports the `$meminit` sine table correctly (4 `SP` blocks for 4096x16).
* **HDMI differential output**: Amaranth's Gowin platform emits true-LVDS
  `TLVDS_TBUF`, which gowin_pack rejects ("emulated lvds pin"). Xsynth overrides
  `GowinPlatform.get_io_buffer` (`xsynth/platform/gowin_io.py`) to emit
  `ELVDS_TBUF` and — importantly — to connect `o`/`oe`. Hand-instantiating
  `ELVDS_OBUF` instead leaves the output enable undriven.
* **HDMI core**: hdl-util/hdmi is compiled with Yosys' slang frontend
  (`read_slang`), not `read_verilog -sv`, because it uses unpacked-array ports.
  `-j 1` is mandatory (the threaded parser traps under WebAssembly).
  `+/gowin/cells_sim.v` and `+/gowin/cells_xtra_gw1n.v` are pre-loaded so
  `OSER10`/`ELVDS_*` resolve.
* **Vendored sources are patched at build time** (`xsynth/platform/hdmi_patch.py`),
  never edited: two blocking/non-blocking assignment fixes for slang, plus a
  no-op annotation on the TMDS clock. The patch asserts each edit matches once.
* **Clock constraints** are passed via `xsynth_timing.sdc` + nextpnr `--sdc`;
  Amaranth's Apicula flow does not forward them and nextpnr would otherwise use
  its 12 MHz default and print a meaningless "PASS".
* **CST form**: nextpnr requires both P and N pins constrained separately. The
  reference's single-port-two-pins form (`IO_LOC "sig" 71,70`) is rejected with
  "Unconstrained IO".
* Phase 0 resource use (DVI mode): ~334 LUT4, 258 ALU, 0 BSRAM, 3 OSER10,
  1 CLKDIV. `clk_pixel` closes timing at 25.2 MHz with lots of margin.

## Capture card (works — with a workflow caveat)

`/dev/videoN` (MACROSILICON "C1-1 USB3 Video", UVC) **does work** with the
Tang Nano 9K. Verified live: VLC shows the `cycle` pattern changing colour once
per second.

Caveat: after every FPGA reconfiguration the card loses lock and then **holds
the last good frame** (a stale image), which makes scripted captures lie. To get
a live stream again:

1. re-plug the HDMI at the **capture-card end** (the FPGA end is fine), and
2. (re)start the viewer.

**Then wait about six seconds before believing a capture.** After a re-plug the
card serves that same stale frame for the first several seconds and only then
starts delivering live ones — with no error and no change in the image
metadata. A capture that grabs a single frame, or three frames in a burst,
lands entirely inside that window and looks exactly like a dead link. Take one
frame per second for ~14 seconds and check that the last ones have different
md5s; a stale frame repeats one md5 forever, a live picture never repeats.

A re-plug also **renumbers the device node** — it has been `/dev/video2`,
`/dev/video3` and `/dev/video4` across sessions. Always re-check with
`v4l2-ctl --list-devices` rather than trusting the last known number.

A stuck capture (an old viewer holding the device, e.g. a VLC left over from a
previous session) produces the same stale-frame symptom. Check
`pgrep -a -f vlc` and kill by PID.

```bash
vlc v4l2:///dev/video2 :v4l2-width=640 :v4l2-height=480 :v4l2-fps=60 :v4l2-chroma=YUYV
```

For scripted screenshots:

```bash
ffmpeg -f v4l2 -input_format mjpeg -video_size 640x480 -framerate 60 \
       -i /dev/video2 -t 14 -vf fps=1 -frames:v 14 -y warm%02d.png
```

The last frames are the real ones; the colour bars should read white, yellow,
cyan, green, magenta, red, blue, black across the row.

### Capturing the HDMI audio

The same card exposes an HDMI audio input over USB:

```bash
SRC=alsa_input.usb-MACROSILICON_C1-1_USB3_Video_20210623-02.analog-stereo
pactl set-source-mute "$SRC" 0     # it ships MUTED; recordings are all-zero otherwise
pactl set-source-volume "$SRC" 100%
timeout 15 parecord --device="$SRC" --file-format=wav --rate=48000 \
        --channels=2 --format=s16le tone.wav
```

Then measure it rather than trusting your ears:

```bash
uv run xsynth analyse tone.wav          # peak, RMS, fundamental, partials
uv run xsynth analyse tone.wav --channel 1
```

`xsynth/host/analysis.py` reports the peak, RMS, crest factor, the fundamental
and the partials relative to it. A correct single tone is one peak with every
harmonic at 0.000 and an RMS of `peak / sqrt(2)`; a square wave shows 1/3, 1/5,
1/7 with the even harmonics at zero. This is the instrument every audio
acceptance gate has been judged with, so keep it working.

Because of the stale-frame behaviour, **always confirm liveness with a changing
pattern** (`--pattern cycle`) and treat the monitor as authoritative for
"is the FPGA output correct".

Do not use `pkill` on this machine — it hangs. Kill specific PIDs instead.

## Phase 1 — 48 kHz audio + 440 Hz test tone (done)

How it works:

* `XsynthClocks` divides the pixel clock by `mode.audio_divisor` (525 for VGA,
  so exactly 48 kHz) to make `clk_audio`, and pulses `audio_strobe` on the same
  pixel edge. Both `clk_audio` and the strobe come off the *same* counter, so
  the audio clock and the DDS cannot drift apart.
* `xsynth/hdl/audio.py::SineDDS` is a 32-bit phase accumulator advanced by
  `audio_strobe`, taking the top 12 bits as the address of a 4096x16 sine table
  in BSRAM. It runs in the `pixel` domain: **the whole design has one clock**.
* `xsynth/hdl/phase1.py` drives `audio_left`/`audio_right` from the DDS and adds
  LED3 (audio-sample heartbeat, ~0.7 Hz).

Verified on hardware through the capture card's USB audio: both channels, peak
32767, RMS 23051 (= 32767/sqrt(2)), a single 440 Hz peak, no harmonics.

Resource use: 502 LUT4, 388 ALU, **4 BSRAM** (the table), 3 OSER10, 1 CLKDIV,
1 rPLL. `clk_pixel` closes at 128.5 MHz (target 25.2 MHz); `clk_audio` is a
fabric clock and nextpnr times it at 322 MHz.

Two things that cost real time here:

* **HDMI L-PCM is signed.** hdl-util zero-extends the 16-bit input to 24 bits
  and left-justifies it, so the input must be the two's-complement bit pattern.
  Feeding offset-binary (unsigned, centred on `0x8000`) makes the sink see a
  huge DC offset plus a squarish waveform: the *fundamental* stays correct, but
  odd harmonics at 1/n swamp it, which reads exactly like a 3x sample-rate bug.
  `SineDDS` therefore stores signed values; `to_signed()` recovers them.
* The capture card's audio source is **muted by default** in PipeWire, so
  recordings come back as all zeros, not as noise.

## Phase 2 — UART control of the voice (done, verified on hardware)

Data path: `UART RX -> frame decoder -> command FIFO -> sample scheduler -> voice`,
with `handler -> FrameTx -> UART TX` for responses.

* **Wire format**: `AA 55 | LEN | PAYLOAD | CRC16-LE`, CRC-16/CCITT-FALSE over
  LEN and payload. The first payload byte is the packet type
  (`0x01` commands, `0x02` ping, `0x03` status). Resynchronisation is on the next
  `AA 55`; the decoder buffers the payload and only releases it once the CRC
  matches, so a corrupted frame can never reach the FIFO.
* **Command word (64-bit, the shared UART/FIFO/PCPI encoding)**:
  `opcode[63:56] | voice[55:48] | value[47:16] | delay[15:0]`. `delay` is in
  48 kHz samples *between* successive applications, so a host can send a whole
  sequence in one burst and the engine needs no absolute time base. 0 and 1 both
  mean "the next sample".
* **Async FIFO**: Gray-coded pointers, 64-bit wide, 16 deep, write side on the
  27 MHz control clock and read side on the 25.2 MHz pixel clock. The read port
  is combinational, so it is first-word-fall-through and the scheduler can
  consume a command on the exact `audio_strobe` edge it is due. Occupancy is
  computed on the write side from the synchronised read pointer, so the status
  byte needs no further crossing.
* **Voice**: four 2048-entry wavetables (sine/saw/square/triangle) concatenated
  into one 8192x16 memory, addressed as `wave * 2048 + phase[31:21]`, with a
  16-bit amplitude gate and a 32-bit accumulator. The tables are naive, so saw
  and square alias; PLAN.md puts real oscillators in Phase 4.
* **Errors** are sticky in the status flags byte (bit 0 CRC, 1 length, 2 FIFO
  overflow, 3 bad command, 4 unknown packet, 5 PLL lock) and are cleared only by
  `OP_RESET`. The board also answers a malformed frame with an error packet.
* `Phase2Core` is everything except clocks/pins/HDMI, which is what makes the
  whole path simulatable; `xsynth sim --phase 2` runs a ping, a status read and a
  note on/off through it.

Resource use: **2248 LUT4 (26%)**, 616 ALU, 1387 DFF (21%), **8 BSRAM (30%)**
(the wavetable bank), 18 `RAM16SDP4` (the FIFO and the frame buffer), 1
`MULT18X18` (the amplitude), 1 rPLL, 1 CLKDIV. Timing after place and route:
`clk` (27 MHz control) 86.96 MHz, `clk_pixel` 87.73 MHz, `clk_pixel_x5` and
`clk_audio` also pass. The 27 MHz control clock needed its own `create_clock`;
without it nextpnr reported it against the 12 MHz default.

## Phase 3a — PicoRV32 and program upload (done, verified on hardware)

The CPU is real and runs code uploaded over the wire:

```bash
uv run xsynth host --port /dev/ttyUSB1 load      # build, upload and run
uv run xsynth host --port /dev/ttyUSB1 status    # cpu running, cpu_stat 0x12345678
uv run xsynth host --port /dev/ttyUSB1 run --stop
```

What was added:

* `xsynth/third_party/picorv32/` — the vendored core, read by Yosys's ordinary
  `read_verilog` (plain Verilog, so no patch machinery is needed).
* `xsynth/hdl/soc.py` — `PicoRV32` (an Amaranth black box) and `SoC`: 8 KB of
  program/data BSRAM at address 0, memory-mapped registers at `0x1000_0000`.
  The loader and the CPU share one write port; the loader wins and stalls the
  CPU, so a mistimed load cannot silently corrupt a running program.
* `xsynth/hdl/loader.py` — `ProgramLoader`, which turns validated LOAD frames
  into memory writes and latches the run control.
* `xsynth/sw/` plus `xsynth/firmware.py` — startup, linker script and a minimal
  firmware, compiled by clang/`ld.lld` into a flat image. The C register header
  is generated from `xsynth.hdl.soc`, so the two cannot drift apart.
* Protocol: `PKT_LOAD` and `PKT_RUN`, and a 17-byte status reply carrying the
  CPU flags, the firmware's scratch register and the free-running counter.
  `VERSION` is now 3.
* `xsynth/sim/verilog.py` and `xsynth/sim/soc.py` — Amaranth cannot simulate a
  Verilog `Instance`, so designs containing one are emitted to Verilog and run
  under iverilog. `tests/test_soc.py` boots the real firmware on the real core.

Measured: **4504 LUT4 (52%), 2443 DFF (37%), 12/26 BSRAM**, and a full build
takes about **5m45s**. The CPU roughly doubles the LUT count and nextpnr's
placer is superlinear in density, so a phase 3 build is several times a phase 2
one — worth knowing before concluding that it has hung.

### Phase 3b — the soft core owns the command path (done, verified on hardware)

The host's `note-on` now goes *through the CPU* rather than straight into the
engine, and the acceptance gate is met: a full command FIFO stalls the core
instead of losing a command.

```bash
uv run xsynth host --port /dev/ttyUSB1 load              # upload the 3b firmware
uv run xsynth host --port /dev/ttyUSB1 status            # cpu_stat 0x5853594e ("XSYN")
uv run xsynth host --port /dev/ttyUSB1 note-on --hz 440 --wave saw
```

Measured on the capture card: 440.1 Hz with partials 1/n (0.516, 0.336, 0.256,
0.203, ...) — a sawtooth, so the whole chain ran.

What was added:

* `xsynth/hdl/pcpi.py` — `CommandCoProcessor`, the custom-0 instruction
  `xsynth.push {rs2, rs1}` that writes a 64-bit command into the engine FIFO.
  It asserts `pcpi_wait` while the FIFO is full and `pcpi_ready` otherwise, so
  the CPU stalls rather than dropping. `funct3 = 1` reads the FIFO level back.
* `xsynth/hdl/mailbox.py` — `FrameMailbox`, the byte path from the frame decoder
  to the CPU. One packet type is forwarded; each frame is announced by a header
  word (bit 15 set, type in bits 7:0, body length in bits 13:8) followed by the
  body, one byte per 16-bit word.
* `SoC` grew `REG_RX_DATA` (read pops, write flushes) and `REG_RX_STATUS`
  (`{frames, overflow, empty}`), plus `REG_COMMANDS`, and passes PCPI through.
* `xsynth/hdl/phase2.py` — `PacketHandler(forward_commands=...)`: in phase 3 the
  handler length-checks a COMMANDS frame and then leaves it alone, because only
  one thing may drive the FIFO.
* `xsynth/sw/main.c` — the firmware unpacks each frame and pushes every command
  through the co-processor. No overflow handling is needed: the stall is the
  backpressure.
* `xsynth/sim/phase3.py` and `tests/test_phase3.py` — the whole chain is
  simulated end to end under iverilog: UART in, voice amplitude out.
  `tests/test_pcpi.py` tests the stall directly on a deliberately tiny FIFO.

Two bugs worth remembering, both in the SoC's read path (see gotchas): a
register read must act on the `ready` cycle, not the address cycle, and a
strobe register needs an explicit default or it latches on.

Measured: **4833 LUT4 (55%), 2542 DFF (39%), 12/26 BSRAM** — the co-processor
and mailbox cost about 330 LUTs and 100 flip-flops over phase 3a, and the build
time is unchanged at roughly six minutes.

`xsynth sim --phase 3` runs the whole chain under iverilog: it boots the
firmware, plays a note through it, and stops it again.

### Phase 4a — eight voices, one table port (built, simulated, not on hardware)

The engine was one voice. A voice needs a wavetable lookup per sample and the
tables live in one BSRAM, whose ports are far too precious to replicate the
bank eight times over, so `VoiceBank` walks the voices instead: two pixel
clocks each, sixteen for the eight of them, three percent of the 525 a 48 kHz
sample leaves in the 25.2 MHz pixel domain. Every voice shares one read port,
one multiplier and one accumulator.

The awkward part is that a command arrives whenever it arrives, and the walk is
writing a voice's registers back for two cycles out of every sixteen. A command
is therefore parked and applied when the walk reaches the voice it names —
the only moment that voice is not being written back — which keeps the two
writers from racing without stalling either of them.

Design decisions worth keeping:

* **The envelope settings are global; the envelope state is per voice.** One
  set of rates, one level and one stage each. That is what a synth normally
  does, and it means a note keeps its own shape while the panel is retuned.
* **Velocity is the envelope's peak, not a gain on top of it.** A quiet note is
  quiet all the way through its decay. The decay's floor is the sustain level
  capped by the note's own peak, so a quiet note cannot swell to meet it.
* **The envelope runs finer than the sample it produces** — the level is the
  top sixteen bits of a 24-bit accumulator. That is what turns a 16-bit rate
  into a useful range of times, from about three minutes down to instant.
* **The mix saturates.** Eight full-scale voices sum well past the rail, and a
  loud chord is better than a wrapping one. `master` is how a host keeps a
  chord inside it.
* **The table address is combinational.** The BSRAM read port is registered, so
  registering the address too would push the data a cycle late; the walk is two
  clocks per voice precisely because the read is one of them.

New commands (all in `xsynth/protocol.py`): `OP_SET_ATTACK`, `OP_SET_DECAY`,
`OP_SET_SUSTAIN`, `OP_SET_RELEASE` (the last three in 24-bit units), and
`OP_SET_MASTER`. `OP_SET_AMP` now means the note's own level. The host turns
seconds into rates (`XsynthClient.envelope_rate`) and fractions into levels
(`level_from`), because that arithmetic belongs where floating point exists.

Simulation covers the whole path: `tests/test_voice.py` drives the bank
directly and checks each stage of the envelope against exact arithmetic,
`tests/test_phase2.py` sends a chord and a voice-addressed note-off over the
wire, and `xsynth sim --phase 2` prints a three-voice chord and an envelope.

### Next: Phase 4b — the firmware starts deciding

**The filter is dropped; do not build one.** PLAN.md records the reasoning. The
short version: a filter treats the symptom (our naive saw/square alias badly)
and band-limited wavetables treat the cause, and it is the weakest item left for
the co-design story — it is just more DSP in a slot whose reuse is already
proven, whereas the allocator and the sequencer show the *soft core making
decisions*, which is the thing this project exists to show. It was a checkbox
in a plan written before we knew where the difficulty was.

So Phase 4b is firmware work, in `xsynth/sw/main.c`:

* **A note-to-voice allocator.** A host should be able to send a note-on with no
  voice in mind and have the firmware pick one. That needs the firmware to track
  which voices are busy, which note each holds, and what to steal when all eight
  are (oldest, or the one furthest into its release). It needs a way to tell a
  host's note-on from a voice-addressed one — the `voice` field is free for
  that, and `0xFF` is the obvious "any".
* **Sample-accurate sequencing** on the command `delay` field. The hardware
  already honours `delay` exactly (that is Phase 2's `CommandScheduler`), so
  what is missing is the firmware's ability to *emit* a schedule: absolute
  timestamps in, relative delays out, without the FIFO overflowing.

Both are decisions, not plumbing, which is the point. `xsynth/sw/main.c`
currently forwards commands one to one and decides nothing.

The engine itself is done: eight voices, a shared ADSR, per-voice level, a
saturating mix and a master. `xsynth sim --phase 2` and `xsynth sim --phase 3`
both exercise it, and the host can already address any voice
(`note-on --voice N`).

## Gotchas learned the hard way

* **A register holding a request must not also have an unconditional default.**
  `d += pending.eq(0)` at the top of an `elaborate` looks like a harmless
  default and is not: it clears the flag on the very next cycle, so a request
  that is supposed to wait for something is gone before that something happens.
  It only showed up for requests aimed past the first slot, which is why the
  first two tests passed and the rest did not.
* **A Python list of signals cannot be indexed by a signal.** `self.step[slot]`
  raises `TypeError: list indices must be integers`. Reads go through
  `Array(...)[slot]`; writes need `with m.Switch(slot)` and a `Case` per index.
* **Do not register the address of a BSRAM read port.** Amaranth's synchronous
  read port already registers the data, so an address register delays it by a
  second cycle and the value read is the previous voice's. The address must be
  combinational and the consumer waits exactly one cycle.
* **A testbench constant is not a parameter.** `STROBE_PERIOD` was raised from
  8 to 32 to give the voice walk room, but the generated Verilog still had
  `reg [3:0] strobe_div`, so the strobe stayed at 16 cycles and the walk never
  finished. Derive the width from the constant.
* **Measure RMS, not peak, when the phase steps through the table faster than
  once per entry.** Where the samples land relative to the sine's crest is then
  an artefact of the frequency, and a peak assertion measures that instead of
  the gain.
* **Trim the leading silence before measuring anything from a scenario.** The
  frame has to cross the UART, so a fifth of a short capture is zeros and every
  average is diluted by them.
* **HDMI audio samples are signed.** hdl-util zero-extends and left-justifies
  them, so an offset-binary (unsigned) sample is read as a big DC offset plus a
  squarish wave. The fundamental is still right, so it looks like a sample-rate
  bug; it is not.
* The capture card's audio source ships **muted**; a recording of pure zeros
  means "muted", not "silent FPGA".
* The capture card showing a frozen/stale frame (and "no signal") sent us
  chasing a physical-link theory for a long time. The real bug was `IO_TYPE`.
  Confirm video bring-up on a real sink, and when using the card, confirm
  liveness with a changing pattern.
* **The onboard debugger is the UART.** `lsusb` shows `0403:6010 Sipeed JTAG
  Debugger` — it is an FT2232 clone, interface 0 JTAG and interface 1 UART, so
  the control port is `/dev/ttyUSB1`. An external FT2232 JTAG probe would not
  provide this; the Tang Nano 9K's own debugger does.
* **Do not check for devices with a bare `ls /dev/ttyUSB* /dev/ttyACM*` under
  zsh.** A glob that matches nothing aborts the whole command before `ls` runs,
  which is how a working `/dev/ttyUSB1` was reported as "no serial devices".
* `read_slang` needs `-j 1` or it traps.
* `IO_TYPE` on an `ELVDS` pin silently disables differential mode.
* nextpnr's `--sdc` is required or timing results are meaningless — including
  for the fabric-generated `clk_audio` and for the raw 27 MHz `clk`.
* **A strobe is not a level.** `audio_strobe` is high for exactly one pixel
  clock; a pixel-domain register that samples it advances *once*. Driving it as
  a several-cycle level from a different clock domain advances the register once
  per pixel edge it spans, which silently multiplies the sample rate. Test
  strobes must be one pixel cycle wide.
* **Latched vs pending.** The response type must be copied into a `sending`
  register when a response starts; reading it from the `pending` flag fails,
  because that flag is cleared in the same cycle the frame begins.
* `amaranth.lib.memory.Memory` requires `init=` (or `data=`), even for a FIFO
  where every location is written before it is read.
* Apicula's `brams_map.v` prints "Range select out of bounds on
  `PORT_A_WR_DATA`" for every inferred BSRAM. It is noise from the mapping
  template; a read-only (ROM) memory never drives the write port.
* Reference implementations found: `muzhiyun/Tang_9K_HDMI` (vendors the same
  hdl-util core for the 9K, and generates `clk_audio` with a fabric divider just
  like we do), `zf3/some-tang-nano-9k-examples` (`01.hdmi`, SVO core),
  `joachimdraeger/vic64-t9k`. All are built with the Gowin IDE; ours is the
  Apicula/nextpnr equivalent.
* **Amaranth's simulator cannot run a Verilog `Instance`.** Anything containing
  one (PicoRV32, the HDMI core) has to be emitted to Verilog and simulated
  externally — `xsynth/sim/verilog.py` drives iverilog. The exception is why
  `Phase2Core` exists: keeping the core free of black boxes lets pysim test it.
* `amaranth.back.verilog` needs **`amaranth-yosys`**, not `yowasp-yosys`: it
  looks for a `yosys` binary or the builtin package, ignoring the `YOSYS`
  environment variable that the platform build uses. Both are dependencies now.
* **A memory-mapped read has two cycles, and they are not interchangeable.**
  `address_phase` is the cycle the address appears; the CPU samples `mem_rdata`
  on the `ready` cycle after it. A register that *acts* on being read — popping
  a FIFO, clearing a flag — must act on `ready`. Acting on `address_phase` pops
  one entry early, so every read returns its neighbour, which looks like a
  framing bug and is not one.
* **A strobe register needs an explicit default.** `d += sig.eq(1)` inside a
  `with m.If(...)` with no matching `d += sig.eq(0)` makes `sig` a register that
  latches on forever. Put the default *before* the conditional block: in
  Amaranth, a later assignment in the same domain wins.
* In a Verilog testbench, `"a" + "b"` is **arithmetic on the string bits**, not
  concatenation — `$display` prints a huge integer instead of the message. Use
  one long string literal.
* The frame decoder ignores incoming bytes while it drains a validated frame
  (`DRAIN` lasts up to `MAX_PAYLOAD` cycles), so the UART byte period must be
  longer than that in control-clock cycles. At 115200 baud and 27 MHz a byte is
  234 cycles against a 32-cycle worst case, so there is margin; raising the baud
  would need this checked.
* An iverilog testbench's top module must be named exactly what
  `verilog.convert(name=...)` used, or iverilog reports the design as an unknown
  module type.
* The firmware's C register header is **generated** from `xsynth.hdl.soc` at
  build time. Editing a `#define` by hand would be silently overwritten.
* `ld.lld` relaxes `la` into `auipc`/`addi` pairs, which is why the disassembly
  of `_start` does not look like the assembly source.
* The CPU costs roughly 2300 LUTs and the program memory 4 BSRAM, which takes a
  phase 3 build from ~2 minutes to ~5m45s. nextpnr's placer is superlinear in
  density; a long build is not necessarily a hung one.
