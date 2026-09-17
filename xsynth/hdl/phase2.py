"""Phase 2: UART control of the wavetable voice.

Acceptance gate: the host can turn notes on and off and change
frequency and waveform in real time, malformed frames do not disturb later
frames, and the command FIFO survives full/empty and random-phase conditions.

Data path::

    UART RX -> frame decoder -> command FIFO -> sample scheduler -> voice
                    |                                  |
                    +-> responses <- status registers  +-> HDMI audio

The frame decoder validates the checksum before anything reaches the FIFO, and
the scheduler applies each command on a 48 kHz sample boundary, so a command can
never land in the middle of a sample.

:class:`Phase2Core` is everything except the clocks, the board pins and the HDMI
black box, so it can be simulated directly; :class:`Phase2` wraps it for the
board.
"""

from __future__ import annotations

from amaranth import Array, Cat, Const, Elaboratable, Module, Signal

from xsynth.hdl.audio import SAMPLE_BITS
from xsynth.hdl.clock import ClockDomains, PowerOnReset, XsynthClocks
from xsynth.hdl.fifo import AsyncFifo
from xsynth.hdl.framing import FrameDecoder, FrameTx
from xsynth.hdl.hdmi import HDMIOutput
from xsynth.hdl.loader import ProgramLoader
from xsynth.hdl.mailbox import FrameMailbox
from xsynth.hdl.phase0 import StatusLeds
from xsynth.hdl.phase1 import AudioLeds
from xsynth.hdl.pcpi import CommandCoProcessor
from xsynth.hdl.uart import UartRx, UartTx, uart_timing
from xsynth.hdl.video import make_pattern
from xsynth.hdl.video_modes import DEFAULT_MODE, VideoMode
from xsynth.hdl.voice import CommandScheduler, VoiceBank
from xsynth.protocol import (
    ERR_BAD_COMMAND,
    ERR_BAD_LENGTH,
    ERR_FIFO_OVERFLOW,
    ERR_UNKNOWN_PACKET,
    OP_RESET,
    PKT_COMMANDS,
    PKT_ERROR,
    PKT_LOAD,
    PKT_PING,
    PKT_PONG,
    PKT_RUN,
    PKT_STATUS,
    PKT_STATUS_REPLY,
    STATUS_ARGUMENTS,
    VERSION,
)

CONTROL_CLOCK_HZ = 27_000_000
DEFAULT_BAUD = 115_200
DEFAULT_FIFO_DEPTH = 16

STATUS_BYTES = STATUS_ARGUMENTS + 1
ERROR_BYTES = 3

PENDING_NONE = 0
PENDING_PONG = 1
PENDING_STATUS = 2
PENDING_ERROR = 3

STATE_IGNORE = 0
STATE_COMMANDS = 1


class SampleCounter(Elaboratable):
    """A free-running 48 kHz sample counter, Gray-coded across clock domains.

    The audio side counts samples; the control side needs the value for status
    reports. Only a Gray-coded copy crosses, so the control side can never read
    a value that was never held.
    """

    def __init__(self, *, bits: int = 32, source: str = "pixel",
                 sink: str = "sync"):
        self.bits = bits
        self.source = source
        self.sink = sink

        self.strobe = Signal()
        self.count = Signal(bits)
        self.synced = Signal(bits)

    def elaborate(self, platform):
        m = Module()

        gray = Signal(self.bits)
        m.d.comb += gray.eq(self.count ^ (self.count >> 1))
        m.d[self.source] += self.count.eq(self.count + self.strobe)

        meta = Signal(self.bits, init=0)
        captured = Signal(self.bits, init=0)
        d = m.d[self.sink]
        d += meta.eq(gray)
        d += captured.eq(meta)

        binary = captured
        for shift in (1, 2, 4, 8, 16):
            if shift < self.bits:
                binary = binary ^ (binary >> shift)
        m.d.comb += self.synced.eq(binary)

        return m


def _as_value(value):
    """Wrap a plain int so it can be sliced and concatenated like a signal.

    The status reply is assembled from whatever the caller supplies, and a
    caller with nothing to report naturally passes ``0``.
    """
    return Const(value) if isinstance(value, int) else value


class PacketHandler(Elaboratable):
    """Turn validated frames into FIFO writes and UART responses.

    Commands are packed from the little-endian command bytes into the 64-bit
    word the engine consumes; a command that arrives while the FIFO is full is
    dropped and reported rather than blocking the UART.

    With ``forward_commands`` the command payload is not consumed here at all:
    the frame is length-checked and then left to the soft core, which owns
    voice allocation and reaches the engine through the co-processor. Only one
    thing may drive the FIFO, so the two modes are exclusive.
    """

    def __init__(self, fifo, *, status_version: int = VERSION,
                 status_locked=0, status_fifo_level=0, status_samples=0,
                 status_cpu_flags=0, status_cpu_status=0,
                 status_cpu_counter=0, forward_commands: bool = False,
                 domain: str = "sync"):
        self.fifo = fifo
        self.domain = domain

        self.status_version = status_version
        self.status_locked = status_locked
        self.status_fifo_level = status_fifo_level
        self.status_samples = _as_value(status_samples)
        self.status_cpu_flags = _as_value(status_cpu_flags)
        self.status_cpu_status = _as_value(status_cpu_status)
        self.status_cpu_counter = _as_value(status_cpu_counter)
        self.forward_commands = forward_commands

        self.rx_byte = Signal(8)
        self.rx_stb = Signal()
        self.rx_index = Signal(8)
        self.rx_length = Signal(8)
        self.crc_error = Signal()
        self.length_error = Signal()

        self.tx_data = Signal(8)
        self.tx_stb = Signal()
        self.tx_ready = Signal()

        self.error_flags = Signal(8)

    def elaborate(self, platform):
        m = Module()
        d = m.d[self.domain]
        fifo = self.fifo

        tx = FrameTx()
        m.submodules.tx = tx
        m.d.comb += [
            self.tx_data.eq(tx.tx_data),
            self.tx_stb.eq(tx.tx_stb),
            tx.tx_ready.eq(self.tx_ready),
        ]

        crc_sticky = Signal()
        length_sticky = Signal()
        overflow_sticky = Signal()
        bad_command_sticky = Signal()
        unknown_sticky = Signal()

        m.d.comb += self.error_flags.eq(Cat(
            crc_sticky,
            length_sticky,
            overflow_sticky,
            bad_command_sticky,
            unknown_sticky,
            self.status_locked,
            Const(0),
            Const(0),
        ))

        status_bytes = Array([
            PKT_STATUS_REPLY,
            self.status_version,
            self.error_flags,
            self.status_fifo_level,
            self.status_cpu_flags,
            self.status_samples[0:8],
            self.status_samples[8:16],
            self.status_samples[16:24],
            self.status_samples[24:32],
            self.status_cpu_status[0:8],
            self.status_cpu_status[8:16],
            self.status_cpu_status[16:24],
            self.status_cpu_status[24:32],
            self.status_cpu_counter[0:8],
            self.status_cpu_counter[8:16],
            self.status_cpu_counter[16:24],
            self.status_cpu_counter[24:32],
        ])

        pending = Signal(2)
        sending = Signal(2)
        error_code = Signal(8)
        error_bytes = Array([PKT_ERROR, error_code, 0])
        frame_state = Signal(1)
        accum = Signal(64)

        # ``sending`` latches the response being transmitted: ``pending`` is
        # cleared as the frame starts, so it cannot select the payload.
        with m.If(sending == PENDING_STATUS):
            m.d.comb += tx.byte_in.eq(status_bytes[tx.index])
        with m.Elif(sending == PENDING_ERROR):
            m.d.comb += tx.byte_in.eq(error_bytes[tx.index])
        with m.Else():
            m.d.comb += tx.byte_in.eq(PKT_PONG)

        d += tx.start.eq(0)
        with m.If(~tx.busy & (pending != PENDING_NONE)):
            d += sending.eq(pending)
            with m.If(pending == PENDING_STATUS):
                d += tx.length.eq(STATUS_BYTES)
            with m.Elif(pending == PENDING_ERROR):
                d += tx.length.eq(ERROR_BYTES)
            with m.Else():
                d += tx.length.eq(1)
            d += tx.start.eq(1)
            d += pending.eq(PENDING_NONE)

        # Sticky error reporting; only OP_RESET clears it.
        d += crc_sticky.eq(crc_sticky | self.crc_error)
        d += length_sticky.eq(length_sticky | self.length_error)
        if not self.forward_commands:
            d += fifo.w_inc.eq(0)

        with m.If(self.rx_stb):
            with m.If(self.rx_index == 0):
                d += accum.eq(0)
                with m.Switch(self.rx_byte):
                    with m.Case(PKT_COMMANDS):
                        with m.If((self.rx_length >= 9)
                                  & ((self.rx_length - 1) % 8 == 0)):
                            d += frame_state.eq(STATE_COMMANDS)
                        with m.Else():
                            d += frame_state.eq(STATE_IGNORE)
                            d += bad_command_sticky.eq(1)
                            d += error_code.eq(ERR_BAD_COMMAND)
                            d += pending.eq(PENDING_ERROR)
                    with m.Case(PKT_PING):
                        with m.If(self.rx_length == 1):
                            d += frame_state.eq(STATE_IGNORE)
                            d += pending.eq(PENDING_PONG)
                        with m.Else():
                            d += frame_state.eq(STATE_IGNORE)
                            d += bad_command_sticky.eq(1)
                            d += error_code.eq(ERR_BAD_LENGTH)
                            d += pending.eq(PENDING_ERROR)
                    with m.Case(PKT_STATUS):
                        with m.If(self.rx_length == 1):
                            d += frame_state.eq(STATE_IGNORE)
                            d += pending.eq(PENDING_STATUS)
                        with m.Else():
                            d += frame_state.eq(STATE_IGNORE)
                            d += bad_command_sticky.eq(1)
                            d += error_code.eq(ERR_BAD_LENGTH)
                            d += pending.eq(PENDING_ERROR)
                    with m.Case(PKT_LOAD, PKT_RUN):
                        # The program loader consumes these; the handler only
                        # has to not call them unknown.
                        d += frame_state.eq(STATE_IGNORE)
                    with m.Default():
                        d += frame_state.eq(STATE_IGNORE)
                        d += unknown_sticky.eq(1)
                        d += error_code.eq(ERR_UNKNOWN_PACKET)
                        d += pending.eq(PENDING_ERROR)
            with m.Elif(frame_state == STATE_COMMANDS):
                d += accum.eq(Cat(accum[8:], self.rx_byte))
                with m.If((self.rx_index % 8) == 0):
                    if not self.forward_commands:
                        d += fifo.w_data.eq(Cat(accum[8:], self.rx_byte))
                        with m.If(fifo.w_full):
                            d += overflow_sticky.eq(1)
                            d += error_code.eq(ERR_FIFO_OVERFLOW)
                            d += pending.eq(PENDING_ERROR)
                        with m.Else():
                            d += fifo.w_inc.eq(1)
                    with m.If(self.rx_byte == OP_RESET):
                        d += crc_sticky.eq(0)
                        d += length_sticky.eq(0)
                        d += overflow_sticky.eq(0)
                        d += bad_command_sticky.eq(0)
                        d += unknown_sticky.eq(0)

        return m


class Phase2Core(Elaboratable):
    """The control and audio path, without clocks, pins or HDMI."""

    def __init__(self, *, baud: int = DEFAULT_BAUD,
                 fifo_depth: int = DEFAULT_FIFO_DEPTH,
                 control_clock_hz: int = CONTROL_CLOCK_HZ, soc=None):
        self.baud = baud
        self.fifo_depth = fifo_depth
        self.control_clock_hz = control_clock_hz
        # Phase 3 adds the soft core alongside the engine; with no SoC this is
        # exactly the phase 2 design.
        self.soc = soc

        self.rx = Signal(init=1)
        self.tx = Signal()
        self.audio_strobe = Signal()
        self.locked = Signal()

        self.sample = Signal(SAMPLE_BITS)
        self.error_flags = Signal(8)
        self.fifo_level = Signal(8)
        self.samples = Signal(32)

        # The engine is built here rather than in elaborate so that the
        # simulations and the phase 3 testbench can name its signals as
        # top-level ports before the design is elaborated.
        self.voice = VoiceBank()
        # Voice 0's envelope level: the note's amplitude, for the harnesses to
        # read without reaching into the voice array.
        self.amp = Signal(SAMPLE_BITS)

    def elaborate(self, platform):
        m = Module()

        timing = uart_timing(self.control_clock_hz, self.baud)
        m.submodules.uart_rx = uart_rx = UartRx(timing.divisor)
        m.submodules.uart_tx = uart_tx = UartTx(timing.divisor)
        m.d.comb += [
            uart_rx.rx.eq(self.rx),
            self.tx.eq(uart_tx.tx),
        ]

        m.submodules.decoder = decoder = FrameDecoder()
        m.d.comb += [
            decoder.rx_byte.eq(uart_rx.data),
            decoder.rx_stb.eq(uart_rx.stb),
        ]

        m.submodules.fifo = fifo = AsyncFifo(
            width=64, depth=self.fifo_depth,
            w_domain="sync", r_domain="pixel",
        )
        m.d.comb += [fifo.w_rst.eq(0), fifo.r_rst.eq(0)]

        m.submodules.counter = counter = SampleCounter()
        m.d.comb += counter.strobe.eq(self.audio_strobe)

        cpu_status = {}
        if self.soc is not None:
            # The status byte the host reads: running, halted, trapped.
            cpu_status = {
                "status_cpu_flags": Cat(
                    self.soc.run & ~self.soc.halted,
                    self.soc.halted,
                    self.soc.trap,
                ),
                "status_cpu_status": self.soc.status,
                "status_cpu_counter": self.soc.counter,
            }

        m.submodules.handler = handler = PacketHandler(
            fifo,
            status_version=VERSION,
            status_locked=self.locked,
            status_fifo_level=fifo.w_level,
            status_samples=counter.synced,
            forward_commands=self.soc is not None,
            **cpu_status,
        )
        m.d.comb += [
            handler.rx_byte.eq(decoder.out_byte),
            handler.rx_stb.eq(decoder.out_stb),
            handler.rx_index.eq(decoder.out_index),
            handler.rx_length.eq(decoder.out_length),
            handler.crc_error.eq(decoder.crc_error),
            handler.length_error.eq(decoder.length_error),
            uart_tx.data.eq(handler.tx_data),
            uart_tx.stb.eq(handler.tx_stb),
            handler.tx_ready.eq(~uart_tx.busy),
            self.error_flags.eq(handler.error_flags),
            self.fifo_level.eq(fifo.w_level),
            self.samples.eq(counter.synced),
        ]

        if self.soc is not None:
            m.submodules.soc = self.soc
            m.submodules.loader = loader = ProgramLoader(
                mem_words=self.soc.mem_words)
            m.d.comb += [
                loader.rx_byte.eq(decoder.out_byte),
                loader.rx_stb.eq(decoder.out_stb),
                loader.rx_index.eq(decoder.out_index),
                loader.rx_length.eq(decoder.out_length),
                self.soc.load_stb.eq(loader.stb),
                self.soc.load_addr.eq(loader.addr),
                self.soc.load_data.eq(loader.data),
                self.soc.run.eq(loader.run),
                self.soc.samples.eq(counter.synced),
            ]

            m.submodules.mailbox = mailbox = FrameMailbox(
                forward_type=PKT_COMMANDS)
            m.d.comb += [
                mailbox.rx_byte.eq(decoder.out_byte),
                mailbox.rx_stb.eq(decoder.out_stb),
                mailbox.rx_index.eq(decoder.out_index),
                mailbox.rx_length.eq(decoder.out_length),
                self.soc.mailbox_data.eq(mailbox.r_data),
                self.soc.mailbox_empty.eq(mailbox.r_empty),
                self.soc.mailbox_overflow.eq(mailbox.overflow),
                self.soc.mailbox_frames.eq(mailbox.frames),
                self.soc.mailbox_pushed.eq(mailbox.pushed),
                self.soc.mailbox_popped.eq(mailbox.popped),
                mailbox.r_inc.eq(self.soc.mailbox_pop),
                mailbox.flush.eq(self.soc.mailbox_flush),
            ]

            m.submodules.coprocessor = coprocessor = CommandCoProcessor(fifo)
            m.d.comb += [
                coprocessor.valid.eq(self.soc.pcpi_valid),
                coprocessor.insn.eq(self.soc.pcpi_insn),
                coprocessor.rs1.eq(self.soc.pcpi_rs1),
                coprocessor.rs2.eq(self.soc.pcpi_rs2),
                self.soc.pcpi_wr.eq(coprocessor.wr),
                self.soc.pcpi_rd.eq(coprocessor.rd),
                self.soc.pcpi_wait.eq(coprocessor.wait),
                self.soc.pcpi_ready.eq(coprocessor.ready),
            ]

        m.submodules.scheduler = scheduler = CommandScheduler(fifo)
        m.d.comb += scheduler.strobe.eq(self.audio_strobe)

        m.submodules.voice = voice = self.voice
        m.d.comb += [
            voice.strobe.eq(self.audio_strobe),
            voice.apply.eq(scheduler.apply),
            voice.command.eq(scheduler.command),
            self.sample.eq(voice.sample),
            self.amp.eq(voice.env[0][voice.env_shift:]),
        ]

        return m


class Phase2(Elaboratable):
    """Phase 2 on the board: clocks, pins, HDMI and the core."""

    def __init__(self, mode: VideoMode = DEFAULT_MODE, *, audio_bits: int = 16,
                 pattern: str = "bars", baud: int = DEFAULT_BAUD,
                 fifo_depth: int = DEFAULT_FIFO_DEPTH):
        if audio_bits != SAMPLE_BITS:
            raise ValueError(
                f"the voice produces {SAMPLE_BITS}-bit samples but the HDMI "
                f"core was configured for {audio_bits}-bit audio"
            )
        self.mode = mode
        self.audio_bits = audio_bits
        self.pattern = pattern
        self.baud = baud
        self.fifo_depth = fifo_depth

    def make_core(self):
        """The control and audio core; phase 3 overrides this to add the SoC."""
        return Phase2Core(baud=self.baud, fifo_depth=self.fifo_depth)

    def elaborate(self, platform):
        m = Module()
        mode = self.mode

        clocks = XsynthClocks(mode)
        m.submodules.clocks = clocks
        m.submodules.domains = ClockDomains(clocks)
        m.submodules.reset = reset = PowerOnReset(clocks.locked)

        uart_pins = platform.request("uart", 0, dir={"rx": "i", "tx": "o"})
        m.submodules.core = core = self.make_core()
        m.d.comb += [
            core.rx.eq(uart_pins.rx.i),
            uart_pins.tx.o.eq(core.tx),
            core.audio_strobe.eq(clocks.audio_strobe),
            core.locked.eq(clocks.locked),
        ]

        hdmi_pins = platform.request("hdmi", dir={"clk": "o", "d": "o"})
        m.submodules.hdmi = hdmi = HDMIOutput(mode, audio_bits=self.audio_bits)
        m.submodules.pattern = pattern = make_pattern(
            self.pattern,
            screen_width=mode.screen_width,
            screen_height=mode.screen_height,
        )
        m.d.comb += [
            hdmi.clk_pixel.eq(clocks.clk_pixel),
            hdmi.clk_pixel_x5.eq(clocks.clk_pixel_x5),
            hdmi.clk_audio.eq(clocks.clk_audio),
            hdmi.reset.eq(reset.reset),
            pattern.cx.eq(hdmi.cx),
            pattern.cy.eq(hdmi.cy),
            hdmi.rgb.eq(pattern.rgb),
            hdmi.audio_left.eq(core.sample),
            hdmi.audio_right.eq(core.sample),
            hdmi_pins.d.o.eq(hdmi.tmds),
            hdmi_pins.clk.o.eq(hdmi.tmds_clock),
        ]

        m.submodules.leds = StatusLeds(clocks)
        m.submodules.audio_leds = AudioLeds(clocks.audio_strobe)

        return m
