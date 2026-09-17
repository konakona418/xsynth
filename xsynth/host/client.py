"""Serial client for the Phase 2 command protocol.

Typical use::

    from xsynth.host import XsynthClient

    with XsynthClient("/dev/ttyUSB0") as client:
        client.ping()
        client.note_on(440.0, wave="saw")

The client is deliberately thin: it frames commands, parses responses and turns
musical parameters into the engine's units (a frequency becomes a 32-bit phase
increment, an amplitude becomes a 16-bit sample scale).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import serial
from serial.tools import list_ports

from xsynth.hdl.audio import DEFAULT_TONE_HZ, PEAK, phase_step
from xsynth.hdl.voice import ENV_BITS, ENV_SHIFT
from xsynth.protocol import (
    CPU_HALTED,
    CPU_RUNNING,
    CPU_TRAP,
    FLAG_BAD_COMMAND,
    FLAG_CRC_ERROR,
    FLAG_LENGTH_ERROR,
    FLAG_LOCKED,
    FLAG_OVERFLOW,
    FLAG_UNKNOWN_PACKET,
    OP_CLEAR_SCHEDULE,
    OP_NOTE_OFF,
    OP_NOTE_ON,
    OP_RESET,
    OP_SCHEDULE_AT,
    OP_SET_AMP,
    OP_SET_ATTACK,
    OP_SET_DECAY,
    OP_SET_FREQ,
    OP_SET_MASTER,
    OP_SET_RELEASE,
    OP_SET_SUSTAIN,
    OP_SET_WAVE,
    PKT_ERROR,
    PKT_PING,
    PKT_PONG,
    PKT_STATUS,
    PKT_STATUS_REPLY,
    STATUS_ARGUMENTS,
    STATUS_CPU_COUNTER,
    STATUS_CPU_FLAGS,
    STATUS_CPU_STATUS,
    STATUS_SAMPLES,
    VOICE_ANY,
    Command,
    FrameDecoder,
    decode_response,
    encode_frame,
    encode_run,
    load_packets,
)

SAMPLE_RATE = 48_000
DEFAULT_BAUD = 115_200

# The envelope's full swing, and the largest increment its 24-bit accumulator
# takes: anything at or above that crosses it in a single sample.
ENV_TOP = PEAK << ENV_SHIFT
ENV_MAX = (1 << ENV_BITS) - 1
DEFAULT_TIMEOUT = 1.0

_FLAG_NAMES = (
    (FLAG_CRC_ERROR, "crc_error"),
    (FLAG_LENGTH_ERROR, "length_error"),
    (FLAG_OVERFLOW, "fifo_overflow"),
    (FLAG_BAD_COMMAND, "bad_command"),
    (FLAG_UNKNOWN_PACKET, "unknown_packet"),
)


def find_port() -> str:
    """Pick the one USB serial port, or explain why we cannot."""
    ports = list(list_ports.comports())
    usb = [port for port in ports if port.vid is not None]
    if len(usb) == 1:
        return usb[0].device
    if len(ports) == 1:
        return ports[0].device
    if not ports:
        raise SystemExit("no serial ports found; pass --port explicitly")
    listing = ", ".join(port.device for port in ports)
    raise SystemExit(
        f"several serial ports ({listing}); pass --port to choose one"
    )


@dataclass(frozen=True)
class Status:
    """A decoded ``PKT_STATUS_REPLY`` payload."""

    version: int
    flags: int
    fifo_level: int
    cpu_flags: int
    samples: int
    cpu_status: int
    cpu_counter: int

    @classmethod
    def parse(cls, payload: bytes) -> Status:
        packet, arguments = decode_response(payload)
        if packet != PKT_STATUS_REPLY:
            raise ValueError(f"expected a status reply, got packet {packet:#04x}")
        if len(arguments) != STATUS_ARGUMENTS:
            raise ValueError(
                f"a status reply carries {STATUS_ARGUMENTS} arguments, got "
                f"{len(arguments)}"
            )

        def word(offset: int) -> int:
            return int.from_bytes(arguments[offset:offset + 4], "little")

        return cls(
            version=arguments[0],
            flags=arguments[1],
            fifo_level=arguments[2],
            cpu_flags=arguments[STATUS_CPU_FLAGS],
            samples=word(STATUS_SAMPLES),
            cpu_status=word(STATUS_CPU_STATUS),
            cpu_counter=word(STATUS_CPU_COUNTER),
        )

    @property
    def locked(self) -> bool:
        return bool(self.flags & (1 << FLAG_LOCKED))

    @property
    def errors(self) -> list[str]:
        return [name for bit, name in _FLAG_NAMES if self.flags & (1 << bit)]

    @property
    def running(self) -> bool:
        return bool(self.cpu_flags & (1 << CPU_RUNNING))

    @property
    def halted(self) -> bool:
        return bool(self.cpu_flags & (1 << CPU_HALTED))

    @property
    def trapped(self) -> bool:
        return bool(self.cpu_flags & (1 << CPU_TRAP))


class XsynthClient:
    """A framed command channel to a programmed board."""

    def __init__(self, port: str | None = None, *, baud: int = DEFAULT_BAUD,
                 timeout: float = DEFAULT_TIMEOUT, transport=None):
        if transport is not None:
            # An already-open serial-like object, for tests and for callers that
            # manage their own connection.
            self.serial = transport
            self.port = getattr(transport, "name", "<transport>")
        else:
            self.port = port or find_port()
            self.serial = serial.Serial(self.port, baud, timeout=0.02)
        self.baud = baud
        self.timeout = timeout

    def close(self) -> None:
        self.serial.close()

    def __enter__(self) -> XsynthClient:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def flush_input(self) -> None:
        self.serial.reset_input_buffer()

    def send_frame(self, frame: bytes) -> None:
        """Send an already-framed packet."""
        self.serial.write(frame)
        self.serial.flush()

    def send(self, payload: bytes) -> None:
        self.send_frame(encode_frame(payload))

    def send_commands(self, commands) -> None:
        payload = bytearray([0x01])
        for command in commands:
            payload += command.pack()
        self.send(bytes(payload))

    def read_frame(self, timeout: float | None = None) -> bytes | None:
        """Read until one frame passes its checksum, or the timeout expires."""
        deadline = time.monotonic() + (self.timeout if timeout is None
                                       else timeout)
        decoder = FrameDecoder()
        while time.monotonic() < deadline:
            for byte in self.serial.read(64):
                payload = decoder.feed(byte)
                if payload is not None:
                    return payload
        return None

    def request(self, payload: bytes, timeout: float | None = None) -> bytes:
        self.flush_input()
        self.send(payload)
        response = self.read_frame(timeout)
        if response is None:
            raise TimeoutError("the board did not answer")
        return response

    def ping(self, timeout: float | None = None) -> bool:
        response = self.request(bytes([PKT_PING]), timeout)
        return decode_response(response)[0] == PKT_PONG

    def status(self, timeout: float | None = None) -> Status:
        response = self.request(bytes([PKT_STATUS]), timeout)
        packet, arguments = decode_response(response)
        if packet == PKT_ERROR:
            code = arguments[0] if arguments else 0
            raise RuntimeError(f"the board reported error {code:#04x}")
        return Status.parse(response)

    def note_on(self, hz: float = DEFAULT_TONE_HZ, *, voice: int = VOICE_ANY,
                wave: str | None = None, delay: int = 0) -> None:
        """Start a note.

        Naming no voice asks the firmware to pick one. A note is identified by
        the phase increment it was started with, so `note_off` wants the same
        `hz` back rather than a voice number.

        `wave` names no voice either, and since a waveform is not what makes a
        note a note, it goes to every voice. That is the useful reading of
        "play this note with a saw": the patch changes, then the note starts.
        """
        commands = []
        if wave is not None:
            commands.append(self.wave_command(wave, voice=voice, delay=0))
        commands.append(Command(
            OP_NOTE_ON, voice=voice, value=self.step_for(hz), delay=delay,
        ))
        self.send_commands(commands)

    def note_off(self, hz: float | None = None, *, voice: int = VOICE_ANY,
                 delay: int = 0) -> None:
        """Stop a note.

        With no voice, `hz` identifies which note: the firmware releases the
        voice whose step matches, oldest first, so a host never has to learn
        which voice it was given. Naming a voice stops that voice and ignores
        `hz`.
        """
        if voice == VOICE_ANY and hz is None:
            raise ValueError(
                "a note-off with no voice needs the hz the note was started at"
            )
        value = self.step_for(hz) if hz is not None else 0
        self.send_commands([
            Command(OP_NOTE_OFF, voice=voice, value=value, delay=delay),
        ])

    def set_freq(self, hz: float, *, voice: int = VOICE_ANY,
                 delay: int = 0) -> None:
        """Change a voice's pitch. Naming no voice changes every one, since
        "which voice" and "the new frequency" would both want the value
        field."""
        self.send_commands([
            Command(OP_SET_FREQ, voice=voice, value=self.step_for(hz),
                    delay=delay),
        ])

    def set_amp(self, fraction: float, *, voice: int = VOICE_ANY,
                delay: int = 0) -> None:
        """Set the note's own level, which is how far its attack travels.
        Naming no voice sets every one."""
        self.send_commands([
            Command(OP_SET_AMP, voice=voice, value=self.level_for(fraction),
                    delay=delay),
        ])

    def set_wave(self, wave: str, *, voice: int = VOICE_ANY,
                 delay: int = 0) -> None:
        """Select a waveform. Naming no voice selects it for every one."""
        self.send_commands([self.wave_command(wave, voice=voice, delay=delay)])

    def set_envelope(self, *, attack: float | None = None,
                     decay: float | None = None,
                     sustain: float | None = None,
                     release: float | None = None,
                     delay: int = 0) -> None:
        """Set the shared ADSR, each stage in the units it is played in.

        ``attack``, ``decay`` and ``release`` are seconds for the stage to
        cross the whole envelope; ``sustain`` is a fraction of the note's own
        peak. Only the stages named are sent, so a patch can be retuned one
        knob at a time.
        """
        commands = []
        for opcode, seconds in (
            (OP_SET_ATTACK, attack), (OP_SET_DECAY, decay),
            (OP_SET_RELEASE, release),
        ):
            if seconds is not None:
                commands.append(Command(
                    opcode, value=self.envelope_rate(seconds), delay=delay))
        if sustain is not None:
            commands.append(Command(
                OP_SET_SUSTAIN, value=self.level_for(sustain), delay=delay))
        self.send_commands(commands)

    def set_master(self, fraction: float, *, delay: int = 0) -> None:
        """Scale the whole mix. Eight voices at unity will clip; this is how a
        chord is kept inside the rails."""
        self.send_commands([
            Command(OP_SET_MASTER, value=self.level_for(fraction), delay=delay),
        ])

    def reset(self, *, delay: int = 0) -> None:
        """Silence everything and clear the firmware's own state: the voices,
        the allocator's shadow, the pending schedule and the error flags."""
        self.send_commands([Command(OP_RESET, delay=delay)])

    def now(self, timeout: float | None = None) -> int:
        """The engine's 48 kHz sample count, which is what a schedule's times
        are measured in. The counter is free running from power-up, so a host
        reads it once and works in absolutes from then on."""
        return self.status(timeout).samples

    def anchor(self, sample: int) -> None:
        """Every command sent after this takes effect at `sample` plus the
        delays accumulated since the anchor.

        A single `delay` is 16 bits, so it reaches 1.365 seconds; a longer
        silence is another anchor rather than an unrepresentable number. The
        anchor stays in force until `clear_schedule` or `reset`, so a host that
        sends nothing scheduled for a while should clear it rather than let a
        live note land where the last planned one did.
        """
        self.send_commands([Command(OP_SCHEDULE_AT, value=sample)])

    def clear_schedule(self) -> None:
        """Drop the events waiting to be played and go back to immediate. The
        notes already sounding are left alone, and so is the allocator's
        knowledge of them -- a later note-off still has to find its voice."""
        self.send_commands([Command(OP_CLEAR_SCHEDULE)])

    def load(self, image: bytes, *, base: int = 0) -> int:
        """Write a firmware image into program memory.

        Returns the number of bytes written. The CPU must be stopped, or at
        least not depending on the words being replaced: the loader wins the
        memory port, but a running program will still fetch whatever is there.
        """
        for frame in load_packets(image, base=base):
            self.send_frame(frame)
        return len(image)

    def run(self, running: bool = True) -> None:
        """Let the CPU out of reset, or put it back in."""
        self.send_frame(encode_run(running))

    @staticmethod
    def step_for(hz: float) -> int:
        return phase_step(hz, SAMPLE_RATE)

    @staticmethod
    def level_for(fraction: float) -> int:
        """A 0.0-1.0 fraction as the 16-bit level the engine wants."""
        if not 0.0 <= fraction <= 1.0:
            raise ValueError("level must be between 0 and 1")
        return round(fraction * PEAK)

    @staticmethod
    def envelope_rate(seconds: float) -> int:
        """The per-sample increment that crosses the envelope in ``seconds``.

        The envelope is a 24-bit accumulator whose top bits are the level, so
        a stage that crosses it in ``seconds`` moves ``ENV_TOP`` over that many
        samples. Zero or less is instant, which is the engine's own default.
        """
        if seconds <= 0:
            return ENV_MAX
        return min(round(ENV_TOP / (seconds * SAMPLE_RATE)), ENV_MAX)

    @staticmethod
    def wave_command(wave: str, *, voice: int = 0, delay: int = 0) -> Command:
        from xsynth.hdl.voice import WAVES

        try:
            index = WAVES.index(wave)
        except ValueError:
            raise SystemExit(
                f"unknown waveform {wave!r}; known: {', '.join(WAVES)}"
            ) from None
        return Command(OP_SET_WAVE, voice=voice, value=index, delay=delay)
