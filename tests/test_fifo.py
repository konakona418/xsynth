# amaranth: UnusedElaboratable=disable
"""Dual-clock FIFO: ordering across domains, full/empty and reset."""

import pytest
from amaranth.sim import Simulator

from xsynth.hdl.fifo import AsyncFifo

WRITE_HZ = 27_000_000
READ_HZ = 25_200_000


def test_depth_must_be_a_power_of_two_of_at_least_four():
    for bad in (0, 1, 2, 3, 6, 12):
        with pytest.raises(ValueError):
            AsyncFifo(width=8, depth=bad)


def test_entries_cross_the_clock_boundary_in_order():
    depth = 16
    count = 200
    dut = AsyncFifo(width=8, depth=depth)
    received: list[int] = []

    async def producer(ctx):
        for index in range(count):
            while ctx.get(dut.w_full):
                await ctx.tick("sync")
            ctx.set(dut.w_data, index & 0xFF)
            ctx.set(dut.w_inc, 1)
            await ctx.tick("sync")
            ctx.set(dut.w_inc, 0)

    async def consumer(ctx):
        while len(received) < count:
            if not ctx.get(dut.r_empty):
                received.append(ctx.get(dut.r_data))
                ctx.set(dut.r_inc, 1)
            else:
                ctx.set(dut.r_inc, 0)
            await ctx.tick("pixel")

    sim = Simulator(dut)
    sim.add_clock(1 / WRITE_HZ, domain="sync")
    sim.add_clock(1 / READ_HZ, domain="pixel")
    sim.add_testbench(producer)
    sim.add_testbench(consumer)
    sim.run()

    assert received == [index & 0xFF for index in range(count)]


def test_full_and_empty_boundaries():
    depth = 4
    dut = AsyncFifo(width=8, depth=depth)

    async def settle(ctx, cycles: int = 4):
        for _ in range(cycles):
            await ctx.tick("sync")

    async def bench(ctx):
        assert ctx.get(dut.r_empty)
        assert not ctx.get(dut.w_full)

        for index in range(depth):
            ctx.set(dut.w_data, index + 1)
            ctx.set(dut.w_inc, 1)
            await ctx.tick("sync")
        ctx.set(dut.w_inc, 0)
        await settle(ctx)
        assert ctx.get(dut.w_full), "a full FIFO must report full"

        # A write while full is dropped rather than overwriting the head.
        ctx.set(dut.w_data, 0xEE)
        ctx.set(dut.w_inc, 1)
        await ctx.tick("sync")
        ctx.set(dut.w_inc, 0)
        await settle(ctx)

        for expected in range(1, depth + 1):
            assert not ctx.get(dut.r_empty)
            assert ctx.get(dut.r_data) == expected
            ctx.set(dut.r_inc, 1)
            await ctx.tick("pixel")
            ctx.set(dut.r_inc, 0)
            await ctx.tick("pixel")
        assert ctx.get(dut.r_empty), "a drained FIFO must report empty"

    sim = Simulator(dut)
    sim.add_clock(1 / WRITE_HZ, domain="sync")
    sim.add_clock(1 / READ_HZ, domain="pixel")
    sim.add_testbench(bench)
    sim.run()


def test_write_reset_returns_the_write_side_to_empty():
    depth = 8
    dut = AsyncFifo(width=8, depth=depth)

    async def bench(ctx):
        for index in range(depth):
            ctx.set(dut.w_data, index)
            ctx.set(dut.w_inc, 1)
            await ctx.tick("sync")
        ctx.set(dut.w_inc, 0)
        for _ in range(4):
            await ctx.tick("sync")

        ctx.set(dut.w_rst, 1)
        await ctx.tick("sync")
        ctx.set(dut.w_rst, 0)
        for _ in range(8):
            await ctx.tick("sync")

        assert ctx.get(dut.r_empty), "the write reset must drain the FIFO"

    sim = Simulator(dut)
    sim.add_clock(1 / WRITE_HZ, domain="sync")
    sim.add_clock(1 / READ_HZ, domain="pixel")
    sim.add_testbench(bench)
    sim.run()
