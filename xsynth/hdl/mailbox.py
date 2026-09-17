"""A byte path from validated UART frames to the soft core.

The frame decoder reassembles and CRC-checks a whole payload before it replays
a single byte. In phase 2 those bytes went straight into the command FIFO. In
phase 3 they go to the CPU instead: the core owns voice allocation and
sequencing, and reaches the engine through the co-processor.

The CPU reads 16-bit words. A frame is announced by a header word::

    bit 15      header marker
    bits 13:8   how many payload bytes follow (the length, less the type)
    bits 7:0    the packet type

and the payload body follows, one byte per word, with bit 15 clear. Carrying
the type in the header rather than as its own byte keeps the firmware from
having to guess where a frame starts, and keeps the two from disagreeing about
whether the type byte counts.

Only one packet type is forwarded; everything else is the hardware handler's
business. A frame that does not fit is dropped whole, and ``overflow`` goes
sticky so the firmware can tell that its byte stream lost a frame rather than
silently mis-slicing the next one.
"""

from __future__ import annotations

from amaranth import Elaboratable, Module, Mux, Signal

from xsynth.hdl.fifo import AsyncFifo
from xsynth.protocol import MAX_PAYLOAD

HEADER_BIT = 15
BODY_SHIFT = 8
MAX_BODY = MAX_PAYLOAD - 1


class FrameMailbox(Elaboratable):
    """Forward one packet type from the frame decoder to the soft core."""

    def __init__(self, *, forward_type: int, depth: int = 64,
                 max_payload: int = MAX_PAYLOAD, domain: str = "sync"):
        self.forward_type = forward_type
        self.depth = depth
        self.domain = domain

        self.rx_byte = Signal(8)
        self.rx_stb = Signal()
        self.rx_index = Signal(range(max_payload))
        self.rx_length = Signal(range(max_payload + 1))

        self.r_data = Signal(16)
        self.r_inc = Signal()
        self.r_empty = Signal()
        self.flush = Signal()

        self.overflow = Signal()
        self.frames = Signal(16)
        self.pushed = Signal(16)
        self.popped = Signal(16)

    def elaborate(self, platform):
        m = Module()

        fifo = AsyncFifo(width=16, depth=self.depth,
                         w_domain=self.domain, r_domain=self.domain)
        m.submodules.fifo = fifo

        # The type byte is index 0, so it is the one byte whose push condition
        # cannot be latched: it decides the latch.
        header = Signal()
        forwarding = Signal()
        m.d.comb += header.eq(self.rx_stb & (self.rx_index == 0)
                              & (self.rx_byte == self.forward_type))

        push = Signal()
        m.d.comb += push.eq(header | (self.rx_stb & forwarding
                                      & (self.rx_index > 0)))

        # The header replaces the type byte rather than preceding it, so the
        # body length is one less than the payload length.
        body = Signal(range(MAX_PAYLOAD))
        m.d.comb += body.eq(Mux(self.rx_length > 0, self.rx_length - 1, 0))

        m.d.comb += [
            fifo.w_rst.eq(self.flush),
            fifo.r_rst.eq(self.flush),
            fifo.w_data.eq(
                Mux(header,
                    (1 << HEADER_BIT) | (body << BODY_SHIFT) | self.rx_byte,
                    self.rx_byte)
            ),
            fifo.w_inc.eq(push & ~fifo.w_full),
            self.r_data.eq(fifo.r_data),
            self.r_empty.eq(fifo.r_empty),
            fifo.r_inc.eq(self.r_inc),
        ]

        m.d.sync += [
            forwarding.eq(Mux(self.flush, 0,
                              Mux(self.rx_stb & (self.rx_index == 0),
                                  self.rx_byte == self.forward_type,
                                  forwarding))),
        ]

        with m.If(push):
            with m.If(fifo.w_full):
                m.d.sync += self.overflow.eq(1)
            with m.Elif(header):
                m.d.sync += self.frames.eq(self.frames + 1)
        with m.If(fifo.w_inc):
            m.d.sync += self.pushed.eq(self.pushed + 1)
        with m.If(self.r_inc & ~fifo.r_empty):
            m.d.sync += self.popped.eq(self.popped + 1)
        with m.If(self.flush):
            m.d.sync += self.overflow.eq(0)

        return m
