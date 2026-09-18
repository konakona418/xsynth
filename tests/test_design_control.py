"""The control design end to end in simulation: UART in, voice parameters and audio out.

The harness and the scenario runner live in :mod:`xsynth.sim.designs.control`; this file
is the assertions. The core runs at a deliberately fast baud so a whole frame
fits in a few thousand cycles; the wire format is identical to the board's.
"""

import pytest

from xsynth.hdl.audio import PEAK, phase_step
from xsynth.hdl.voice import WAVES
from xsynth.protocol import (
    FLAG_LOCKED,
    OP_NOTE_OFF,
    OP_NOTE_ON,
    OP_SET_AMP,
    OP_SET_FREQ,
    OP_SET_MASTER,
    OP_SET_WAVE,
    PKT_ERROR,
    PKT_PING,
    PKT_PONG,
    PKT_STATUS,
    PKT_STATUS_REPLY,
    STATUS_ARGUMENTS,
    Command,
    decode_response,
    encode_commands,
    encode_frame,
)
from xsynth.sim.designs.audio import measure_frequency
from xsynth.sim.designs.control import TEST_SAMPLE_RATE, run_scenario


def test_ping_is_answered_with_a_pong():
    result = run_scenario([encode_frame(bytes([PKT_PING]))])
    assert result.responses == [bytes([PKT_PONG])]


def test_status_reports_the_version_and_the_lock_bit():
    result = run_scenario([encode_frame(bytes([PKT_STATUS]))], locked=True)
    assert len(result.responses) == 1
    packet, arguments = decode_response(result.responses[0])
    assert packet == PKT_STATUS_REPLY
    assert len(arguments) == STATUS_ARGUMENTS
    assert arguments[1] & (1 << FLAG_LOCKED)
    assert arguments[2] == 0  # the FIFO is drained


def test_commands_reach_the_voice():
    step = phase_step(440.0, TEST_SAMPLE_RATE)
    saw = WAVES.index("saw")
    result = run_scenario([encode_commands([
        Command(OP_SET_WAVE, value=saw),
        Command(OP_SET_FREQ, value=step),
        Command(OP_NOTE_ON, value=step),
    ])])

    assert result.wave == saw
    assert result.step == step
    assert result.amp == PEAK


def test_note_off_silences_the_voice():
    step = phase_step(440.0, TEST_SAMPLE_RATE)
    result = run_scenario([encode_commands([
        Command(OP_NOTE_ON, value=step),
        Command(OP_NOTE_OFF, delay=4),
    ])])
    assert result.amp == 0


def test_amplitude_is_clamped_to_the_sample_range():
    result = run_scenario([encode_commands([
        Command(OP_SET_AMP, value=0xFFFFFFFF),
    ])])
    assert result.level == PEAK


def test_a_corrupted_frame_does_not_disturb_the_next_one():
    good = encode_commands([Command(OP_SET_FREQ, value=1234)])
    bad = bytearray(encode_frame(bytes([PKT_STATUS])))
    bad[3] ^= 0xFF
    result = run_scenario([bytes(bad), good])

    assert result.step == 1234
    # The corrupted frame never reached the handler, so no response came back.
    assert result.responses == []


def test_an_unknown_packet_is_reported_and_survives():
    result = run_scenario([encode_frame(bytes([0x42]))])
    assert len(result.responses) == 1
    packet, arguments = decode_response(result.responses[0])
    assert packet == PKT_ERROR
    assert arguments[0] == 3  # ERR_UNKNOWN_PACKET


def test_the_voice_plays_the_frequency_it_was_given():
    tone = 10_000.0
    step = phase_step(tone, TEST_SAMPLE_RATE)
    result = run_scenario(
        [encode_commands([
            Command(OP_SET_WAVE, value=WAVES.index("sine")),
            Command(OP_NOTE_ON, value=step),
        ])],
        cycles=25_000,
        samples=True,
    )

    assert result.amp == PEAK
    measured = measure_frequency(result.audio, TEST_SAMPLE_RATE)
    assert abs(measured - tone) < 1.0, f"measured {measured:.1f} Hz"


def test_a_zero_step_never_emits_a_dc_offset():
    # A saw at phase 0 is -full scale, so a stopped voice must be forced silent
    # rather than left holding that value.
    result = run_scenario(
        [encode_commands([
            Command(OP_SET_WAVE, value=WAVES.index("saw")),
            Command(OP_SET_AMP, value=PEAK),
        ])],
        cycles=20_000,
        samples=True,
    )
    assert set(result.audio) == {0}


def test_a_command_can_address_any_voice():
    """The voice field has to survive UART, framing, the FIFO and the scheduler.

    Everything between the wire and the engine treats a command as an opaque
    64-bit word, so a voice that arrives as zero would leave the whole bank
    unreachable and only a build would show it.
    """
    step = phase_step(440.0, TEST_SAMPLE_RATE)
    result = run_scenario([encode_commands([
        Command(OP_NOTE_ON, voice=3, value=step),
        Command(OP_NOTE_ON, voice=6, value=step),
    ])])
    assert result.envelopes == [0, 0, 0, PEAK, 0, 0, PEAK, 0]


def test_a_note_off_reaches_the_voice_that_is_playing():
    step = phase_step(440.0, TEST_SAMPLE_RATE)
    result = run_scenario([encode_commands([
        Command(OP_NOTE_ON, voice=5, value=step),
        Command(OP_NOTE_ON, voice=2, value=step),
        Command(OP_NOTE_OFF, voice=5, delay=4),
    ])])
    assert result.envelopes == [0, 0, PEAK, 0, 0, 0, 0, 0]


def _rms(samples) -> float:
    return (sum(value * value for value in samples) / len(samples)) ** 0.5


def _from_the_first_sample(samples) -> list[int]:
    """Drop the silence before the note starts; the frame has to cross the UART."""
    for index, value in enumerate(samples):
        if value:
            return samples[index:]
    raise AssertionError("the voice never made a sound")


def test_the_master_scales_the_output():
    step = phase_step(10_000.0, TEST_SAMPLE_RATE)

    def frames(master):
        return [encode_commands([
            Command(OP_SET_MASTER, value=master),
            Command(OP_NOTE_ON, value=step),
        ])]

    loud = _from_the_first_sample(
        run_scenario(frames(PEAK), cycles=25_000, samples=True).audio)
    quiet = _from_the_first_sample(
        run_scenario(frames(PEAK // 4), cycles=25_000, samples=True).audio)

    # RMS rather than peak: the phase steps through the table faster than once
    # per entry, so where the samples land relative to the sine's crest is an
    # artefact of the frequency, not of the gain.
    loud_rms = _rms(loud)
    assert loud_rms == pytest.approx(PEAK / 2 ** 0.5, rel=0.02)
    assert _rms(quiet) == pytest.approx(loud_rms / 4, rel=0.02)
