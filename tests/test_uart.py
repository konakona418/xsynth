# amaranth: UnusedElaboratable=disable
"""UART: baud timing, loopback and framing-error recovery."""

import pytest
from amaranth import Elaboratable, Module
from amaranth.sim import Simulator

from xsynth.hdl.uart import UartRx, UartTx, uart_timing

CLOCK_HZ = 27_000_000
BAUD = 115_200
DIVISOR = uart_timing(CLOCK_HZ, BAUD).divisor


class _Loopback(Elaboratable):
    """Transmitter wired straight into the receiver."""

    def __init__(self, divisor: int):
        self.tx = UartTx(divisor)
        self.rx = UartRx(divisor)

    def elaborate(self, platform):
        m = Module()
        m.submodules.tx = self.tx
        m.submodules.rx = self.rx
        m.d.comb += self.rx.rx.eq(self.tx.tx)
        return m


def test_timing_rounds_to_the_nearest_clock():
    timing = uart_timing(CLOCK_HZ, BAUD)
    assert timing.divisor == 234
    assert timing.actual_baud == pytest.approx(CLOCK_HZ / 234)
    # UARTs tolerate a few percent; anything near this is comfortable.
    assert abs(timing.error) < 0.005


def test_timing_rejects_impossible_baud_rates():
    with pytest.raises(ValueError):
        uart_timing(CLOCK_HZ, 0)
    with pytest.raises(ValueError):
        uart_timing(CLOCK_HZ, CLOCK_HZ)
    with pytest.raises(ValueError):
        UartRx(2)
    with pytest.raises(ValueError):
        UartTx(2)


def test_loopback_returns_every_byte_in_order():
    payload = [0x00, 0x55, 0xAA, 0xFF, 0x5A, 0x01, 0x80]
    dut = _Loopback(DIVISOR)
    received: list[int] = []

    async def bench(ctx):
        sent = 0
        limit = DIVISOR * 12 * (len(payload) + 2)
        for _ in range(limit):
            if ctx.get(dut.rx.stb):
                received.append(ctx.get(dut.rx.data))
                if len(received) == len(payload):
                    return
            if sent < len(payload) and not ctx.get(dut.tx.busy):
                ctx.set(dut.tx.data, payload[sent])
                ctx.set(dut.tx.stb, 1)
                sent += 1
            else:
                ctx.set(dut.tx.stb, 0)
            await ctx.tick()
        raise AssertionError(
            f"only received {received!r} after {limit} cycles"
        )

    sim = Simulator(dut)
    sim.add_clock(1 / CLOCK_HZ)
    sim.add_testbench(bench)
    sim.run()

    assert received == payload


def test_a_bad_stop_bit_is_dropped_without_wedging_the_receiver():
    dut = UartRx(DIVISOR)
    received: list[int] = []

    async def send_byte(ctx, value: int, *, stop: int = 1):
        ctx.set(dut.rx, 0)  # start bit
        for _ in range(DIVISOR):
            await ctx.tick()
        for index in range(8):  # LSB first
            ctx.set(dut.rx, (value >> index) & 1)
            for _ in range(DIVISOR):
                await ctx.tick()
        ctx.set(dut.rx, stop)
        for _ in range(DIVISOR):
            await ctx.tick()
        ctx.set(dut.rx, 1)  # idle between frames
        for _ in range(2 * DIVISOR):
            await ctx.tick()

    async def driver(ctx):
        ctx.set(dut.rx, 1)
        for _ in range(4 * DIVISOR):
            await ctx.tick()
        await send_byte(ctx, 0x3C, stop=0)
        await send_byte(ctx, 0xA5)

    async def collector(ctx):
        for _ in range(60 * DIVISOR):
            if ctx.get(dut.stb):
                received.append(ctx.get(dut.data))
            await ctx.tick()

    sim = Simulator(dut)
    sim.add_clock(1 / CLOCK_HZ)
    sim.add_testbench(driver)
    sim.add_testbench(collector)
    sim.run()

    assert received == [0xA5]
