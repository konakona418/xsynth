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

from xsynth.hdl.audio import DEFAULT_TONE_HZ, phase_step
from xsynth.protocol import (
    FLAG_BAD_COMMAND,
    FLAG_CRC_ERROR,
    FLAG_LENGTH_ERROR,
    FLAG_LOCKED,
    FLAG_OVERFLOW,
    FLAG_UNKNOWN_PACKET,
    OP_NOTE_OFF,
    OP_NOTE_ON,
    OP_RESET,
    OP_SET_AMP,
    OP_SET_FREQ,
    OP_SET_WAVE,
    PKT_ERROR,
    PKT_PING,
    PKT_PONG,
    PKT_STATUS,
    PKT_STATUS_REPLY,
    Command,
    FrameDecoder,
    decode_response,
    encode_frame,
)

SAMPLE_RATE = 48_000
DEFAULT_BAUD = 115_200
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
    samples: int

    @classmethod
    def parse(cls, payload: bytes) -> Status:
        packet, arguments = decode_response(payload)
        if packet != PKT_STATUS_REPLY:
            raise ValueError(f"expected a status reply, got packet {packet:#04x}")
        if len(arguments) != 8:
            raise ValueError(
                f"a status reply carries 8 arguments, got {len(arguments)}"
            )
        return cls(
            version=arguments[0],
            flags=arguments[1],
            fifo_level=arguments[2],
            samples=int.from_bytes(arguments[4:8], "little"),
        )

    @property
    def locked(self) -> bool:
        return bool(self.flags & (1 << FLAG_LOCKED))

    @property
    def errors(self) -> list[str]:
        return [name for bit, name in _FLAG_NAMES if self.flags & (1 << bit)]


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

    def send(self, payload: bytes) -> None:
        self.serial.write(encode_frame(payload))
        self.serial.flush()

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

    def note_on(self, hz: float = DEFAULT_TONE_HZ, *, voice: int = 0,
                wave: str | None = None, delay: int = 0) -> None:
        commands = []
        if wave is not None:
            commands.append(self.wave_command(wave, voice=voice, delay=0))
        commands.append(Command(
            OP_NOTE_ON, voice=voice, value=self.step_for(hz), delay=delay,
        ))
        self.send_commands(commands)

    def note_off(self, *, voice: int = 0, delay: int = 0) -> None:
        self.send_commands([Command(OP_NOTE_OFF, voice=voice, delay=delay)])

    def set_freq(self, hz: float, *, voice: int = 0, delay: int = 0) -> None:
        self.send_commands([
            Command(OP_SET_FREQ, voice=voice, value=self.step_for(hz),
                    delay=delay),
        ])

    def set_amp(self, fraction: float, *, voice: int = 0,
                delay: int = 0) -> None:
        if not 0.0 <= fraction <= 1.0:
            raise ValueError("amplitude must be between 0 and 1")
        scale = round(fraction * ((1 << 15) - 1))
        self.send_commands([
            Command(OP_SET_AMP, voice=voice, value=scale, delay=delay),
        ])

    def set_wave(self, wave: str, *, voice: int = 0, delay: int = 0) -> None:
        self.send_commands([self.wave_command(wave, voice=voice, delay=delay)])

    def reset(self, *, delay: int = 0) -> None:
        self.send_commands([Command(OP_RESET, delay=delay)])

    @staticmethod
    def step_for(hz: float) -> int:
        return phase_step(hz, SAMPLE_RATE)

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
