"""The allocator and the schedule, compiled for the host and driven from here.

`xsynth/sw/control.c` touches no register: it is handed the current sample
count and hands back the commands to push. That is what lets it be built with
the host's compiler and called through ctypes, which turns a timing bug from a
six-minute build and a board into a millisecond. `main.c` is the only part that
needs the hardware, and it has nothing left in it to get wrong.
"""

from __future__ import annotations

import ctypes
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from xsynth.firmware import protocol_header
from xsynth.protocol import (
    OP_CLEAR_SCHEDULE,
    OP_NOTE_OFF,
    OP_NOTE_ON,
    OP_RESET,
    OP_SCHEDULE_AT,
    OP_SET_ATTACK,
    OP_SET_AMP,
    OP_SET_FREQ,
    OP_SET_MASTER,
    OP_SET_WAVE,
    VOICE_ANY,
    Command,
)

SW_DIR = Path(__file__).resolve().parent.parent / "xsynth" / "sw"

# The window the firmware pushes within, and how many commands one call may
# hand back. Read from the header rather than repeated, so the test cannot
# drift from the thing it is testing.
CONTROL_H = (SW_DIR / "control.h").read_text()


def _define(name: str, text: str = CONTROL_H) -> int:
    match = re.search(rf"^#define {name}\s+(\S+)", text, re.M)
    assert match, f"{name} is not defined"
    return int(match.group(1), 0)


VOICE_COUNT = _define("VOICE_COUNT", protocol_header())
SCHEDULE_ENTRIES = _define("SCHEDULE_ENTRIES", protocol_header())
DISPATCH_WINDOW = _define("DISPATCH_WINDOW")
CONTROL_MAX_OUT = _define("CONTROL_MAX_OUT")


class ControlWord(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_uint), ("hi", ctypes.c_uint)]


class ControlVoice(ctypes.Structure):
    _fields_ = [
        ("step", ctypes.c_uint),
        ("state", ctypes.c_uint),
        ("stamp", ctypes.c_uint),
    ]


class ControlEvent(ctypes.Structure):
    _fields_ = [
        ("time", ctypes.c_uint),
        ("lo", ctypes.c_uint),
        ("hi", ctypes.c_uint),
    ]


class ControlState(ctypes.Structure):
    _fields_ = [
        ("voices", ControlVoice * VOICE_COUNT),
        ("events", ControlEvent * SCHEDULE_ENTRIES),
        ("head", ctypes.c_uint),
        ("count", ctypes.c_uint),
        ("tick", ctypes.c_uint),
        ("anchor", ctypes.c_uint),
        ("anchored", ctypes.c_uint),
        ("chain", ctypes.c_uint),
        ("chained", ctypes.c_uint),
    ]


IDLE, SOUNDING, RELEASING = 0, 1, 2


def note_on(hz_step: int, voice: int = VOICE_ANY, delay: int = 0) -> Command:
    return Command(OP_NOTE_ON, voice=voice, value=hz_step, delay=delay)


def note_off(hz_step: int, voice: int = VOICE_ANY, delay: int = 0) -> Command:
    return Command(OP_NOTE_OFF, voice=voice, value=hz_step, delay=delay)


class Control:
    """The firmware's logic, in this process."""

    def __init__(self, library: ctypes.CDLL):
        self.lib = library
        self.state = ControlState()
        self.out = (ControlWord * CONTROL_MAX_OUT)()
        self.lib.control_reset.argtypes = [ctypes.POINTER(ControlState)]
        self.lib.control_command.argtypes = [
            ctypes.POINTER(ControlState), ctypes.c_uint, ctypes.c_uint,
            ctypes.POINTER(ControlWord),
        ]
        self.lib.control_command.restype = ctypes.c_int
        self.lib.control_dispatch.argtypes = [
            ctypes.POINTER(ControlState), ctypes.c_uint,
            ctypes.POINTER(ControlWord),
        ]
        self.lib.control_dispatch.restype = ctypes.c_int
        self.reset()

    def reset(self) -> None:
        self.lib.control_reset(ctypes.byref(self.state))

    def _emit(self, count: int) -> list[Command]:
        return [
            Command.unpack(
                (self.out[i].lo | (self.out[i].hi << 32)).to_bytes(8, "little")
            )
            for i in range(count)
        ]

    def command(self, command: Command) -> list[Command]:
        word = command.word
        count = self.lib.control_command(
            ctypes.byref(self.state), word & 0xFFFF_FFFF, word >> 32,
            self.out,
        )
        return self._emit(count)

    def dispatch(self, now: int) -> list[Command]:
        count = self.lib.control_dispatch(ctypes.byref(self.state), now, self.out)
        return self._emit(count)

    @property
    def count(self) -> int:
        return self.state.count

    def state_of(self, voice: int) -> int:
        return self.state.voices[voice].state

    def step_of(self, voice: int) -> int:
        return self.state.voices[voice].step

    def states(self) -> list[int]:
        return [self.state.voices[i].state for i in range(VOICE_COUNT)]


@pytest.fixture(scope="session")
def control():
    """Compile control.c once for the whole session and wrap it."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "protocol.h").write_text(protocol_header())
        library = root / "control.so"
        result = subprocess.run(
            [
                shutil.which("cc"), "-shared", "-fPIC", "-O2",
                "-Wall", "-Wextra", "-Werror",
                "-I", str(root), "-I", str(SW_DIR),
                str(SW_DIR / "control.c"), "-o", str(library),
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        yield Control(ctypes.CDLL(str(library)))


# --- the allocator ---------------------------------------------------------


def test_a_note_with_no_voice_takes_the_first_free_one(control):
    control.reset()
    out = control.command(note_on(1000))
    assert [(c.opcode, c.voice, c.value) for c in out] == [(OP_NOTE_ON, 0, 1000)]


def test_successive_notes_fill_the_bank(control):
    control.reset()
    for i in range(VOICE_COUNT):
        out = control.command(note_on(100 + i))
        assert len(out) == 1
        assert out[0].voice == i
    assert control.states() == [SOUNDING] * VOICE_COUNT


def test_a_note_past_the_end_of_the_bank_steals_the_oldest(control):
    control.reset()
    for i in range(VOICE_COUNT):
        control.command(note_on(100 + i))
    out = control.command(note_on(999))
    assert out[0].voice == 0
    assert control.step_of(0) == 999
    assert control.step_of(1) == 101


def test_a_releasing_voice_is_preferred_to_a_sounding_one(control):
    control.reset()
    for i in range(VOICE_COUNT):
        control.command(note_on(100 + i))
    control.command(note_off(105))
    assert control.state_of(5) == RELEASING

    out = control.command(note_on(999))
    assert out[0].voice == 5
    assert control.state_of(5) == SOUNDING


def test_the_longest_release_goes_first(control):
    control.reset()
    for i in range(VOICE_COUNT):
        control.command(note_on(100 + i))
    control.command(note_off(106))  # voice 6, released first
    control.command(note_off(107))  # voice 7, released second

    assert control.command(note_on(999))[0].voice == 6
    assert control.command(note_on(998))[0].voice == 7


def test_a_named_voice_is_taken_as_it_is_and_remembered(control):
    control.reset()
    out = control.command(note_on(440, voice=3))
    assert out[0].voice == 3
    assert control.state_of(3) == SOUNDING
    assert control.step_of(3) == 440


def test_a_named_voice_that_does_not_exist_is_dropped(control):
    control.reset()
    assert control.command(note_on(440, voice=VOICE_COUNT)) == []
    assert control.states() == [IDLE] * VOICE_COUNT


def test_a_note_off_finds_the_voice_by_its_step(control):
    control.reset()
    control.command(note_on(440, voice=5))
    out = control.command(note_off(440))
    assert out[0].voice == 5
    assert control.state_of(5) == RELEASING


def test_a_note_off_for_a_note_that_is_not_playing_goes_nowhere(control):
    control.reset()
    assert control.command(note_off(440)) == []


def test_a_repeated_note_off_does_not_move_the_voice_up_the_queue(control):
    control.reset()
    for i in range(VOICE_COUNT):
        control.command(note_on(100 + i))
    control.command(note_off(103))
    first = control.state.voices[3].stamp
    control.command(note_off(103))
    assert control.state.voices[3].stamp == first


def test_the_oldest_of_two_notes_at_the_same_pitch_goes_first(control):
    control.reset()
    control.command(note_on(440, voice=0))
    control.command(note_on(440, voice=1))
    assert control.command(note_off(440))[0].voice == 0
    assert control.command(note_off(440))[0].voice == 1


def test_a_note_keeps_the_step_it_started_with(control):
    control.reset()
    control.command(note_on(440, voice=2))
    control.command(Command(OP_SET_FREQ, voice=2, value=660))
    assert control.command(note_off(440))[0].voice == 2


def test_naming_no_voice_on_anything_but_a_note_is_a_broadcast(control):
    control.reset()
    out = control.command(Command(OP_SET_WAVE, voice=VOICE_ANY, value=2, delay=7))
    assert [c.voice for c in out] == list(range(VOICE_COUNT))
    assert [c.value for c in out] == [2] * VOICE_COUNT
    assert [c.delay for c in out] == [7] + [0] * (VOICE_COUNT - 1)


def test_a_global_opcode_is_passed_through_however_the_voice_field_reads(control):
    control.reset()
    for voice in (VOICE_ANY, 0, 3):
        out = control.command(Command(OP_SET_ATTACK, voice=voice, value=1234))
        assert [(c.opcode, c.voice, c.value) for c in out] == [
            (OP_SET_ATTACK, voice, 1234)
        ]


def test_reset_empties_the_bank_and_still_reaches_the_engine(control):
    control.reset()
    for i in range(VOICE_COUNT):
        control.command(note_on(100 + i))
    out = control.command(Command(OP_RESET))
    assert [c.opcode for c in out] == [OP_RESET]
    assert control.states() == [IDLE] * VOICE_COUNT


# --- the schedule ----------------------------------------------------------


def test_an_anchor_is_the_firmwares_business_and_goes_no_further(control):
    control.reset()
    assert control.command(Command(OP_SCHEDULE_AT, value=100_000)) == []


def test_a_command_after_an_anchor_is_held_not_pushed(control):
    control.reset()
    control.command(Command(OP_SCHEDULE_AT, value=100_000))
    assert control.command(note_on(440)) == []
    assert control.count == 1


def test_delays_accumulate_from_the_anchor(control):
    control.reset()
    control.command(Command(OP_SCHEDULE_AT, value=1_000_000))
    control.command(note_on(440, delay=0))
    control.command(note_on(550, delay=4800))
    control.command(note_on(660, delay=4800))
    times = [
        control.state.events[(control.state.head + i) & (SCHEDULE_ENTRIES - 1)].time
        for i in range(control.count)
    ]
    assert times == [1_000_000, 1_004_800, 1_009_600]


def test_an_event_is_dispatched_once_its_time_is_within_the_window(control):
    control.reset()
    control.command(Command(OP_SCHEDULE_AT, value=10_000))
    control.command(note_on(440))

    assert control.dispatch(10_000 - DISPATCH_WINDOW - 1) == []
    out = control.dispatch(10_000 - DISPATCH_WINDOW)
    assert [c.opcode for c in out] == [OP_NOTE_ON]
    assert out[0].delay == DISPATCH_WINDOW


def test_a_dispatched_event_lands_on_its_time(control):
    control.reset()
    control.command(Command(OP_SCHEDULE_AT, value=50_000))
    control.command(note_on(440))
    out = control.dispatch(49_990)
    assert out[0].delay == 10


def test_events_dispatched_together_keep_their_spacing_exactly(control):
    """Within the window they go in one batch, and the engine counts between."""
    control.reset()
    control.command(Command(OP_SCHEDULE_AT, value=50_000))
    control.command(note_on(440, delay=0))
    control.command(note_on(550, delay=20))
    control.command(note_on(660, delay=20))

    out = control.dispatch(49_990)
    assert [c.delay for c in out] == [10, 20, 20]
    assert [c.value for c in out] == [440, 550, 660]


def test_an_event_beyond_the_window_still_lands_on_its_time(control):
    """Past the window the chain has drained, so the delay is measured from
    `now` instead -- and `now` plus that delay is still the event's time."""
    control.reset()
    control.command(Command(OP_SCHEDULE_AT, value=50_000))
    control.command(note_on(440, delay=0))
    control.command(note_on(550, delay=100))
    control.command(note_on(660, delay=100))

    landings = []
    for now in (49_990, 50_036, 50_136):
        for command in control.dispatch(now):
            landings.append(now + command.delay)
    assert landings == [50_000, 50_100, 50_200]


def test_an_event_whose_time_has_gone_by_is_applied_at_once(control):
    control.reset()
    control.command(Command(OP_SCHEDULE_AT, value=50_000))
    control.command(note_on(440))
    out = control.dispatch(60_000)
    assert out[0].delay == 0


def test_a_long_gap_is_a_new_anchor(control):
    """A single delay is 16 bits, so 1.365 seconds is as far as one reaches.
    A host with a longer silence anchors again rather than being stuck."""
    control.reset()
    control.command(Command(OP_SCHEDULE_AT, value=0))
    control.command(note_on(440, delay=0))
    control.command(Command(OP_SCHEDULE_AT, value=96_000))
    control.command(note_on(550, delay=0))

    first = control.dispatch(0)
    assert first[0].value == 440
    second = control.dispatch(96_000 - DISPATCH_WINDOW)
    assert second[0].value == 550
    assert second[0].delay == DISPATCH_WINDOW


def test_a_second_anchor_re_anchors_the_accumulator(control):
    control.reset()
    control.command(Command(OP_SCHEDULE_AT, value=1_000))
    control.command(note_on(440, delay=10))
    control.command(Command(OP_SCHEDULE_AT, value=9_000_000))
    control.command(note_on(550, delay=10))

    times = [
        control.state.events[(control.state.head + i) & (SCHEDULE_ENTRIES - 1)].time
        for i in range(control.count)
    ]
    assert times == [1_010, 9_000_010]


def test_an_event_out_of_order_is_refused(control):
    """Only a fresh anchor can ask for an earlier time, and the ring is drained
    from the front, so such an event would never be reached in time."""
    control.reset()
    control.command(Command(OP_SCHEDULE_AT, value=5_000))
    control.command(note_on(440, delay=100))
    control.command(Command(OP_SCHEDULE_AT, value=1_000))
    control.command(note_on(550, delay=0))
    assert control.count == 1


def test_two_events_at_the_same_sample_are_both_kept(control):
    control.reset()
    control.command(Command(OP_SCHEDULE_AT, value=5_000))
    control.command(note_on(440, delay=100))
    control.command(note_on(550, delay=0))
    assert control.count == 2


def test_the_ring_refuses_rather_than_wraps(control):
    control.reset()
    control.command(Command(OP_SCHEDULE_AT, value=0))
    for i in range(SCHEDULE_ENTRIES):
        control.command(note_on(100 + i, delay=1))
    assert control.count == SCHEDULE_ENTRIES
    control.command(note_on(999, delay=1))
    assert control.count == SCHEDULE_ENTRIES


def test_clearing_the_schedule_leaves_the_notes_alone(control):
    control.reset()
    control.command(note_on(440, voice=0))
    control.command(Command(OP_SCHEDULE_AT, value=10_000))
    control.command(note_on(550))

    assert control.command(Command(OP_CLEAR_SCHEDULE)) == []
    assert control.count == 0
    assert control.state_of(0) == SOUNDING


def test_clearing_the_schedule_goes_back_to_immediate(control):
    """Otherwise the anchor would be sticky for the life of the firmware and a
    live note would land wherever the last scheduled one did."""
    control.reset()
    control.command(Command(OP_SCHEDULE_AT, value=10_000))
    control.command(Command(OP_CLEAR_SCHEDULE))

    out = control.command(note_on(440))
    assert [c.opcode for c in out] == [OP_NOTE_ON]
    assert control.count == 0


def test_reset_clears_the_schedule_too(control):
    control.reset()
    control.command(Command(OP_SCHEDULE_AT, value=10_000))
    control.command(note_on(550))
    control.command(Command(OP_RESET))
    assert control.count == 0
    assert control.dispatch(10_000) == []


def test_a_scheduled_note_takes_its_voice_when_it_is_dispatched(control):
    control.reset()
    control.command(Command(OP_SCHEDULE_AT, value=1_000))
    control.command(note_on(440))
    assert control.states() == [IDLE] * VOICE_COUNT

    out = control.dispatch(1_000)
    assert out[0].voice == 0
    assert control.state_of(0) == SOUNDING


def test_times_near_the_wrap_are_ordered_correctly(control):
    """The counter is 32 bits and free running, so `now` will wrap."""
    control.reset()
    start = 0xFFFF_FFE0
    control.command(Command(OP_SCHEDULE_AT, value=start))
    control.command(note_on(440, delay=0))
    control.command(note_on(550, delay=50))

    out = control.dispatch(start)
    assert [c.value for c in out] == [440, 550]
    assert [(start + c.delay) & 0xFFFF_FFFF for c in out] == [
        0xFFFF_FFE0, 0x0000_0012
    ]


def test_the_stealing_order_survives_the_tick_counter_wrapping(control):
    """`tick` is compared modularly, so it must not be read as a magnitude."""
    control.reset()
    control.state.tick = 0xFFFF_FFF0
    for i in range(VOICE_COUNT):
        control.command(note_on(100 + i))
    assert control.command(note_on(999))[0].voice == 0
    assert control.command(note_on(998))[0].voice == 1


def test_dispatch_stops_at_the_output_limit_and_resumes(control):
    """One call hands back a bounded batch; the rest wait their turn rather
    than being lost or overrunning the caller's buffer."""
    control.reset()
    control.command(Command(OP_SCHEDULE_AT, value=0))
    for i in range(CONTROL_MAX_OUT + 4):
        control.command(note_on(100 + i, delay=1))

    first = control.dispatch(1_000)
    assert 0 < len(first) <= CONTROL_MAX_OUT
    assert control.count == CONTROL_MAX_OUT + 4 - len(first)

    rest = []
    while control.count:
        rest += control.dispatch(1_000)
    assert [c.value for c in first + rest] == [
        100 + i for i in range(CONTROL_MAX_OUT + 4)
    ]
