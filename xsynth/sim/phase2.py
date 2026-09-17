"""Phase 2 simulation: drive the control core without a board.

The board build wraps :class:`~xsynth.hdl.phase2.Phase2Core` in clocks, pins and
the HDMI black box; this harness keeps just the core, with a UART transmitter
feeding its RX and a receiver watching its TX, so a whole command round trip can
be exercised in Python.

The audio strobe is generated here rather than by the pixel-clock divider, so
the engine's sample rate in simulation is ``PIXEL_HZ / STROBE_PERIOD`` rather
than 48 kHz. It is still a one-cycle pulse, exactly like the hardware's, which
is what matters for the scheduler's timing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from amaranth import Elaboratable, Module
from amaranth.sim import Simulator

from xsynth.hdl.audio import PEAK, phase_step, to_signed
from xsynth.hdl.phase2 import Phase2Core
from xsynth.hdl.uart import UartRx, UartTx, uart_timing
from xsynth.hdl.voice import WAVES
from xsynth.protocol import (
    FLAG_LOCKED,
    OP_NOTE_OFF,
    OP_NOTE_ON,
    OP_SET_WAVE,
    PKT_PING,
    PKT_PONG,
    PKT_STATUS,
    Command,
    FrameDecoder,
    decode_response,
    encode_commands,
    encode_frame,
)
from xsynth.sim.phase1 import measure_frequency

CONTROL_HZ = 27_000_000
PIXEL_HZ = 25_200_000
SAMPLE_RATE = 48_000
DEFAULT_BAUD = 1_000_000
STROBE_PERIOD = 8

TEST_SAMPLE_RATE = PIXEL_HZ // STROBE_PERIOD


class CoreHarness(Elaboratable):
    """A :class:`Phase2Core` with a UART on each side."""

    def __init__(self, baud: int = DEFAULT_BAUD):
        divisor = uart_timing(CONTROL_HZ, baud).divisor
        self.core = Phase2Core(baud=baud)
        self.tx = UartTx(divisor)
        self.rx = UartRx(divisor)

    def elaborate(self, platform):
        m = Module()
        m.submodules.core = self.core
        m.submodules.tx = self.tx
        m.submodules.rx = self.rx
        m.d.comb += [
            self.core.rx.eq(self.tx.tx),
            self.rx.rx.eq(self.core.tx),
        ]
        return m


@dataclass
class Result:
    responses: list[bytes] = field(default_factory=list)
    audio: list[int] = field(default_factory=list)
    step: int = 0
    wave: int = 0
    amp: int = 0
    fifo_level: int = 0
    error_flags: int = 0


def run_scenario(frames, *, baud: int = DEFAULT_BAUD, cycles: int = 15_000,
                 samples: bool = False, locked: bool = True,
                 vcd: str | None = None) -> Result:
    """Send ``frames`` into the core and report what came back."""
    harness = CoreHarness(baud)
    core = harness.core
    stream = b"".join(frames)

    result = Result()
    reference = FrameDecoder()
    finished = False

    async def control(ctx):
        nonlocal finished
        ctx.set(core.locked, locked)
        sent = 0
        for _ in range(cycles):
            if sent < len(stream) and not ctx.get(harness.tx.busy):
                ctx.set(harness.tx.data, stream[sent])
                ctx.set(harness.tx.stb, 1)
                sent += 1
            else:
                ctx.set(harness.tx.stb, 0)
            if ctx.get(harness.rx.stb):
                payload = reference.feed(ctx.get(harness.rx.data))
                if payload is not None:
                    result.responses.append(payload)
            await ctx.tick("sync")
        for name in ("step", "wave", "amp", "fifo_level", "error_flags"):
            setattr(result, name, ctx.get(getattr(core, name)))
        finished = True

    async def audio(ctx):
        while not finished:
            ctx.set(core.audio_strobe, 1)
            await ctx.tick("pixel")
            ctx.set(core.audio_strobe, 0)
            for _ in range(STROBE_PERIOD - 1):
                await ctx.tick("pixel")
            if samples:
                result.audio.append(to_signed(ctx.get(core.sample)))

    sim = Simulator(harness)
    sim.add_clock(1 / CONTROL_HZ, domain="sync")
    sim.add_clock(1 / PIXEL_HZ, domain="pixel")
    sim.add_testbench(control)
    sim.add_testbench(audio)
    if vcd:
        with sim.write_vcd(vcd):
            sim.run()
    else:
        sim.run()

    return result


def run(*, vcd: str | None = None) -> None:
    """The ``xsynth sim --phase 2`` entry point: a short, readable demo."""
    tone = 10_000.0
    step = phase_step(tone, TEST_SAMPLE_RATE)

    ping = run_scenario([encode_frame(bytes([PKT_PING]))], vcd=vcd)
    packet, _ = decode_response(ping.responses[0])
    assert packet == PKT_PONG, f"expected a pong, got {packet:#04x}"
    print("ping: pong")

    status = run_scenario([encode_frame(bytes([PKT_STATUS]))])
    packet, arguments = decode_response(status.responses[0])
    assert packet == 0x82
    print(f"status: version={arguments[0]} fifo={arguments[2]} "
          f"locked={bool(arguments[1] & (1 << FLAG_LOCKED))}")

    played = run_scenario(
        [encode_commands([
            Command(OP_SET_WAVE, value=WAVES.index("saw")),
            Command(OP_NOTE_ON, value=step),
        ])],
        cycles=25_000,
        samples=True,
    )
    assert played.amp == PEAK, "the note never turned on"
    measured = measure_frequency(played.audio, TEST_SAMPLE_RATE)
    print(f"note_on: wave={WAVES[played.wave]} amp={played.amp} "
          f"measured {measured:.1f} Hz (asked for {tone:.1f})")

    stopped = run_scenario(
        [encode_commands([
            Command(OP_NOTE_ON, value=step),
            Command(OP_NOTE_OFF, delay=8),
        ])],
    )
    assert stopped.amp == 0, "the note never turned off"
    print("note_off: silenced")
