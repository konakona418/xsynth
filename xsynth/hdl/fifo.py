"""A dual-clock asynchronous FIFO with Gray-coded pointers.

The control domain (27 MHz) and the pixel/audio domain (25.2 MHz) have no fixed
phase relationship, so commands must cross in a real asynchronous FIFO rather
than through a synchronised multi-bit bus. Only the Gray-coded read and write
pointers cross a clock boundary; the payload itself never does.

The read port is combinational, which makes the FIFO first-word-fall-through:
while ``r_empty`` is low, ``r_data`` already holds the head entry, and pulsing
``r_inc`` pops it. That is what lets the sample scheduler consume a command on
the exact ``audio_strobe`` edge it is due.
"""

from __future__ import annotations

from amaranth import Elaboratable, Module, Signal, unsigned
from amaranth.lib.memory import Memory


def _sync(m: Module, signal: Signal, domain: str) -> Signal:
    """Two-flop synchroniser for a single-bit-per-FF Gray code."""
    meta = Signal(signal.shape(), init=0)
    out = Signal(signal.shape(), init=0)
    d = m.d[domain]
    d += meta.eq(signal)
    d += out.eq(meta)
    return out


class AsyncFifo(Elaboratable):
    """Write in ``w_domain``, read in ``r_domain``.

    ``depth`` must be a power of two of at least 4. The write and read sides
    have independent synchronous resets; because only the pointers are reset,
    the synchroniser chains follow within two cycles and no data can be read
    across a reset.
    """

    def __init__(self, *, width: int, depth: int,
                 w_domain: str = "sync", r_domain: str = "pixel"):
        if depth < 4 or depth & (depth - 1):
            raise ValueError("depth must be a power of two and at least 4")
        self.width = width
        self.depth = depth
        self.w_domain = w_domain
        self.r_domain = r_domain

        self.addr_bits = depth.bit_length() - 1
        self.ptr_bits = self.addr_bits + 1

        self.w_data = Signal(width)
        self.w_inc = Signal()
        self.w_full = Signal()
        self.w_rst = Signal()
        self.w_level = Signal(self.ptr_bits)

        self.r_data = Signal(width)
        self.r_inc = Signal()
        self.r_empty = Signal()
        self.r_rst = Signal()

    def elaborate(self, platform):
        m = Module()
        d_w = m.d[self.w_domain]
        d_r = m.d[self.r_domain]

        w_bin = Signal(self.ptr_bits, init=0)
        r_bin = Signal(self.ptr_bits, init=0)
        w_gray = Signal(self.ptr_bits)
        r_gray = Signal(self.ptr_bits)
        m.d.comb += [
            w_gray.eq(w_bin ^ (w_bin >> 1)),
            r_gray.eq(r_bin ^ (r_bin >> 1)),
        ]

        memory = Memory(
            shape=unsigned(self.width),
            depth=self.depth,
            init=[0] * self.depth,
        )
        m.submodules.memory = memory
        write_port = memory.write_port(domain=self.w_domain)
        read_port = memory.read_port(domain="comb")
        m.d.comb += [
            write_port.addr.eq(w_bin[: self.addr_bits]),
            write_port.data.eq(self.w_data),
            write_port.en.eq(self.w_inc & ~self.w_full),
            read_port.addr.eq(r_bin[: self.addr_bits]),
            self.r_data.eq(read_port.data),
        ]

        w_rptr = _sync(m, r_gray, self.w_domain)
        r_wptr = _sync(m, w_gray, self.r_domain)

        # Full when the write pointer has lapped the read pointer: equal address
        # bits with the top two Gray bits inverted. Empty when they are equal.
        full_mask = (1 << self.addr_bits) | (1 << (self.addr_bits - 1))
        m.d.comb += [
            self.w_full.eq(w_gray == (w_rptr ^ full_mask)),
            self.r_empty.eq(r_gray == r_wptr),
        ]

        # Occupancy, seen from the write side: both pointers are available there
        # (one is synchronised), so the status path needs no further crossing.
        r_bin_synced = w_rptr
        for shift in (1, 2, 4, 8, 16):
            if shift < self.ptr_bits:
                r_bin_synced = r_bin_synced ^ (r_bin_synced >> shift)
        m.d.comb += self.w_level.eq(w_bin - r_bin_synced)

        with m.If(self.w_rst):
            d_w += w_bin.eq(0)
        with m.Elif(self.w_inc & ~self.w_full):
            d_w += w_bin.eq(w_bin + 1)

        with m.If(self.r_rst):
            d_r += r_bin.eq(0)
        with m.Elif(self.r_inc & ~self.r_empty):
            d_r += r_bin.eq(r_bin + 1)

        return m
