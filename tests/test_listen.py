"""Choosing a capture source, and recording it, without the card or the board.

Which source to record is not guessed at: a name like
``alsa_input.usb-MACROSILICON_C1-1_USB3_Video_20210621-02.analog-stereo`` is
unpleasant to type and easy to get subtly wrong, so sources are listed with a
short handle and ``--source`` takes either. Everything here is about that, and
about the three `parecord` traps: `-d` does nothing, the source ships muted, and
it takes seconds to wake from suspended.
"""

import subprocess

import pytest

from xsynth.host import listen

CARD = ("alsa_input.usb-MACROSILICON_C1-1_USB3_Video_20210621-02."
        "analog-stereo")
MONITOR = "alsa_output.pci-0000_05_00.6.analog-stereo.monitor"

SOURCES = (
    f"51\t{MONITOR}\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
    f"20522\t{CARD}\tPipeWire\ts16le 2ch 48000Hz\tSUSPENDED\n"
)

# The same two sources, the other way round.
REORDERED = (
    f"20522\t{CARD}\tPipeWire\ts16le 2ch 48000Hz\tSUSPENDED\n"
    f"51\t{MONITOR}\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
)


def _pactl(monkeypatch, output, returncode=0, stderr=""):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, returncode, output, stderr)
    monkeypatch.setattr(listen, "_run", fake_run)


def _names(monkeypatch, output):
    _pactl(monkeypatch, output)
    return listen.sources()


def test_the_listing_pairs_every_source_with_a_handle(monkeypatch):
    known = _names(monkeypatch, SOURCES)
    assert listen.listing() == [(listen.handle(name), name) for name in known]


def test_a_handle_does_not_move_when_the_list_does(monkeypatch):
    """Derived from the name, not from a position: a source that moves up the
    list keeps its handle, and a handle for a source that is gone matches
    nothing rather than whatever took its place."""
    _pactl(monkeypatch, SOURCES)
    before = dict((name, handle) for handle, name in listen.listing())
    _pactl(monkeypatch, REORDERED)
    after = dict((name, handle) for handle, name in listen.listing())
    assert before == after
    assert before[CARD] == listen.handle(CARD)


def test_handles_lengthen_until_two_names_do_not_share_one(monkeypatch):
    def stub(name, length=listen.HANDLE_LENGTH):
        # These two agree for six characters and part on the seventh.
        return {"a": "abcdefg", "b": "abcdefz"}[name][:length]

    monkeypatch.setattr(listen, "handle", stub)
    assert listen.handles(["a", "b"]) == {"a": "abcdefg", "b": "abcdefz"}


def test_a_handle_resolves_to_its_source(monkeypatch):
    known = _names(monkeypatch, SOURCES)
    assert listen.resolve(listen.handle(CARD), known) == CARD


def test_a_whole_name_resolves_too(monkeypatch):
    known = _names(monkeypatch, SOURCES)
    assert listen.resolve(CARD, known) == CARD


def test_anything_else_is_refused_with_the_list(monkeypatch):
    known = _names(monkeypatch, SOURCES)
    with pytest.raises(listen.ListenError) as caught:
        listen.resolve("deadbe", known)
    assert CARD in str(caught.value)
    assert listen.handle(CARD) in str(caught.value)


def test_no_sound_server_is_reported(monkeypatch):
    _pactl(monkeypatch, "", returncode=1, stderr="connection refused")
    with pytest.raises(listen.ListenError, match="connection refused"):
        listen.sources()


def test_the_source_is_unmuted_and_turned_up(monkeypatch):
    """It ships muted, and a muted capture is a file of zeroes that looks
    exactly like a board that is not playing."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(listen, "_run", fake_run)
    listen.prepare("card")
    assert calls == [
        ["pactl", "set-source-mute", "card", "0"],
        ["pactl", "set-source-volume", "card", "100%"],
    ]


def test_a_capture_throws_a_warmup_away_before_the_one_that_counts(
        monkeypatch, tmp_path):
    """`parecord -d` does nothing, so `timeout` is the only bound. The throwaway
    in front is what stops the card losing the beginning of the real capture:
    measured against a 43.93 s score, one second of it still lost 1.15 s and six
    lost nothing."""
    runs = []

    def fake_run(command, **kwargs):
        runs.append(command)
        return subprocess.CompletedProcess(command, listen.TIMED_OUT, "", "")

    monkeypatch.setattr(listen, "_run", fake_run)
    monkeypatch.setattr(listen, "prepare", lambda source: None)
    listen.capture(3.0, tmp_path / "out.wav", source="card")

    assert [command[0] for command in runs] == ["timeout", "timeout"]
    captures = [command for command in runs if command[2] == "parecord"]
    assert captures[0][1] == str(listen.WARMUP_SECONDS)
    assert captures[1][1] == "3.0"
    assert captures[1][-1] == str(tmp_path / "out.wav")
    assert "--device=card" in captures[1]


def test_the_warmup_can_be_turned_off(monkeypatch, tmp_path):
    runs = []

    def fake_run(command, **kwargs):
        runs.append(command)
        return subprocess.CompletedProcess(command, listen.TIMED_OUT, "", "")

    monkeypatch.setattr(listen, "_run", fake_run)
    monkeypatch.setattr(listen, "prepare", lambda source: None)
    listen.capture(3.0, tmp_path / "out.wav", source="card", warmup=0)
    assert len(runs) == 1


def test_the_recording_is_announced_when_it_starts_not_before(monkeypatch,
                                                              tmp_path):
    """The warm-up must not be inside the window the announcement covers, or it
    would swallow whatever was played right after it."""
    order = []

    def fake_run(command, **kwargs):
        order.append(("capture", command[1]))
        return subprocess.CompletedProcess(command, listen.TIMED_OUT, "", "")

    monkeypatch.setattr(listen, "_run", fake_run)
    monkeypatch.setattr(listen, "prepare", lambda source: None)
    monkeypatch.setattr(listen, "resolve", lambda source, known=None: "card")
    monkeypatch.setattr(listen, "play_back", lambda path: None)
    listen.listen(3.0, source="card", output=tmp_path / "out.wav",
                  warmup=6.0, report=lambda line: order.append(("report", line)))

    assert order[0] == ("capture", "6.0")
    assert order[1][0] == "report" and "recording" in order[1][1]
    assert order[2] == ("capture", "3.0")


def test_a_capture_that_fails_for_another_reason_is_reported(monkeypatch,
                                                             tmp_path):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "no such device")

    monkeypatch.setattr(listen, "_run", fake_run)
    monkeypatch.setattr(listen, "prepare", lambda source: None)
    with pytest.raises(listen.ListenError, match="no such device"):
        listen.capture(3.0, tmp_path / "out.wav", source="card")


def test_listen_says_what_it_recorded_and_where(monkeypatch, tmp_path):
    _pactl(monkeypatch, SOURCES)

    def fake_capture(seconds, path, source, warmup=0, report=None):
        if report is not None:
            report(f"recording {seconds:g} s from {source}")
        return path

    monkeypatch.setattr(listen, "capture", fake_capture)
    monkeypatch.setattr(listen, "play_back", lambda path: None)

    lines = []
    path = listen.listen(2.0, source=listen.handle(CARD),
                         output=tmp_path / "kept.wav", report=lines.append)
    assert path == tmp_path / "kept.wav"
    assert "2 s" in lines[0] and CARD in lines[0]
    assert str(path) in lines[1]


def test_playback_can_be_skipped(monkeypatch, tmp_path):
    _pactl(monkeypatch, SOURCES)
    played = []
    monkeypatch.setattr(listen, "capture", lambda seconds, path, source, **kw: path)
    monkeypatch.setattr(listen, "play_back", played.append)

    listen.listen(1.0, source=CARD, output=tmp_path / "kept.wav", play=False)
    assert played == []


def test_the_default_output_is_a_temporary_file(monkeypatch):
    _pactl(monkeypatch, SOURCES)
    monkeypatch.setattr(listen, "capture", lambda seconds, path, source, **kw: path)
    monkeypatch.setattr(listen, "play_back", lambda path: None)

    path = listen.listen(1.0, source=CARD)
    assert path.suffix == ".wav"
    assert path.name.startswith("xsynth-")


def test_an_unlisted_source_never_reaches_parecord(monkeypatch, tmp_path):
    _pactl(monkeypatch, SOURCES)
    monkeypatch.setattr(listen, "capture",
                        lambda *a, **k: pytest.fail("it recorded anyway"))
    with pytest.raises(listen.ListenError):
        listen.listen(1.0, source="nonsense", output=tmp_path / "out.wav")
