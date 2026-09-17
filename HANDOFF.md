# Handoff / current state

_Last updated: Phase 2 verified on hardware._

## Where we are

**Phases 0, 1 and 2 are all done and verified on hardware.** The Tang Nano 9K
sends full HDMI (640x480@60 colour bars plus a 48 kHz wavetable voice) and a
host drives that voice live over the USB UART.

```
uv run xsynth sim   --phase 2                  # Amaranth simulation, no toolchain
uv run xsynth build --phase 2 --no-program     # synthesize + PnR + pack only
uv run xsynth build --phase 2 --no-flash       # build + program SRAM (volatile)
uv run xsynth host  status                     # talk to a programmed board
uv run pytest -q                               # 78 tests
```

The default build is 640x480, colour bars. Useful flags:
`--pattern bars|cycle|<hex>`, `--video-mode 640x480|1280x720`, `--tone <hz>`
(phase 1), `--baud <rate>` (phase 2), `--hdmi` (force full HDMI; phase 0 defaults
to DVI, phase 1+ to HDMI).

Host actions: `ping`, `status`, `reset`, `note-on [--hz|--note] [--wave]`,
`note-off`, `freq <hz>`, `wave <name>`, `amp <0..1>`.

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

`/dev/video3` (MACROSILICON "C1-1 USB3 Video", UVC) **does work** with the
Tang Nano 9K. Verified live: VLC shows the `cycle` pattern changing colour once
per second.

Caveat: after every FPGA reconfiguration the card loses lock and then **holds
the last good frame** (a stale image), which makes scripted captures lie. To get
a live stream again:

1. re-plug the HDMI at the **capture-card end** (the FPGA end is fine), and
2. (re)start the viewer.

```bash
vlc v4l2:///dev/video3 :v4l2-width=640 :v4l2-height=480 :v4l2-fps=60 :v4l2-chroma=YUYV
```

For scripted screenshots:

```bash
ffmpeg -f v4l2 -input_format mjpeg -video_size 640x480 -framerate 60 \
       -i /dev/video3 -t 3 -frames:v 1 -update 1 -y out.png
```

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

## Next: Phase 3 — PicoRV32 and program upload

1. Vendor PicoRV32, add program/data BRAM, UART and timer.
2. Firmware takes over the frame parsing the hardware decoder does today.
3. Extend the protocol with memory write, entry point, RUN/STOP/RESET.
4. PCPI custom opcode -> the same 64-bit command word.

## Gotchas learned the hard way

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
