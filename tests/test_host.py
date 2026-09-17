"""Host client: framing on the wire and response parsing.

A fake transport stands in for the serial port, so the exact bytes the host
sends and the way it decodes replies are both pinned down without a board.
"""

import pytest

from xsynth.host.client import Status, XsynthClient
from xsynth.hdl.audio import phase_step
from xsynth.hdl.voice import WAVES
from xsynth.protocol import (
    CPU_RUNNING,
    FLAG_CRC_ERROR,
    FLAG_LOCKED,
    OP_NOTE_ON,
    OP_SET_WAVE,
    PKT_PING,
    PKT_PONG,
    PKT_STATUS,
    PKT_STATUS_REPLY,
    Command,
    encode_commands,
    encode_frame,
    encode_run,
    load_packets,
)


class _FakeSerial:
    """Collects writes and replays a canned response."""

    name = "/dev/fake"

    def __init__(self, response: bytes = b""):
        self.written = bytearray()
        self._response = bytearray(response)

    def write(self, data: bytes) -> int:
        self.written += data
        return len(data)

    def flush(self) -> None:
        pass

    def read(self, size: int = 1) -> bytes:
        chunk = bytes(self._response[:size])
        del self._response[:size]
        return chunk

    def reset_input_buffer(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_note_on_frames_the_wave_then_the_note():
    fake = _FakeSerial()
    client = XsynthClient(transport=fake)
    client.note_on(440.0, wave="saw")

    step = phase_step(440.0, 48_000)
    expected = encode_commands([
        Command(OP_SET_WAVE, value=WAVES.index("saw")),
        Command(OP_NOTE_ON, value=step),
    ])
    assert bytes(fake.written) == expected


def test_commands_can_be_batched_with_delays():
    fake = _FakeSerial()
    client = XsynthClient(transport=fake)
    client.send_commands([
        Command(OP_NOTE_ON, value=100, delay=0),
        Command(OP_NOTE_ON, value=200, delay=48_000),
    ])
    assert bytes(fake.written).startswith(bytes([0xAA, 0x55, 17, 0x01]))


def test_ping_reads_a_pong():
    fake = _FakeSerial(encode_frame(bytes([PKT_PONG])))
    client = XsynthClient(transport=fake)
    assert client.ping()
    assert bytes(fake.written) == encode_frame(bytes([PKT_PING]))


def test_status_is_decoded():
    payload = bytes([
        PKT_STATUS_REPLY, 3, 1 << FLAG_LOCKED, 3, 1 << CPU_RUNNING,
        0x11, 0x22, 0x33, 0x44,   # samples
        0xAA, 0xBB, 0xCC, 0xDD,   # the firmware's scratch register
        0x01, 0x02, 0x03, 0x04,   # the free-running counter
    ])
    fake = _FakeSerial(encode_frame(payload))
    client = XsynthClient(transport=fake)

    status = client.status()
    assert status.version == 3
    assert status.locked
    assert status.fifo_level == 3
    assert status.samples == 0x44332211
    assert status.cpu_status == 0xDDCCBBAA
    assert status.cpu_counter == 0x04030201
    assert status.running
    assert not status.halted
    assert not status.trapped
    assert status.errors == []
    assert bytes(fake.written) == encode_frame(bytes([PKT_STATUS]))


def test_status_reports_sticky_errors():
    status = Status(version=3, flags=1 << FLAG_CRC_ERROR, fifo_level=0,
                    cpu_flags=0, samples=0, cpu_status=0, cpu_counter=0)
    assert status.errors == ["crc_error"]
    assert not status.locked


def test_a_missing_response_times_out():
    client = XsynthClient(transport=_FakeSerial(), timeout=0.05)
    with pytest.raises(TimeoutError):
        client.ping()


def test_amplitude_outside_the_unit_range_is_rejected():
    client = XsynthClient(transport=_FakeSerial())
    with pytest.raises(ValueError):
        client.set_amp(1.5)
    with pytest.raises(ValueError):
        client.set_amp(-0.1)


def test_an_unknown_waveform_is_rejected():
    client = XsynthClient(transport=_FakeSerial())
    with pytest.raises(SystemExit):
        client.set_wave("noise")


def test_load_sends_one_frame_per_packet():
    fake = _FakeSerial()
    client = XsynthClient(transport=fake)

    image = bytes(range(32))  # eight words, so two packets
    assert client.load(image) == len(image)

    assert bytes(fake.written) == b"".join(load_packets(image))


def test_run_and_stop_send_the_run_packet():
    fake = _FakeSerial()
    client = XsynthClient(transport=fake)

    client.run(True)
    client.run(False)

    assert bytes(fake.written) == encode_run(True) + encode_run(False)


def test_a_load_image_is_rounded_up_to_whole_words():
    fake = _FakeSerial()
    client = XsynthClient(transport=fake)

    client.load(b"\x01\x02\x03")  # not a whole number of words

    assert bytes(fake.written) == b"".join(load_packets(b"\x01\x02\x03"))
