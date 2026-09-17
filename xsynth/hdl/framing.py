"""Frame decoding and encoding in the control domain.

The wire format is described in :mod:`xsynth.protocol`; these are the hardware
implementations. :class:`FrameDecoder` is the reference decoder's counterpart
and :class:`FrameTx` builds a frame around a payload the caller presents one
byte at a time.

The decoder validates the checksum *before* releasing anything, so a corrupted
frame can never reach the command FIFO. The payload is buffered in a small
memory while the bytes arrive and replayed from it once the CRC matches; the
replay runs at one byte per cycle, which is thousands of times faster than the
UART, so it never collides with the next frame.
"""

from __future__ import annotations

from amaranth import Cat, Elaboratable, Mux, Module, Signal, unsigned
from amaranth.lib.memory import Memory

from xsynth.hdl.crc import Crc16
from xsynth.protocol import MAX_PAYLOAD, SOF0, SOF1


class FrameDecoder(Elaboratable):
    """Turn a UART byte stream into validated payload bytes.

    ``rx_stb`` pulses for each received byte. Once a frame's checksum matches,
    ``out_stb`` pulses once per payload byte while ``out_index`` walks from 0 to
    ``out_length - 1``; index 0 is the packet type. Bad frames raise a one-cycle
    ``crc_error`` or ``length_error`` instead, and the decoder resynchronises on
    the next start-of-frame.

    The decoder does not stall: bytes that arrive while it is replaying a frame
    are dropped, which is safe because a replay takes a few hundred nanoseconds
    against 87 microseconds per byte on the wire.
    """

    def __init__(self, *, max_payload: int = MAX_PAYLOAD, domain: str = "sync"):
        if max_payload < 1:
            raise ValueError("max_payload must be positive")
        self.max_payload = max_payload
        self.domain = domain

        self.rx_byte = Signal(8)
        self.rx_stb = Signal()

        self.out_byte = Signal(8)
        self.out_stb = Signal()
        self.out_index = Signal(range(max_payload))
        self.out_length = Signal(range(max_payload + 1))

        self.busy = Signal()
        self.crc_error = Signal()
        self.length_error = Signal()

    def elaborate(self, platform):
        m = Module()
        d = m.d[self.domain]

        crc = Crc16(domain=self.domain)
        m.submodules.crc = crc

        buffer = Memory(
            shape=unsigned(8),
            depth=self.max_payload,
            init=[0] * self.max_payload,
        )
        m.submodules.buffer = buffer
        write_port = buffer.write_port(domain=self.domain)
        read_port = buffer.read_port(domain="comb")

        length = Signal(range(self.max_payload + 1))
        crc_lo = Signal(8)
        index = Signal(range(self.max_payload))

        d += self.crc_error.eq(0)
        d += self.length_error.eq(0)
        d += crc.start.eq(0)
        d += crc.stb.eq(0)

        with m.FSM(domain=self.domain) as fsm:
            with m.State("IDLE"):
                with m.If(self.rx_stb & (self.rx_byte == SOF0)):
                    m.next = "SOF1"
            with m.State("SOF1"):
                with m.If(self.rx_stb):
                    with m.If(self.rx_byte == SOF1):
                        d += crc.start.eq(1)
                        m.next = "LEN"
                    with m.Elif(self.rx_byte != SOF0):
                        m.next = "IDLE"
            with m.State("LEN"):
                with m.If(self.rx_stb):
                    with m.If(self.rx_byte > self.max_payload):
                        # With an impossible length the frame's end is unknown,
                        # so the only safe move is to resynchronise.
                        d += self.length_error.eq(1)
                        m.next = "IDLE"
                    with m.Else():
                        d += length.eq(self.rx_byte)
                        d += crc.byte.eq(self.rx_byte)
                        d += crc.stb.eq(1)
                        d += index.eq(0)
                        with m.If(self.rx_byte == 0):
                            m.next = "CRC_LO"
                        with m.Else():
                            m.next = "PAYLOAD"
            with m.State("PAYLOAD"):
                with m.If(self.rx_stb):
                    d += crc.byte.eq(self.rx_byte)
                    d += crc.stb.eq(1)
                    with m.If(index == length - 1):
                        m.next = "CRC_LO"
                    with m.Else():
                        d += index.eq(index + 1)
            with m.State("CRC_LO"):
                with m.If(self.rx_stb):
                    d += crc_lo.eq(self.rx_byte)
                    m.next = "CRC_HI"
            with m.State("CRC_HI"):
                with m.If(self.rx_stb):
                    with m.If(Cat(crc_lo, self.rx_byte) == crc.value):
                        d += self.out_length.eq(length)
                        d += index.eq(0)
                        m.next = "DRAIN"
                    with m.Else():
                        d += self.crc_error.eq(1)
                        m.next = "IDLE"
            with m.State("DRAIN"):
                with m.If(index == length - 1):
                    m.next = "IDLE"
                with m.Else():
                    d += index.eq(index + 1)

        m.d.comb += [
            self.busy.eq(~fsm.ongoing("IDLE")),
            self.out_stb.eq(fsm.ongoing("DRAIN")),
            self.out_index.eq(index),
            self.out_byte.eq(read_port.data),
            write_port.en.eq(fsm.ongoing("PAYLOAD") & self.rx_stb),
            write_port.addr.eq(index),
            write_port.data.eq(self.rx_byte),
            read_port.addr.eq(index),
        ]

        return m


class FrameTx(Elaboratable):
    """Build a frame around a payload the caller presents combinationally.

    Set ``length``, pulse ``start``, and drive ``byte_in`` for the byte the
    transmitter asks for on ``index``. ``tx_data``/``tx_stb`` drive a
    :class:`~xsynth.hdl.uart.UartTx`, whose ``busy`` inverted feeds
    ``tx_ready``. The checksum is accumulated as the payload is sent, so no
    payload storage is needed.
    """

    def __init__(self, *, max_payload: int = MAX_PAYLOAD, domain: str = "sync"):
        if max_payload < 1:
            raise ValueError("max_payload must be positive")
        self.max_payload = max_payload
        self.domain = domain

        self.length = Signal(range(max_payload + 1))
        self.start = Signal()
        self.index = Signal(range(max_payload))
        self.byte_in = Signal(8)

        self.tx_data = Signal(8)
        self.tx_stb = Signal()
        self.tx_ready = Signal()
        self.busy = Signal()

    def elaborate(self, platform):
        m = Module()
        d = m.d[self.domain]

        crc = Crc16(domain=self.domain)
        m.submodules.crc = crc

        with m.FSM(domain=self.domain) as fsm:
            with m.State("IDLE"):
                with m.If(self.start):
                    d += self.index.eq(0)
                    m.next = "SOF0"
            with m.State("SOF0"):
                with m.If(self.tx_ready):
                    m.next = "SOF1"
            with m.State("SOF1"):
                with m.If(self.tx_ready):
                    m.next = "LEN"
            with m.State("LEN"):
                with m.If(self.tx_ready):
                    with m.If(self.length == 0):
                        m.next = "CRC_LO"
                    with m.Else():
                        m.next = "PAYLOAD"
            with m.State("PAYLOAD"):
                with m.If(self.tx_ready):
                    with m.If(self.index == self.length - 1):
                        m.next = "CRC_LO"
                    with m.Else():
                        d += self.index.eq(self.index + 1)
            with m.State("CRC_LO"):
                with m.If(self.tx_ready):
                    m.next = "CRC_HI"
            with m.State("CRC_HI"):
                with m.If(self.tx_ready):
                    m.next = "IDLE"

        in_payload = fsm.ongoing("LEN") | fsm.ongoing("PAYLOAD")
        sending = (fsm.ongoing("SOF0") | fsm.ongoing("SOF1") | in_payload
                   | fsm.ongoing("CRC_LO") | fsm.ongoing("CRC_HI"))

        m.d.comb += [
            self.busy.eq(~fsm.ongoing("IDLE")),
            self.tx_stb.eq(sending),
            crc.start.eq(self.start),
            crc.byte.eq(Mux(fsm.ongoing("LEN"), self.length, self.byte_in)),
            crc.stb.eq(in_payload & self.tx_ready),
        ]

        with m.If(fsm.ongoing("SOF0")):
            m.d.comb += self.tx_data.eq(SOF0)
        with m.Elif(fsm.ongoing("SOF1")):
            m.d.comb += self.tx_data.eq(SOF1)
        with m.Elif(fsm.ongoing("LEN")):
            m.d.comb += self.tx_data.eq(self.length)
        with m.Elif(fsm.ongoing("PAYLOAD")):
            m.d.comb += self.tx_data.eq(self.byte_in)
        with m.Elif(fsm.ongoing("CRC_LO")):
            m.d.comb += self.tx_data.eq(crc.value[:8])
        with m.Elif(fsm.ongoing("CRC_HI")):
            m.d.comb += self.tx_data.eq(crc.value[8:])
        with m.Else():
            m.d.comb += self.tx_data.eq(0)

        return m
