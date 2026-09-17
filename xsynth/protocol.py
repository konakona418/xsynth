"""The Xsynth control protocol, shared by the host tools and the FPGA.

Frames on the wire, most significant bit first, one UART byte at a time::

    +------+------+-----+---------------+--------+--------+
    | 0xAA | 0x55 | LEN | PAYLOAD[LEN]  | CRC_LO | CRC_HI |
    +------+------+-----+---------------+--------+--------+

``LEN`` and the payload are covered by CRC-16/CCITT-FALSE (:mod:`xsynth.hdl.crc`),
transmitted little endian. A frame carries one packet, whose first payload byte
is the packet type.

Commands are fixed 64-bit words so that the same encoding can be carried by the
UART, the command FIFO and, later, the PicoRV32 PCPI port::

    bits 63..56  opcode
    bits 55..48  voice
    bits 47..16  value
    bits 15..0   delay, in 48 kHz samples, relative to the previous command

The delay is relative rather than an absolute timestamp, so the engine needs no
time base: a host can send a whole sequence in one burst and the engine will
apply each command the requested number of samples after the one before it.
"""

from __future__ import annotations

from dataclasses import dataclass

from xsynth.hdl.crc import INIT, crc16

SOF0 = 0xAA
SOF1 = 0x55
MAX_PAYLOAD = 32
COMMAND_BYTES = 8

PKT_COMMANDS = 0x01
PKT_PING = 0x02
PKT_STATUS = 0x03

PKT_PONG = 0x81
PKT_STATUS_REPLY = 0x82
PKT_ERROR = 0x83

OP_NOTE_ON = 0x01
OP_NOTE_OFF = 0x02
OP_SET_FREQ = 0x03
OP_SET_WAVE = 0x04
OP_SET_AMP = 0x05
OP_RESET = 0x06

OP_NAMES = {
    OP_NOTE_ON: "note_on",
    OP_NOTE_OFF: "note_off",
    OP_SET_FREQ: "set_freq",
    OP_SET_WAVE: "set_wave",
    OP_SET_AMP: "set_amp",
    OP_RESET: "reset",
}

WAVES = ("sine", "saw", "square", "triangle")
WAVE_INDEX = {name: index for index, name in enumerate(WAVES)}

ERR_BAD_CRC = 1
ERR_BAD_LENGTH = 2
ERR_UNKNOWN_PACKET = 3
ERR_FIFO_OVERFLOW = 4
ERR_BAD_COMMAND = 5

ERR_NAMES = {
    ERR_BAD_CRC: "bad_crc",
    ERR_BAD_LENGTH: "bad_length",
    ERR_UNKNOWN_PACKET: "unknown_packet",
    ERR_FIFO_OVERFLOW: "fifo_overflow",
    ERR_BAD_COMMAND: "bad_command",
}

# Bit positions in the status flags byte. The first five are sticky and are
# cleared only by OP_RESET; LOCKED is live.
FLAG_CRC_ERROR = 0
FLAG_LENGTH_ERROR = 1
FLAG_OVERFLOW = 2
FLAG_BAD_COMMAND = 3
FLAG_UNKNOWN_PACKET = 4
FLAG_LOCKED = 5

VERSION = 2

VALUE_BITS = 32
DELAY_BITS = 16
VALUE_MASK = (1 << VALUE_BITS) - 1
DELAY_MASK = (1 << DELAY_BITS) - 1
MAX_DELAY = DELAY_MASK


@dataclass(frozen=True)
class Command:
    """One 64-bit engine command."""

    opcode: int
    voice: int = 0
    value: int = 0
    delay: int = 0

    def __post_init__(self):
        if not 0 <= self.opcode <= 0xFF:
            raise ValueError(f"opcode {self.opcode} does not fit in 8 bits")
        if not 0 <= self.voice <= 0xFF:
            raise ValueError(f"voice {self.voice} does not fit in 8 bits")
        if not 0 <= self.value <= VALUE_MASK:
            raise ValueError(f"value {self.value} does not fit in 32 bits")
        if not 0 <= self.delay <= DELAY_MASK:
            raise ValueError(f"delay {self.delay} does not fit in 16 bits")

    @property
    def word(self) -> int:
        return ((self.opcode << 56) | (self.voice << 48)
                | (self.value << DELAY_BITS) | self.delay)

    def pack(self) -> bytes:
        return self.word.to_bytes(COMMAND_BYTES, "little")

    @classmethod
    def unpack(cls, data: bytes) -> Command:
        if len(data) != COMMAND_BYTES:
            raise ValueError(
                f"a command is {COMMAND_BYTES} bytes, got {len(data)}"
            )
        word = int.from_bytes(data, "little")
        return cls(
            opcode=(word >> 56) & 0xFF,
            voice=(word >> 48) & 0xFF,
            value=(word >> DELAY_BITS) & VALUE_MASK,
            delay=word & DELAY_MASK,
        )


def encode_frame(payload: bytes | bytearray) -> bytes:
    """Wrap a payload in a frame, CRC included."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(
            f"payload of {len(payload)} bytes exceeds the {MAX_PAYLOAD} limit"
        )
    body = bytes([len(payload)]) + bytes(payload)
    checksum = crc16(body, INIT)
    return bytes([SOF0, SOF1]) + body + checksum.to_bytes(2, "little")


def encode_commands(commands) -> bytes:
    payload = bytes([PKT_COMMANDS])
    for command in commands:
        payload += command.pack()
    return encode_frame(payload)


def decode_response(payload: bytes) -> tuple[int, bytes]:
    """Split a response payload into ``(packet_type, arguments)``."""
    if not payload:
        raise ValueError("an empty payload carries no packet type")
    return payload[0], bytes(payload[1:])


class FrameDecoder:
    """Streaming decoder, the reference for :class:`xsynth.hdl.framing.FrameDecoder`.

    Feed bytes with :meth:`feed`; it returns a payload whenever a frame passes
    its checksum, and ``None`` otherwise. A malformed frame is discarded and
    decoding resumes at the next ``0xAA 0x55``.
    """

    def __init__(self, max_payload: int = MAX_PAYLOAD):
        self.max_payload = max_payload
        self.reset()

    def reset(self) -> None:
        self._state = "idle"
        self._body = bytearray()
        self._crc_lo = 0

    @property
    def busy(self) -> bool:
        return self._state != "idle"

    def feed(self, byte: int) -> bytes | None:
        byte &= 0xFF
        if self._state == "idle":
            if byte == SOF0:
                self._state = "sof1"
        elif self._state == "sof1":
            if byte == SOF1:
                self._state = "len"
            elif byte == SOF0:
                self._state = "sof1"
            else:
                self._state = "idle"
        elif self._state == "len":
            if byte > self.max_payload:
                # The frame has no trustworthy length, so the only way out is to
                # resynchronise on the next start-of-frame.
                self.reset()
            else:
                self._body = bytearray([byte])
                self._state = "payload" if byte else "crc_lo"
        elif self._state == "payload":
            self._body.append(byte)
            if len(self._body) == self._body[0] + 1:
                self._state = "crc_lo"
        elif self._state == "crc_lo":
            self._crc_lo = byte
            self._state = "crc_hi"
        else:  # crc_hi
            expected = crc16(bytes(self._body), INIT)
            got = self._crc_lo | (byte << 8)
            payload = bytes(self._body[1:])
            self.reset()
            if expected == got:
                return payload
        return None
