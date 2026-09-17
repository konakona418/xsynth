"""Play a score: stream it into the firmware's schedule and let the board keep time.

The firmware holds :data:`~xsynth.protocol.SCHEDULE_ENTRIES` scheduled events
and drops anything past that, silently, so a host cannot fill the ring in one go
and walk away. It has to pace itself against how much is still outstanding,
which is why the capacity lives in :mod:`xsynth.protocol` where both sides read
it rather than being a number the firmware knows alone.

Times in a score are absolute seconds; the engine counts absolute samples, so
one anchor at the start places the whole piece. A command's `delay` is relative
to the command before it and only sixteen bits wide -- 1.365 seconds -- so the
player carries an accumulator mirroring the firmware's and sends the gap between
events, re-anchoring when a gap is too long for the field. Rests longer than a
second are ordinary in music, so that path is not exotic.

The batching is a pure function of the engine's sample count: hand
:class:`Player` a count and it hands back the commands to send, or nothing when
the ring is still full. That is what the tests drive. :func:`play` is the loop
around it that talks to a board.
"""

from __future__ import annotations

import time
from bisect import bisect_right
from dataclasses import dataclass, replace

from xsynth.host.client import SAMPLE_RATE, XsynthClient
from xsynth.host.score import Patch, Score
from xsynth.protocol import (
    MAX_DELAY,
    OP_NOTE_OFF,
    OP_NOTE_ON,
    OP_SCHEDULE_AT,
    SCHEDULE_ENTRIES,
    VOICE_ANY,
    Command,
)

# How far ahead of the engine's sample count to keep the ring filled. Well
# inside the ring for any sane note density, and short enough that the host is
# still involved while the music plays.
LOOKAHEAD_SECONDS = 2.0

# How far ahead of now the piece starts, so its first events are queued before
# their time rather than after it.
LEAD_IN_SECONDS = 0.5

# Ring entries left unused. The host's count of what is outstanding is already
# an over-estimate, but the cost of being wrong is a dropped note, and the cost
# of the margin is nothing.
MARGIN = 16

# How long to wait before asking the engine again when there is nothing to send.
REFILL_SECONDS = 0.05


@dataclass(frozen=True)
class Event:
    """One command, at one absolute 48 kHz sample."""

    sample: int
    command: Command


def events(score: Score, origin: int = 0) -> list[Event]:
    """The score as commands at absolute samples, in the order they are sent.

    ``origin`` is the sample the piece starts on: score time zero lands there.
    A note is a note-on and a note-off. Events sharing a time are ordered with
    the releases first: at a re-articulation the old note should let go before
    the new one asks for a voice. Both go to VOICE_ANY -- the note is identified
    by its step, which is what the firmware matches a note-off against, so a
    host never needs to know which voice it was given.
    """
    built = []
    for note in score.notes:
        step = XsynthClient.step_for(note.hz)
        for offset, opcode in (
            (note.time, OP_NOTE_ON),
            (note.ends, OP_NOTE_OFF),
        ):
            built.append(Event(
                origin + round(offset * SAMPLE_RATE),
                Command(opcode, voice=VOICE_ANY, value=step),
            ))
    built.sort(key=lambda event: (event.sample,
                                  event.command.opcode != OP_NOTE_OFF))
    return built


class Player:
    """A score, paced into the firmware's ring.

    Holds the accumulator that mirrors the firmware's, so the delays it sends
    place every event on the sample the score asked for.
    """

    def __init__(self, score: Score, *, origin: int = 0,
                 capacity: int = SCHEDULE_ENTRIES, margin: int = MARGIN,
                 lookahead: float = LOOKAHEAD_SECONDS):
        self.score = score
        self.origin = origin
        self.events = events(score, origin)
        self.samples = [event.sample for event in self.events]
        self.limit = capacity - margin
        self.horizon = round(lookahead * SAMPLE_RATE)
        self.sent = 0
        self.acc = origin

    @property
    def total(self) -> int:
        return len(self.events)

    @property
    def done(self) -> bool:
        return self.sent >= self.total

    @property
    def ends_at(self) -> int:
        """The sample the last event lands on, release tail included."""
        tail = self.score.patch.release or 0.0
        return self.samples[-1] + round(tail * SAMPLE_RATE) if self.samples else 0

    def outstanding(self, now: int) -> int:
        """How many sent events the engine has not reached yet.

        The firmware dispatches an event once it is within a few dozen samples,
        so this counts some events as still queued after they have played. That
        errs towards sending less, which is the safe direction.
        """
        return self.sent - bisect_right(self.samples, now)

    def anchor(self) -> Command:
        """The one anchor that places the whole piece.

        It resets the firmware's accumulator to the sample the piece starts on,
        so every delay after it is a gap between two events rather than a
        distance from an arbitrary origin.
        """
        return Command(OP_SCHEDULE_AT, value=self.origin)

    def next_batch(self, now: int) -> list[Command]:
        """The commands to send now, given the engine's sample count.

        Empty when the ring is full or when the next event is past the lookahead
        window, either of which means "ask again later".
        """
        room = self.limit - self.outstanding(now)
        horizon = now + self.horizon
        batch: list[Command] = []
        while self.sent < self.total and len(batch) < room:
            event = self.events[self.sent]
            if event.sample > horizon:
                break
            if event.sample - self.acc > MAX_DELAY:
                self.acc = event.sample
                batch.append(Command(OP_SCHEDULE_AT, value=self.acc))
            batch.append(replace(event.command, delay=event.sample - self.acc))
            self.acc = event.sample
            self.sent += 1
        return batch

    def play(self, client: XsynthClient, *, sleep=time.sleep,
             progress=None) -> None:
        """Send the score and wait for the engine to finish playing it."""
        client.send_commands([self.anchor()])
        while not self.done:
            batch = self.next_batch(client.now())
            if batch:
                client.send_commands(batch)
                if progress is not None:
                    progress(self.sent, self.total)
            else:
                sleep(REFILL_SECONDS)
        remaining = self.ends_at - client.now()
        if remaining > 0:
            sleep(remaining / SAMPLE_RATE)


def apply_patch(client: XsynthClient, patch: Patch) -> None:
    """Put a score's sound on the board, leaving alone what it did not mention."""
    if patch.wave is not None:
        client.set_wave(patch.wave)
    stages = {
        name: getattr(patch, name)
        for name in ("attack", "decay", "sustain", "release")
        if getattr(patch, name) is not None
    }
    if stages:
        client.set_envelope(**stages)
    if patch.master is not None:
        client.set_master(patch.master)


def play(client: XsynthClient, score: Score, *, lead_in: float = LEAD_IN_SECONDS,
         sleep=time.sleep, progress=None) -> Player:
    """Reset the board, set the score's sound, and play it through.

    The reset is not optional: a score played over whatever the last session
    left sounding would be two pieces at once, and it also clears the error
    flags so what follows is judged on its own.

    The lead-in is the host's, not the engine's: the piece starts that far past
    the sample count read here, so its first events are queued before their time
    rather than after it. Everything after that is the engine counting samples.
    """
    client.reset()
    apply_patch(client, score.patch)
    player = Player(score, origin=client.now() + round(lead_in * SAMPLE_RATE))
    player.play(client, sleep=sleep, progress=progress)
    return player
