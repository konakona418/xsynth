"""The phase 3 program loader: turn LOAD frames into memory writes.

The loader has to be hardware. It is what puts a program into memory in the
first place, so there is no CPU yet to do it, and afterwards it is the only
thing that may write memory while the CPU is halted.

The frame decoder hands over a validated payload one byte at a time along with
that byte's index, so the layout is purely positional::

    index 0      packet type
    index 1..4   target byte address, little endian
    index 5..6   word count, little endian
    index 7..    that many little-endian words

Nothing here checks the checksum: that has already been done, which is the whole
point of decoding frames in hardware.
"""

from __future__ import annotations

from amaranth import Cat, Elaboratable, Module, Signal

from xsynth.protocol import LOAD_HEADER, PKT_LOAD, PKT_RUN


class ProgramLoader(Elaboratable):
    """Write LOAD frames into program memory and latch the run control.

    The byte index drives everything: a word is complete on the fourth byte
    after the header, at which point it is written and the address advances. A
    frame that would write past the end of memory is rejected rather than
    silently wrapped.
    """

    def __init__(self, *, mem_words: int, domain: str = "sync"):
        self.mem_words = mem_words
        self.domain = domain

        # From the frame decoder.
        self.rx_byte = Signal(8)
        self.rx_stb = Signal()
        self.rx_index = Signal(8)
        self.rx_length = Signal(8)

        # To program memory's loader port.
        self.stb = Signal()
        self.addr = Signal(range(mem_words))
        self.data = Signal(32)

        # Control.
        self.run = Signal()

        # Reporting.
        self.handled = Signal()
        self.error = Signal()

    def elaborate(self, platform):
        m = Module()
        d = m.d[self.domain]

        packet = Signal(8)
        address = Signal(32)
        count = Signal(16)
        base = Signal(32)
        word = Signal(32)
        written = Signal(16)

        loading = Signal()
        in_data = Signal()
        # The whole frame is in range only if its last word is. base and count
        # are only meaningful once the header has been consumed, but stb is
        # gated by this, and stb is only ever raised in the data phase.
        out_of_range = Signal()

        m.d.comb += [
            loading.eq(packet == PKT_LOAD),
            in_data.eq(self.rx_index >= LOAD_HEADER),
            out_of_range.eq((base + count) > self.mem_words),
            self.handled.eq((packet == PKT_LOAD) | (packet == PKT_RUN)),
        ]

        d += self.stb.eq(0)
        d += self.error.eq(0)

        with m.If(self.rx_stb):
            with m.If(self.rx_index == 0):
                d += packet.eq(self.rx_byte)
                d += written.eq(0)
                d += word.eq(0)
                d += address.eq(0)
                d += count.eq(0)
            with m.Elif(loading & ~in_data):
                with m.If(self.rx_index <= 4):
                    d += address.eq(Cat(address[8:], self.rx_byte))
                with m.Else():
                    d += count.eq(Cat(count[8:], self.rx_byte))
                    with m.If(self.rx_index == LOAD_HEADER - 1):
                        # The address is complete, so its word form is too.
                        d += base.eq(address[2:])
            with m.Elif(loading):
                d += word.eq(Cat(word[8:], self.rx_byte))
                with m.If((self.rx_index - LOAD_HEADER) % 4 == 3):
                    with m.If(out_of_range):
                        d += self.error.eq(1)
                    with m.Else():
                        d += self.stb.eq(1)
                        d += self.addr.eq(base + written)
                        d += self.data.eq(Cat(word[8:], self.rx_byte))
                        d += written.eq(written + 1)
            with m.Elif(packet == PKT_RUN):
                with m.If(self.rx_index == 1):
                    d += self.run.eq(self.rx_byte[0])

        return m
