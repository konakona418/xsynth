"""Control protocol: commands, frames and the reference decoder."""

import pytest

from xsynth.protocol import (
    MAX_PAYLOAD,
    OP_SET_FREQ,
    PKT_COMMANDS,
    PKT_PING,
    PKT_STATUS,
    SOF0,
    SOF1,
    Command,
    FrameDecoder,
    decode_response,
    encode_frame,
)


def _decode_all(stream: bytes) -> list[bytes]:
    decoder = FrameDecoder()
    out = []
    for byte in stream:
        payload = decoder.feed(byte)
        if payload is not None:
            out.append(payload)
    return out


def test_command_round_trips_through_its_64_bit_word():
    command = Command(OP_SET_FREQ, voice=3, value=123456, delay=4000)
    packed = command.pack()
    assert len(packed) == 8
    assert Command.unpack(packed) == command
    assert command.word == int.from_bytes(packed, "little")


def test_command_fields_are_range_checked():
    Command(0xFF, voice=0xFF, value=(1 << 32) - 1, delay=0xFFFF)
    for bad in ({"opcode": 0x100}, {"voice": 0x100},
                {"value": 1 << 32}, {"delay": 1 << 16}):
        with pytest.raises(ValueError):
            Command(**{"opcode": 0x01, **bad})
    with pytest.raises(ValueError):
        Command.unpack(b"\x00" * 7)


def test_frames_round_trip_through_the_decoder():
    payloads = [
        bytes([PKT_PING]),
        bytes([PKT_STATUS]),
        bytes([PKT_COMMANDS]) + Command(OP_SET_FREQ, value=99).pack(),
    ]
    stream = b"".join(encode_frame(payload) for payload in payloads)
    assert _decode_all(stream) == payloads


def test_a_corrupted_frame_is_dropped_and_the_next_survives():
    good = encode_frame(bytes([PKT_PING]))
    bad = bytearray(encode_frame(bytes([PKT_STATUS])))
    bad[3] ^= 0xFF
    assert _decode_all(bytes(bad) + good) == [bytes([PKT_PING])]


def test_an_impossible_length_resynchronises():
    stream = bytes([SOF0, SOF1, MAX_PAYLOAD + 1])
    stream += encode_frame(bytes([PKT_PING]))
    assert _decode_all(stream) == [bytes([PKT_PING])]


def test_a_repeated_start_byte_does_not_lose_the_frame():
    frame = encode_frame(bytes([PKT_PING]))
    stream = bytes([SOF0, SOF0, SOF1]) + frame[2:]
    assert _decode_all(stream) == [bytes([PKT_PING])]


def test_a_payload_longer_than_the_limit_is_rejected():
    with pytest.raises(ValueError):
        encode_frame(bytes(MAX_PAYLOAD + 1))


def test_decode_response_splits_the_packet_type():
    packet, arguments = decode_response(bytes([0x82, 1, 2, 3]))
    assert packet == 0x82
    assert arguments == bytes([1, 2, 3])
    with pytest.raises(ValueError):
        decode_response(b"")
