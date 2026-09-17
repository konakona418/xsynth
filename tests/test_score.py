"""The score format: what a file may say, and what it says when it may not."""

import pytest

from xsynth.host.score import (
    Note,
    Patch,
    ScoreError,
    load_score,
    parse_pitch,
    parse_score,
)


def test_a_note_line_is_a_time_a_pitch_and_a_duration():
    score = parse_score("0.5 C4 1.25")
    assert score.notes == (Note(0.5, 60, 1.25),)


def test_pitch_names_follow_midi_where_a4_is_69():
    assert parse_pitch("C4") == 60
    assert parse_pitch("A4") == 69
    assert parse_pitch("A#3") == 58
    assert parse_pitch("Bb3") == 58
    assert parse_pitch("C-1") == 0
    assert parse_pitch("69") == 69
    assert parse_pitch("c4") == 60


def test_a_pitch_the_engine_cannot_reach_is_refused():
    with pytest.raises(ScoreError, match=r"0\.\.127"):
        parse_pitch("C10")


def test_a_pitch_that_is_not_one_says_what_a_pitch_looks_like():
    with pytest.raises(ScoreError, match="A#3"):
        parse_pitch("H4")


def test_the_header_names_the_parameters_the_player_sets():
    score = parse_score("wave saw\nattack 0.005\nmaster 0.5\n0.0 C4 0.1")
    assert score.patch == Patch(wave="saw", attack=0.005, master=0.5)
    assert score.notes == (Note(0.0, 60, 0.1),)


def test_a_chord_is_lines_sharing_a_time():
    score = parse_score("0.0 C4 1.0\n0.0 E4 1.0\n0.0 G4 1.0")
    assert [note.pitch for note in score.notes] == [60, 64, 67]
    assert len({note.time for note in score.notes}) == 1


def test_comments_and_blank_lines_go_away():
    score = parse_score("; a comment\n\n0.0 C4 1.0 ; trailing\n")
    assert score.notes == (Note(0.0, 60, 1.0),)


def test_a_sharp_is_not_a_comment():
    """A#3 is a note, which is why the comment character is ';'."""
    assert parse_score("0.0 A#3 1.0").notes[0].pitch == 58


def test_an_unknown_waveform_names_the_ones_that_exist():
    with pytest.raises(ScoreError, match="sine, saw, square, triangle"):
        parse_score("wave noise\n0.0 C4 1.0")


def test_a_fourth_column_is_refused_rather_than_ignored():
    """Velocity needs a protocol change, not a column, so a score asking for
    it should say so instead of quietly dropping it."""
    with pytest.raises(ScoreError, match="velocity"):
        parse_score("0.0 C4 1.0 0.5")


def test_a_bad_line_reports_its_number():
    with pytest.raises(ScoreError, match="line 2"):
        parse_score("0.0 C4 1.0\nlater C4 1.0")


def test_a_negative_duration_is_refused():
    with pytest.raises(ScoreError, match="negative"):
        parse_score("0.0 C4 -1.0")


def test_a_level_that_is_not_a_fraction_is_refused():
    with pytest.raises(ScoreError, match="fraction"):
        parse_score("master 2\n0.0 C4 1.0")


def test_the_duration_is_when_the_last_note_stops():
    assert parse_score("0.0 C4 0.5\n1.0 E4 2.0").duration == 3.0


def test_notes_come_back_in_time_order():
    score = parse_score("2.0 G4 1.0\n0.0 C4 1.0")
    assert [note.pitch for note in score.notes] == [60, 67]


def test_a_pitch_maps_to_its_frequency():
    assert parse_score("0.0 A4 1.0").notes[0].hz == pytest.approx(440.0)
    assert parse_score("0.0 A3 1.0").notes[0].hz == pytest.approx(220.0)


def test_a_score_loads_from_a_file(tmp_path):
    path = tmp_path / "tune.txt"
    path.write_text("wave square\n0.0 C4 0.5\n")
    score = load_score(path)
    assert score.patch.wave == "square"
    assert score.notes == (Note(0.0, 60, 0.5),)
