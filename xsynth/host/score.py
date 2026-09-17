"""A score: the notes to play and the sound to play them with, in a text file.

Deliberately not MIDI. What this is for is driving the board and then checking
what came back, and for that a file a person can write, read and diff beats a
binary format that needs a parser and a library. A score is:

    ; a comment, and blank lines, are ignored
    wave saw
    attack 0.005
    decay 0.1
    sustain 0.8
    release 0.02
    master 0.5

    0.0   C4   0.5
    0.0   E4   0.5
    0.0   G4   0.5
    1.0   D4   1.0

One note to a line: when it starts, what pitch, how long it lasts. Times are
seconds, so what is written is what a recording is measured against, with no
tempo in between. A chord is several lines sharing a time.

The header lines are the player's own parameter names, so a score reads like
the calls it turns into. `#` is not the comment character because `A#3` is a
note.

There is no velocity column, and that is not an oversight: `SET_AMP` addresses
a voice, and with the firmware allocating voices a host writing a score cannot
know which one a note will land on. Dynamics need a protocol change, not a
column.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from xsynth.protocol import WAVES

# Comments start here. Not '#': A#3 is a note.
COMMENT = ";"

# C4 is MIDI 60, so A4 is 69 and 440 Hz, which is how the rest of the project
# counts and what `_note_to_hz` in the CLI already assumes.
SEMITONE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
A4_PITCH = 69
A4_HZ = 440.0

# The header keys, in the order they are documented.
HEADER = ("wave", "attack", "decay", "sustain", "release", "master")

# The header keys that are a fraction of full scale rather than a duration.
FRACTIONS = ("sustain", "master")


class ScoreError(ValueError):
    """A score file that cannot be read as one."""


@dataclass(frozen=True)
class Note:
    """When a note starts, what pitch it is, and how long it lasts."""

    time: float
    pitch: int
    duration: float

    @property
    def hz(self) -> float:
        return A4_HZ * 2 ** ((self.pitch - A4_PITCH) / 12)

    @property
    def ends(self) -> float:
        return self.time + self.duration


@dataclass(frozen=True)
class Patch:
    """The sound the notes are played with.

    ``None`` means the score said nothing about that parameter, so whatever is
    on the board stays.
    """

    wave: str | None = None
    attack: float | None = None
    decay: float | None = None
    sustain: float | None = None
    release: float | None = None
    master: float | None = None


@dataclass(frozen=True)
class Score:
    patch: Patch
    notes: tuple[Note, ...]

    @property
    def duration(self) -> float:
        """When the last note stops, in seconds from the start."""
        return max((note.ends for note in self.notes), default=0.0)


def parse_pitch(text: str, *, line: int | None = None) -> int:
    """A note name (`C4`, `A#3`, `Bb3`) or a plain MIDI number."""
    where = f"line {line}: " if line is not None else ""
    if text.lstrip("-").isdigit():
        pitch = int(text)
    else:
        letter = text[0].upper()
        if letter not in SEMITONE:
            raise ScoreError(f"{where}unknown pitch {text!r}; try C4, A#3 or 69")
        rest = text[1:]
        accidental = 0
        if rest[:1] in ("#", "b"):
            accidental = 1 if rest[0] == "#" else -1
            rest = rest[1:]
        if not rest.lstrip("-").isdigit():
            raise ScoreError(f"{where}unknown pitch {text!r}; try C4, A#3 or 69")
        pitch = (int(rest) + 1) * 12 + SEMITONE[letter] + accidental
    if not 0 <= pitch <= 127:
        raise ScoreError(f"{where}pitch {text!r} is outside MIDI's 0..127")
    return pitch


def _seconds(text: str, what: str, line: int) -> float:
    try:
        value = float(text)
    except ValueError:
        raise ScoreError(
            f"line {line}: {what} is in seconds, and {text!r} is not a number"
        ) from None
    if value < 0:
        raise ScoreError(f"line {line}: {what} cannot be negative")
    return value


def _setting(key: str, value: str, line: int):
    if key == "wave":
        if value not in WAVES:
            raise ScoreError(
                f"line {line}: unknown waveform {value!r}; "
                f"known: {', '.join(WAVES)}"
            )
        return value
    number = _seconds(value, key, line)
    if key in FRACTIONS and number > 1:
        raise ScoreError(f"line {line}: {key} is a fraction, so at most 1")
    return number


def parse_score(text: str) -> Score:
    """Read a score. Raises :class:`ScoreError` with the line number."""
    settings: dict[str, object] = {}
    notes: list[Note] = []
    for line, raw in enumerate(text.splitlines(), start=1):
        body = raw.split(COMMENT, 1)[0].strip()
        if not body:
            continue
        fields = body.split()
        if len(fields) == 2 and fields[0] in HEADER:
            settings[fields[0]] = _setting(fields[0], fields[1], line)
        elif len(fields) == 3:
            notes.append(Note(
                _seconds(fields[0], "the start time", line),
                parse_pitch(fields[1], line=line),
                _seconds(fields[2], "the duration", line),
            ))
        else:
            raise ScoreError(
                f"line {line}: {body!r} is neither a setting "
                f"({' '.join(HEADER)}) nor a note, which is "
                f"'time pitch duration' with no velocity column"
            )
    notes.sort(key=lambda note: (note.time, note.pitch))
    return Score(Patch(**settings), tuple(notes))


def load_score(path: str | Path) -> Score:
    return parse_score(Path(path).read_text())
