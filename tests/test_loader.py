# amaranth: UnusedElaboratable=disable
"""The phase 3 program loader."""

from amaranth.sim import Simulator

from xsynth.hdl.loader import ProgramLoader
from xsynth.protocol import (
    MAX_LOAD_WORDS,
    PKT_LOAD,
    PKT_RUN,
    encode_load,
    load_packets,
)

MEM_WORDS = 256


def load_payload(address: int, words) -> bytes:
    """The payload of a load frame, without the framing."""
    payload = bytearray([PKT_LOAD])
    payload += address.to_bytes(4, "little")
    payload += len(words).to_bytes(2, "little")
    for word in words:
        payload += (word & 0xFFFF_FFFF).to_bytes(4, "little")
    return bytes(payload)


def feed(payload: bytes, *, mem_words: int = MEM_WORDS):
    """Run one validated payload through the loader; return its writes."""
    dut = ProgramLoader(mem_words=mem_words)
    writes: list[tuple[int, int]] = []
    errors = 0
    run: int | None = None

    async def bench(ctx):
        nonlocal errors, run
        for index, byte in enumerate(payload):
            ctx.set(dut.rx_byte, byte)
            ctx.set(dut.rx_index, index)
            ctx.set(dut.rx_length, len(payload))
            ctx.set(dut.rx_stb, 1)
            await ctx.tick()
            ctx.set(dut.rx_stb, 0)
            if ctx.get(dut.stb):
                writes.append((ctx.get(dut.addr), ctx.get(dut.data)))
            if ctx.get(dut.error):
                errors += 1
            if ctx.get(dut.handled) and index == 1 and payload[0] == PKT_RUN:
                run = ctx.get(dut.run)
            await ctx.tick()

    sim = Simulator(dut)
    sim.add_clock(1 / 27e6)
    sim.add_testbench(bench)
    sim.run()
    return writes, errors, run


def test_a_load_frame_writes_every_word():
    words = [0x11111111, 0x22222222, 0x33333333]
    writes, errors, _ = feed(load_payload(0x40, words))

    assert errors == 0
    assert writes == [
        (0x40 // 4 + 0, 0x11111111),
        (0x40 // 4 + 1, 0x22222222),
        (0x40 // 4 + 2, 0x33333333),
    ]


def test_a_full_packet_of_words_is_written():
    words = [0x1000 + index for index in range(MAX_LOAD_WORDS)]
    writes, errors, _ = feed(load_payload(0, words))

    assert errors == 0
    assert [data for _, data in writes] == words
    assert [address for address, _ in writes] == list(range(MAX_LOAD_WORDS))


def test_a_load_past_the_end_of_memory_is_rejected():
    # One word starting at the very last word is fine; one word later is not.
    _, errors, _ = feed(load_payload((MEM_WORDS - 1) * 4, [0xCAFEF00D]))
    assert errors == 0

    writes, errors, _ = feed(load_payload(MEM_WORDS * 4, [0xCAFEF00D]))
    assert errors == 1
    assert writes == []


def test_a_run_frame_sets_the_run_control():
    _, errors, run = feed(bytes([PKT_RUN, 1]))
    assert errors == 0
    assert run == 1

    _, errors, run = feed(bytes([PKT_RUN, 0]))
    assert errors == 0
    assert run == 0


def test_the_encoder_and_the_loader_agree():
    """The host's framing and the hardware's layout must match exactly."""
    words = [0xDEADBEEF, 0x0BADF00D]
    frame = encode_load(0x20, words)
    # SOF0 SOF1 LEN, then the payload, then the two checksum bytes.
    payload = frame[3:-2]
    assert payload == load_payload(0x20, words)

    writes, errors, _ = feed(payload)
    assert errors == 0
    assert writes == [(0x20 // 4, 0xDEADBEEF), (0x20 // 4 + 1, 0x0BADF00D)]


def test_a_firmware_image_splits_into_working_packets():
    image = bytes(range(64))
    packets = list(load_packets(image))
    assert len(packets) == 3  # 16 words, 6 per packet

    loaded: list[tuple[int, int]] = []
    for packet in packets:
        writes, errors, _ = feed(packet[3:-2])
        assert errors == 0
        loaded.extend(writes)

    expected = [
        (index, int.from_bytes(image[4 * index:4 * index + 4], "little"))
        for index in range(len(image) // 4)
    ]
    assert loaded == expected
