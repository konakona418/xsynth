# amaranth: UnusedElaboratable=disable
"""Hardware frame decoder and transmitter."""

from amaranth import Array, Elaboratable, Module
from amaranth.sim import Simulator

from xsynth.hdl.framing import FrameDecoder, FrameTx
from xsynth.protocol import (
    MAX_PAYLOAD,
    OP_SET_FREQ,
    PKT_COMMANDS,
    PKT_PING,
    PKT_STATUS,
    Command,
    FrameDecoder as ReferenceDecoder,
    encode_frame,
)

PAYLOADS = [
    bytes([PKT_PING]),
    bytes([PKT_STATUS]),
    bytes([PKT_COMMANDS]) + Command(OP_SET_FREQ, value=1234).pack(),
    bytes(range(1, MAX_PAYLOAD + 1)),
]


class _Sender(Elaboratable):
    """Drives a FrameTx from a Python payload, presenting one byte per index."""

    def __init__(self, payload: bytes):
        self.payload = bytes(payload)
        self.tx = FrameTx()

    def elaborate(self, platform):
        m = Module()
        m.submodules.tx = self.tx
        m.d.comb += self.tx.byte_in.eq(Array(self.payload)[self.tx.index])
        return m


def test_decoder_matches_the_reference_on_a_clean_stream():
    stream = b"".join(encode_frame(payload) for payload in PAYLOADS)
    dut = FrameDecoder()

    expected = []
    reference = ReferenceDecoder()
    for byte in stream:
        payload = reference.feed(byte)
        if payload is not None:
            expected.append(payload)

    received: list[bytes] = []
    current: list[int] = []

    async def bench(ctx):
        for byte in stream:
            ctx.set(dut.rx_byte, byte)
            ctx.set(dut.rx_stb, 1)
            await ctx.tick()
            ctx.set(dut.rx_stb, 0)
            for _ in range(80):
                if ctx.get(dut.out_stb):
                    current.append(ctx.get(dut.out_byte))
                    if len(current) == ctx.get(dut.out_length):
                        received.append(bytes(current))
                        current.clear()
                await ctx.tick()

    sim = Simulator(dut)
    sim.add_clock(1 / 27e6)
    sim.add_testbench(bench)
    sim.run()

    assert received == expected
    assert len(received) == len(PAYLOADS)


def test_decoder_reports_a_checksum_error_and_recovers():
    good = encode_frame(bytes([PKT_PING]))
    bad = bytearray(encode_frame(bytes([PKT_STATUS])))
    bad[3] ^= 0xFF
    stream = bytes(bad) + good
    dut = FrameDecoder()

    received: list[bytes] = []
    current: list[int] = []
    errors = 0

    async def bench(ctx):
        nonlocal errors
        for byte in stream:
            ctx.set(dut.rx_byte, byte)
            ctx.set(dut.rx_stb, 1)
            await ctx.tick()
            ctx.set(dut.rx_stb, 0)
            for _ in range(80):
                if ctx.get(dut.crc_error):
                    errors += 1
                if ctx.get(dut.out_stb):
                    current.append(ctx.get(dut.out_byte))
                    if len(current) == ctx.get(dut.out_length):
                        received.append(bytes(current))
                        current.clear()
                await ctx.tick()

    sim = Simulator(dut)
    sim.add_clock(1 / 27e6)
    sim.add_testbench(bench)
    sim.run()

    assert errors == 1
    assert received == [bytes([PKT_PING])]


def test_transmitter_emits_the_reference_frame():
    for payload in PAYLOADS:
        sender = _Sender(payload)
        expected = encode_frame(payload)
        out: list[int] = []

        async def bench(ctx):
            ctx.set(sender.tx.length, len(payload))
            ctx.set(sender.tx.tx_ready, 1)
            ctx.set(sender.tx.start, 1)
            await ctx.tick()
            ctx.set(sender.tx.start, 0)
            for _ in range(4 * len(expected) + 16):
                if ctx.get(sender.tx.tx_stb):
                    out.append(ctx.get(sender.tx.tx_data))
                await ctx.tick()

        sim = Simulator(sender)
        sim.add_clock(1 / 27e6)
        sim.add_testbench(bench)
        sim.run()

        assert bytes(out) == expected, payload
