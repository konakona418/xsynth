"""Record what the board is playing, and play it back.

The HDMI sink is the only place the audio exists in a form a person can hear,
so every acceptance gate in this project is judged by recording the capture card
and listening to it. `parecord` on its own makes that awkward in three ways,
each of them learned the hard way:

* **``-d`` does nothing.** It records until it is killed, so a "three second"
  capture runs as long as the process does. Only ``timeout`` bounds it, and
  ``parecord`` writes the header's frame count when the signal lands, so the
  file is correct either way.
* **The source ships muted.** A muted capture is a file of zeroes, which looks
  exactly like a board that is not playing.
* **The card is unreliable.** It reports itself `RUNNING` and then sometimes
  delivers nothing at all for a whole capture, and since the board's own silence
  is exact zeroes there is no way to tell that apart from a board that is not
  playing. Nothing here can fix that -- a throwaway capture first was tried and
  measured, and one second of it works no better than six -- so a recording of
  nothing means run it again, not that the board is broken. It also means the
  recording starts when this says it does, rather than a second later with the
  first second thrown away.

Doing the first two here is the whole point: the recording is what the board is
judged on, and it should be one command away rather than a recipe.

Which source to record is **not** guessed. A name like

    alsa_input.usb-MACROSILICON_C1-1_USB3_Video_20210621-02.analog-stereo

is unpleasant to type and easy to get subtly wrong, so ``listen --list`` prints
every source with a short handle and ``--source`` takes the handle or the whole
name. Picking one by matching a substring would work right up until a second
card appeared, and then it would record the wrong thing without saying so.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import time
from pathlib import Path

from xsynth.host.client import SAMPLE_RATE

CHANNELS = 2
SAMPLE_FORMAT = "s16le"

# `timeout` reports this when it had to kill the process, which is how a
# capture is supposed to end.
TIMED_OUT = 124

# Hex characters in a source handle. Long enough that two sources on one
# machine will not collide, short enough to retype; `handles` lengthens them
# anyway if it ever needs to.
HANDLE_LENGTH = 6


class ListenError(RuntimeError):
    """The sound server or a capture tool would not cooperate."""


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True)


def sources() -> list[str]:
    """Every capture source the sound server knows about."""
    result = _run(["pactl", "list", "short", "sources"])
    if result.returncode:
        raise ListenError(f"pactl failed: {result.stderr.strip()}")
    names = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) >= 2:
            names.append(fields[1])
    return names


def handle(name: str, length: int = HANDLE_LENGTH) -> str:
    """A short, stable name for a source, derived from the source itself.

    Derived from the name rather than from a position in the list, so it means
    the same thing next time: a source that moves up the list keeps its handle,
    and a handle for a source that is gone matches nothing instead of quietly
    matching whatever took its place. The name is built from the card's USB
    serial, so it survives a replug.
    """
    return hashlib.sha256(name.encode()).hexdigest()[:length]


def handles(names: list[str]) -> dict[str, str]:
    """Name to handle, lengthened until no two names share one."""
    length = HANDLE_LENGTH
    while True:
        assigned = {name: handle(name, length) for name in names}
        if len(set(assigned.values())) == len(assigned):
            return assigned
        length += 1


def listing() -> list[tuple[str, str]]:
    """Every source as ``(handle, name)``, in the order the server gives them."""
    known = sources()
    assigned = handles(known)
    return [(assigned[name], name) for name in known]


def _table(known: list[str]) -> str:
    assigned = handles(known)
    return "\n".join(f"  {assigned[name]}  {name}" for name in known)


def resolve(source: str, known: list[str] | None = None) -> str:
    """The full name for a handle or a name, refusing anything not listed."""
    known = sources() if known is None else known
    assigned = handles(known)
    for name, assigned_handle in assigned.items():
        if source in (assigned_handle, name):
            return name
    raise ListenError(
        f"{source!r} is not one of the capture sources:\n" + _table(known)
    )


def prepare(source: str) -> None:
    """Unmute the source and turn it up.

    It ships muted, and a muted capture is a file of zeroes that looks exactly
    like a board that is not playing.
    """
    for argument in (["set-source-mute", source, "0"],
                     ["set-source-volume", source, "100%"]):
        result = _run(["pactl", *argument])
        if result.returncode:
            raise ListenError(f"pactl {argument[0]} failed: {result.stderr.strip()}")


def _capture(seconds: float, source: str, path: Path) -> None:
    command = [
        "timeout", str(seconds),
        "parecord", f"--device={source}", "--file-format=wav",
        f"--rate={SAMPLE_RATE}", f"--channels={CHANNELS}",
        f"--format={SAMPLE_FORMAT}", str(path),
    ]
    result = _run(command)
    if result.returncode not in (0, TIMED_OUT):
        raise ListenError(f"parecord failed: {result.stderr.strip()}")


def capture(seconds: float, path: Path, *, source: str) -> Path:
    """Record ``seconds`` of ``source`` into ``path``."""
    prepare(source)
    _capture(seconds, source, path)
    return path


def play_back(path: Path) -> None:
    """Send a recording to the default output."""
    result = _run(["paplay", str(path)])
    if result.returncode:
        raise ListenError(f"paplay failed: {result.stderr.strip()}")


def listen(seconds: float, *, source: str, output: str | Path | None = None,
           play: bool = True, report=None) -> Path:
    """Record ``source`` for ``seconds`` and play it back.

    ``source`` is a handle from :func:`listing` or a whole source name; anything
    else is refused rather than guessed at. Returns the file, which is a
    temporary one unless ``output`` names it. ``report`` is called with progress
    lines, if the caller wants them.
    """
    name = resolve(source)
    if output is not None:
        path = Path(output)
    else:
        stamp = time.strftime("%H%M%S")
        path = Path(tempfile.gettempdir()) / f"xsynth-{stamp}.wav"
    if report is not None:
        report(f"recording {seconds:g} s from {name}")
    capture(seconds, path, source=name)
    if report is not None:
        report(f"wrote {path}")
    if play:
        play_back(path)
    return path
