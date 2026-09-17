"""Host client: framing on the wire and response parsing.

A fake transport stands in for the serial port, so the exact bytes the host
sends and the way it decodes replies are both pinned down without a board.
"""

import pytest

from xsynth.host.client import ENV_TOP, Status, XsynthClient
from xsynth.hdl.audio import PEAK, phase_step
from xsynth.hdl.voice import WAVES
from xsynth.protocol import (
    CPU_RUNNING,
    FLAG_CRC_ERROR,
    FLAG_LOCKED,
    OP_CLEAR_SCHEDULE,
    OP_NOTE_ON,
    OP_NOTE_OFF,
    OP_SCHEDULE_AT,
    OP_SET_ATTACK,
    OP_SET_DECAY,
    OP_SET_MASTER,
    OP_SET_RELEASE,
    OP_SET_SUSTAIN,
    OP_SET_WAVE,
    PKT_PING,
    PKT_PONG,
    PKT_STATUS,
    PKT_STATUS_REPLY,
    VOICE_ANY,
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
        Command(OP_SET_WAVE, voice=VOICE_ANY, value=WAVES.index("saw")),
        Command(OP_NOTE_ON, voice=VOICE_ANY, value=step),
    ])
    assert bytes(fake.written) == expected


def test_a_note_with_no_voice_asks_the_firmware_to_pick_one():
    fake = _FakeSerial()
    client = XsynthClient(transport=fake)
    client.note_on(440.0)
    client.note_off(440.0)

    step = phase_step(440.0, 48_000)
    assert bytes(fake.written) == (
        encode_commands([Command(OP_NOTE_ON, voice=VOICE_ANY, value=step)])
        + encode_commands([
            Command(OP_NOTE_OFF, voice=VOICE_ANY, value=step),
        ])
    )


def test_a_note_off_by_pitch_carries_the_step_it_was_started_with():
    """The note's identity is the increment, so this is what the firmware
    matches on -- and the host never learns which voice it got."""
    fake = _FakeSerial()
    client = XsynthClient(transport=fake)
    client.note_on(554.37)
    client.note_off(554.37)

    step = phase_step(554.37, 48_000)
    payload = bytes(fake.written)
    assert payload.count(step.to_bytes(4, "little")) == 2


def test_a_note_off_with_neither_a_pitch_nor_a_voice_is_refused():
    fake = _FakeSerial()
    client = XsynthClient(transport=fake)
    with pytest.raises(ValueError, match="needs the hz"):
        client.note_off()


def test_naming_no_voice_on_a_wave_reaches_every_voice():
    fake = _FakeSerial()
    client = XsynthClient(transport=fake)
    client.set_wave("saw")

    assert bytes(fake.written) == encode_commands([
        Command(OP_SET_WAVE, voice=VOICE_ANY, value=WAVES.index("saw")),
    ])


def test_an_anchor_precedes_the_command_it_applies_to():
    fake = _FakeSerial()
    client = XsynthClient(transport=fake)
    client.anchor(250_000)
    client.note_on(440.0, delay=4800)

    step = phase_step(440.0, 48_000)
    assert bytes(fake.written) == (
        encode_commands([Command(OP_SCHEDULE_AT, value=250_000)])
        + encode_commands([
            Command(OP_NOTE_ON, voice=VOICE_ANY, value=step, delay=4800),
        ])
    )


def test_clearing_the_schedule_is_its_own_command():
    fake = _FakeSerial()
    client = XsynthClient(transport=fake)
    client.clear_schedule()
    assert bytes(fake.written) == encode_commands([Command(OP_CLEAR_SCHEDULE)])


def test_now_reads_the_sample_counter_out_of_a_status_reply():
    payload = bytearray([PKT_STATUS_REPLY]) + bytearray(16)
    payload[1 + 4:1 + 8] = (123_456).to_bytes(4, "little")
    fake = _FakeSerial(encode_frame(bytes(payload)))

    client = XsynthClient(transport=fake)
    assert client.now() == 123_456
    assert bytes(fake.written) == encode_frame(bytes([PKT_STATUS]))


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


def test_a_note_can_name_a_voice():
    fake = _FakeSerial()
    client = XsynthClient(transport=fake)
    client.note_on(440.0, voice=3)
    client.note_off(voice=3)

    assert bytes(fake.written) == (
        encode_commands([
            Command(OP_NOTE_ON, voice=3, value=phase_step(440.0, 48_000)),
        ])
        + encode_commands([Command(OP_NOTE_OFF, voice=3)])
    )


def test_an_envelope_stage_is_a_time_to_cross_the_envelope():
    fake = _FakeSerial()
    client = XsynthClient(transport=fake)
    client.set_envelope(attack=1.0)

    rate = round(ENV_TOP / 48_000)
    assert bytes(fake.written) == encode_commands([
        Command(OP_SET_ATTACK, value=rate),
    ])


def test_a_stage_shorter_than_a_sample_is_instant():
    # The engine's own default is the fastest rate there is, and the client
    # must not ask for something the register cannot hold.
    assert XsynthClient.envelope_rate(0.0) == (1 << 24) - 1
    assert XsynthClient.envelope_rate(-1.0) == (1 << 24) - 1
    assert XsynthClient.envelope_rate(1e-9) == (1 << 24) - 1


def test_a_sustain_level_is_a_fraction_of_the_note_peak():
    fake = _FakeSerial()
    client = XsynthClient(transport=fake)
    client.set_envelope(sustain=0.5)

    assert bytes(fake.written) == encode_commands([
        Command(OP_SET_SUSTAIN, value=round(0.5 * PEAK)),
    ])


def test_the_stages_that_were_not_named_are_not_sent():
    fake = _FakeSerial()
    client = XsynthClient(transport=fake)
    client.set_envelope(decay=0.2, release=0.4)

    assert bytes(fake.written) == encode_commands([
        Command(OP_SET_DECAY, value=XsynthClient.envelope_rate(0.2)),
        Command(OP_SET_RELEASE, value=XsynthClient.envelope_rate(0.4)),
    ])


def test_the_master_scales_the_mix():
    fake = _FakeSerial()
    client = XsynthClient(transport=fake)
    client.set_master(0.25)

    assert bytes(fake.written) == encode_commands([
        Command(OP_SET_MASTER, value=round(0.25 * PEAK)),
    ])


def test_a_level_outside_the_unit_range_is_rejected():
    fake = _FakeSerial()
    client = XsynthClient(transport=fake)
    for fraction in (1.5, -0.1):
        with pytest.raises(ValueError):
            client.set_master(fraction)
        with pytest.raises(ValueError):
            client.level_for(fraction)


# --- how the command line settles a voice ---------------------------------
#
# `freq`, `wave` and `amp` have no default, because naming no voice on those
# would have to mean one voice or every voice and guessing wrong silently
# changes a note the user did not mean to touch. The decision is settled before
# the serial port is opened, so a mistake is reported as one.


def _args(**kwargs):
    from argparse import Namespace

    defaults = {"action": "wave", "voice": None, "all": False, "hz": None,
                "note": None}
    defaults.update(kwargs)
    return Namespace(**defaults)


def test_a_target_needs_a_voice_or_all():
    from xsynth.cli import _resolve

    with pytest.raises(SystemExit, match="--voice N, or --all"):
        _resolve(_args(action="wave"))

    args = _args(action="wave", all=True)
    _resolve(args)
    assert args.voice == VOICE_ANY

    args = _args(action="wave", voice=3)
    _resolve(args)
    assert args.voice == 3


def test_a_target_cannot_be_a_voice_and_all_at_once():
    from xsynth.cli import _resolve

    with pytest.raises(SystemExit, match="opposites"):
        _resolve(_args(action="wave", voice=3, all=True))


def test_a_voice_that_does_not_exist_is_refused():
    from xsynth.cli import _resolve

    with pytest.raises(SystemExit, match="no voice 9"):
        _resolve(_args(action="wave", voice=9))


def test_a_note_names_no_voice_unless_it_is_told_to():
    from xsynth.cli import _resolve

    args = _args(action="note-on")
    _resolve(args)
    assert args.voice == VOICE_ANY


def test_a_note_off_needs_a_pitch_or_a_voice():
    from xsynth.cli import _resolve

    with pytest.raises(SystemExit, match="give the pitch"):
        _resolve(_args(action="note-off"))

    args = _args(action="note-off", hz=440.0)
    _resolve(args)
    assert args.voice == VOICE_ANY

    args = _args(action="note-off", voice=2)
    _resolve(args)
    assert args.voice == 2


def test_more_commands_than_fit_go_in_several_frames():
    """A payload is 32 bytes and a command is 8, so three fit and a fourth
    needs its own frame. The engine applies them in order either way."""
    fake = _FakeSerial()
    client = XsynthClient(transport=fake)
    commands = [Command(OP_SET_ATTACK, value=i) for i in range(4)]
    client.send_commands(commands)

    assert bytes(fake.written) == (
        encode_commands(commands[:3]) + encode_commands(commands[3:])
    )


def test_an_envelope_with_every_stage_is_four_commands_in_two_frames():
    fake = _FakeSerial()
    client = XsynthClient(transport=fake)
    client.set_envelope(attack=0.01, decay=0.2, sustain=0.5, release=0.4)

    written = bytes(fake.written)
    assert written.count(b"\xaa\x55") == 2
