# amaranth: UnusedElaboratable=disable
"""CRC-16/CCITT: reference implementation and the hardware step."""

import random

from amaranth.sim import Simulator

from xsynth.hdl.crc import INIT, crc16, crc16_update, Crc16


def test_matches_the_ccitt_false_check_value():
    # The standard "123456789" check value for CRC-16/CCITT-FALSE.
    assert crc16(b"123456789") == 0x29B1


def test_initial_value_is_all_ones():
    assert INIT == 0xFFFF
    assert crc16(b"") == INIT


def test_update_is_a_fold_of_the_whole_message():
    assert crc16(b"abc") == crc16_update(crc16_update(crc16_update(INIT, ord("a")),
                                                       ord("b")),
                                         ord("c"))


def test_hardware_matches_the_reference():
    payload = bytes(random.Random(1).randrange(256) for _ in range(64))
    dut = Crc16()

    async def bench(ctx):
        ctx.set(dut.start, 1)
        await ctx.tick()
        ctx.set(dut.start, 0)
        for byte in payload:
            ctx.set(dut.byte, byte)
            ctx.set(dut.stb, 1)
            await ctx.tick()
            ctx.set(dut.stb, 0)
        assert ctx.get(dut.value) == crc16(payload)

    sim = Simulator(dut)
    sim.add_clock(1 / 27e6)
    sim.add_testbench(bench)
    sim.run()


def test_hardware_start_reloads_the_initial_value():
    dut = Crc16()

    async def bench(ctx):
        for byte in b"garbage":
            ctx.set(dut.byte, byte)
            ctx.set(dut.stb, 1)
            await ctx.tick()
            ctx.set(dut.stb, 0)
        assert ctx.get(dut.value) != INIT
        ctx.set(dut.start, 1)
        await ctx.tick()
        ctx.set(dut.start, 0)
        assert ctx.get(dut.value) == INIT

    sim = Simulator(dut)
    sim.add_clock(1 / 27e6)
    sim.add_testbench(bench)
    sim.run()
