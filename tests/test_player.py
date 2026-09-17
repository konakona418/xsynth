"""The player: pacing a score into a ring that drops what does not fit.

The batching is a pure function of the engine's sample count, so these drive it
with made-up counts and no board, and check the two things that would otherwise
show up only as a missing note on hardware: the ring is never asked for more
than it holds, and every event lands on the sample the score named.

`_Firmware` is the other side of the protocol -- an accumulator and a ring --
so the commands the player sends are decoded exactly the way the real firmware
decodes them, and the samples that come back are what the engine would play.
"""

import pytest

from xsynth.host.client import SAMPLE_RATE
from xsynth.host.player import Player, apply_patch, play
from xsynth.host.score import Note, Patch, Score
from xsynth.protocol import (
    MAX_DELAY,
    OP_NOTE_OFF,
    OP_NOTE_ON,
    OP_SCHEDULE_AT,
    SCHEDULE_ENTRIES,
    VOICE_ANY,
)


def _score(*notes, **patch):
    return Score(Patch(**patch), tuple(Note(*note) for note in notes))


class _Firmware:
    """The firmware's side: delays accumulate into times, and the ring drops
    what does not fit."""

    def __init__(self, capacity=SCHEDULE_ENTRIES):
        self.capacity = capacity
        self.acc = 0
        self.held = []
        self.scheduled = []
        self.dropped = 0

    def take(self, command):
        if command.opcode == OP_SCHEDULE_AT:
            self.acc = command.value
            return
        self.acc += command.delay
        self.scheduled.append(self.acc)
        if len(self.held) >= self.capacity:
            self.dropped += 1
            return
        self.held.append(self.acc)

    def advance(self, now):
        """What the dispatch pass leaves behind."""
        self.held = [sample for sample in self.held if sample - now > 64]


class _FakeClient:
    """Records what it is sent, and answers with a sample count we control."""

    def __init__(self, now=0):
        self.sample = now
        self.commands = []
        self.patches = []

    def now(self, timeout=None):
        return self.sample

    def send_commands(self, commands):
        self.commands.extend(commands)

    def reset(self, *, delay=0):
        self.patches.append("reset")

    def set_wave(self, wave, **kwargs):
        self.patches.append(("wave", wave))

    def set_envelope(self, **kwargs):
        self.patches.append(("envelope", kwargs))

    def set_master(self, fraction, **kwargs):
        self.patches.append(("master", fraction))


def _drive(player, firmware, *, step=2400, limit=100_000):
    """Run the player's loop against the model, without a board or a clock."""
    now = 0
    rounds = 0
    while not player.done and rounds < limit:
        for command in player.next_batch(now):
            firmware.take(command)
        firmware.advance(now)
        now += step
        rounds += 1
    return now


def test_a_note_becomes_a_note_on_and_a_note_off():
    player = Player(_score((0.0, 69, 1.0)))
    assert [event.command.opcode for event in player.events] == [
        OP_NOTE_ON, OP_NOTE_OFF,
    ]
    assert [event.sample for event in player.events] == [0, SAMPLE_RATE]


def test_releases_come_before_note_ons_at_the_same_sample():
    """A re-articulation should let the old note go before the new one asks
    the allocator for a voice."""
    player = Player(_score((0.0, 69, 1.0), (1.0, 69, 1.0)))
    at_the_join = [event.command.opcode for event in player.events
                   if event.sample == SAMPLE_RATE]
    assert at_the_join == [OP_NOTE_OFF, OP_NOTE_ON]


def test_a_note_off_carries_the_step_its_note_on_did():
    """The step is the note's identity, so this is what the firmware matches
    a note-off against -- and the host never learns the voice."""
    player = Player(_score((0.0, 69, 0.5)))
    started, stopped = (event.command for event in player.events)
    assert started.value == stopped.value
    assert started.voice == stopped.voice == VOICE_ANY


def test_the_piece_is_placed_by_one_anchor_a_lead_in_past_the_sample_count():
    """One anchor, and then gaps: the engine counts samples from there, so the
    whole piece is placed by that single number."""
    client = _FakeClient(now=1000)
    play(client, _score((0.0, 69, 0.1)), lead_in=0.5,
         sleep=lambda seconds: None)
    assert client.commands[0].opcode == OP_SCHEDULE_AT
    assert client.commands[0].value == 1000 + round(0.5 * SAMPLE_RATE)
    assert client.commands[1].delay == 0


def test_delays_are_the_gaps_between_events():
    player = Player(_score((0.0, 69, 0.5), (1.0, 71, 0.5)))
    batch = player.next_batch(0)
    assert [command.delay for command in batch] == [0, 24000, 24000, 24000]


def test_the_delays_reconstruct_the_times_the_score_named():
    player = Player(_score((0.0, 69, 0.5), (1.0, 71, 0.25)))
    firmware = _Firmware()
    _drive(player, firmware)
    assert firmware.scheduled == [0, 24000, 48000, 60000]


def test_a_gap_too_long_for_the_delay_field_gets_a_fresh_anchor():
    """A delay is sixteen bits, which is 1.365 seconds, so a longer rest is
    another anchor rather than an unrepresentable number."""
    rest = 2.0
    player = Player(_score((0.0, 69, 0.1), (rest, 71, 0.1)),
                    lookahead=10.0)
    batch = player.next_batch(0)

    anchors = [command for command in batch if command.opcode == OP_SCHEDULE_AT]
    assert [anchor.value for anchor in anchors] == [round(rest * SAMPLE_RATE)]
    assert all(command.delay <= MAX_DELAY for command in batch)

    firmware = _Firmware()
    for command in batch:
        firmware.take(command)
    assert firmware.scheduled == [
        0, 4800, round(rest * SAMPLE_RATE), round((rest + 0.1) * SAMPLE_RATE),
    ]


def test_a_long_score_never_overflows_the_ring():
    notes = [(index * 0.25, 60 + index % 12, 0.2) for index in range(200)]
    player = Player(_score(*notes))
    firmware = _Firmware()
    _drive(player, firmware)
    assert firmware.dropped == 0
    assert len(firmware.scheduled) == 2 * len(notes)


def test_the_ring_capacity_is_what_stops_a_batch():
    """The lookahead bounds what is queued in practice; the capacity is what
    makes it correct, because past it the firmware drops events silently."""
    notes = [(index * 0.1, 60, 0.05) for index in range(50)]
    player = Player(_score(*notes), capacity=16, margin=2, lookahead=3600.0)
    firmware = _Firmware(capacity=16)
    _drive(player, firmware)
    assert firmware.dropped == 0
    assert len(firmware.scheduled) == 2 * len(notes)


def test_the_batch_stops_at_the_lookahead_horizon():
    """Queuing the whole piece at once would be legal for a short score and
    wrong for a long one: the host should still be involved while it plays."""
    player = Player(_score((0.0, 60, 0.1), (30.0, 62, 0.1)), lookahead=2.0)
    batch = player.next_batch(0)
    assert [command.value for command in batch if command.opcode == OP_NOTE_ON] \
        == [player.events[0].command.value]


def test_an_empty_score_is_done_immediately():
    player = Player(_score())
    assert player.done
    assert player.ends_at == 0
    assert player.next_batch(0) == []


def test_play_resets_the_board_then_sets_the_sound():
    client = _FakeClient()
    play(client, _score((0.0, 69, 0.1), wave="saw", master=0.5),
         sleep=lambda seconds: None)
    assert client.patches == ["reset", ("wave", "saw"), ("master", 0.5)]


def test_play_anchors_before_it_sends_any_note():
    client = _FakeClient()
    play(client, _score((0.0, 69, 0.1)), sleep=lambda seconds: None)
    assert client.commands[0].opcode == OP_SCHEDULE_AT


def test_a_patch_only_touches_what_the_score_mentioned():
    client = _FakeClient()
    apply_patch(client, Patch(attack=0.01, sustain=0.5))
    assert client.patches == [("envelope", {"attack": 0.01, "sustain": 0.5})]


def test_an_untouched_board_gets_no_commands_at_all():
    client = _FakeClient()
    apply_patch(client, Patch())
    assert client.patches == []
