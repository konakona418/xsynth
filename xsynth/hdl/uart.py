"""UART transmit and receive, 8N1, LSB first.

The control domain runs at the board's raw 27 MHz oscillator, which is not an
integer multiple of any common baud rate. The divisor is therefore rounded to
the nearest clock, and :class:`UartTiming` reports the resulting baud error so
that a test can assert it stays well inside the ~2% a UART tolerates.

Both blocks sample the line with a two-flop synchroniser and locate the centre
of each bit, so they are insensitive to the exact phase of the start edge.
"""

from __future__ import annotations

from dataclasses import dataclass

from amaranth import Cat, Elaboratable, Mux, Module, Signal


@dataclass(frozen=True)
class UartTiming:
    """A baud rate expressed as an integer number of clock cycles per bit."""

    clock_hz: int
    baud: int
    divisor: int

    @property
    def actual_baud(self) -> float:
        return self.clock_hz / self.divisor

    @property
    def error(self) -> float:
        """Fractional baud error; positive means we run slightly fast."""
        return self.actual_baud / self.baud - 1


def uart_timing(clock_hz: int, baud: int, *, min_divisor: int = 8) -> UartTiming:
    if baud <= 0:
        raise ValueError("baud rate must be positive")
    divisor = round(clock_hz / baud)
    if divisor < min_divisor:
        raise ValueError(
            f"{baud} baud is too fast for a {clock_hz} Hz clock: "
            f"the divisor would be {divisor}, below the minimum {min_divisor}"
        )
    return UartTiming(clock_hz=clock_hz, baud=baud, divisor=divisor)


class UartRx(Elaboratable):
    """8N1 receiver.

    ``stb`` pulses for one cycle when a byte has been received and latched into
    ``data``. A bad stop bit is discarded without a ``stb``; the receiver always
    returns to idle, so a glitch cannot wedge it.
    """

    def __init__(self, divisor: int, *, data_bits: int = 8, domain: str = "sync"):
        if divisor < 4:
            raise ValueError("divisor must be at least 4 to sample a bit")
        self.divisor = divisor
        self.data_bits = data_bits
        self.domain = domain

        self.rx = Signal(init=1)
        self.data = Signal(data_bits)
        self.stb = Signal()
        self.busy = Signal()

    def elaborate(self, platform):
        m = Module()
        d = m.d[self.domain]
        half = self.divisor // 2

        # Two-flop synchroniser: bit 1 is the level two cycles ago.
        rx_sync = Signal(2, init=0b11)
        d += rx_sync.eq(Cat(self.rx, rx_sync[0]))

        counter = Signal(range(self.divisor), init=0)
        bit = Signal(range(self.data_bits))
        shift = Signal(self.data_bits)

        d += self.stb.eq(0)

        with m.FSM(domain=self.domain) as fsm:
            with m.State("IDLE"):
                with m.If(~rx_sync[1]):
                    d += counter.eq(half - 1)
                    m.next = "START"
            with m.State("START"):
                # Sample the middle of the start bit: a glitch on the falling
                # edge must not be mistaken for a frame.
                with m.If(counter == 0):
                    with m.If(rx_sync[1]):
                        m.next = "IDLE"
                    with m.Else():
                        d += counter.eq(self.divisor - 1)
                        d += bit.eq(0)
                        d += shift.eq(0)
                        m.next = "DATA"
                with m.Else():
                    d += counter.eq(counter - 1)
            with m.State("DATA"):
                with m.If(counter == 0):
                    d += shift.eq(shift | (rx_sync[1] << bit))
                    d += counter.eq(self.divisor - 1)
                    with m.If(bit == self.data_bits - 1):
                        m.next = "STOP"
                    with m.Else():
                        d += bit.eq(bit + 1)
                with m.Else():
                    d += counter.eq(counter - 1)
            with m.State("STOP"):
                with m.If(counter == 0):
                    m.next = "IDLE"
                    with m.If(rx_sync[1]):
                        d += self.data.eq(shift)
                        d += self.stb.eq(1)
                with m.Else():
                    d += counter.eq(counter - 1)

        m.d.comb += self.busy.eq(~fsm.ongoing("IDLE"))
        return m


class UartTx(Elaboratable):
    """8N1 transmitter.

    Assert ``stb`` for one cycle with ``data`` valid to start a transmission;
    ``busy`` is high until the stop bit has been shifted out. ``stb`` is
    ignored while busy, so the caller must check ``busy``.
    """

    def __init__(self, divisor: int, *, data_bits: int = 8, domain: str = "sync"):
        if divisor < 4:
            raise ValueError("divisor must be at least 4 to shape a bit")
        self.divisor = divisor
        self.data_bits = data_bits
        self.domain = domain

        self.data = Signal(data_bits)
        self.stb = Signal()
        self.tx = Signal(init=1)
        self.busy = Signal()

    def elaborate(self, platform):
        m = Module()
        d = m.d[self.domain]

        shift = Signal(self.data_bits)
        bit = Signal(range(self.data_bits))
        counter = Signal(range(self.divisor), init=0)

        with m.FSM(domain=self.domain) as fsm:
            with m.State("IDLE"):
                with m.If(self.stb):
                    d += shift.eq(self.data)
                    d += counter.eq(self.divisor - 1)
                    m.next = "START"
            with m.State("START"):
                with m.If(counter == 0):
                    d += counter.eq(self.divisor - 1)
                    d += bit.eq(0)
                    m.next = "DATA"
                with m.Else():
                    d += counter.eq(counter - 1)
            with m.State("DATA"):
                with m.If(counter == 0):
                    d += shift.eq(shift >> 1)
                    d += counter.eq(self.divisor - 1)
                    with m.If(bit == self.data_bits - 1):
                        m.next = "STOP"
                    with m.Else():
                        d += bit.eq(bit + 1)
                with m.Else():
                    d += counter.eq(counter - 1)
            with m.State("STOP"):
                with m.If(counter == 0):
                    m.next = "IDLE"
                with m.Else():
                    d += counter.eq(counter - 1)

        m.d.comb += [
            self.tx.eq(
                Mux(fsm.ongoing("START"), 0,
                    Mux(fsm.ongoing("DATA"), shift[0], 1))
            ),
            self.busy.eq(~fsm.ongoing("IDLE")),
        ]
        return m
