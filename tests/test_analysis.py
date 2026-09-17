"""Offline tone analysis of a recording."""

import math
import wave

import numpy as np
import pytest

from xsynth.host.analysis import analyse

SAMPLE_RATE = 48_000


def _write(path, samples, *, channels=1):
    data = np.clip(np.round(samples), -32768, 32767).astype("<i2")
    if channels > 1 and data.ndim == 1:
        data = np.repeat(data.reshape(-1, 1), channels, axis=1)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(data.reshape(-1).tobytes())
    return path


def _tone(frequency, seconds=1.0, amplitude=32767.0, harmonics=()):
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    signal = amplitude * np.sin(2 * math.pi * frequency * t)
    for order, level in harmonics:
        signal += amplitude * level * np.sin(2 * math.pi * order * frequency * t)
    return signal


def test_a_pure_sine_has_one_peak_and_no_harmonics(tmp_path):
    path = _write(tmp_path / "sine.wav", _tone(440.0))
    report = analyse(str(path))

    assert report.fundamental_hz == pytest.approx(440.0, abs=0.5)
    assert report.peak == 32767
    # A full-scale sine's RMS is peak/sqrt(2), so the crest factor is sqrt(2).
    assert report.crest_factor == pytest.approx(math.sqrt(2), abs=0.01)
    for _, level in report.harmonics:
        assert level < 0.01


def test_a_square_like_tone_shows_odd_harmonics(tmp_path):
    # 1/3 and 1/5 are the square wave's odd harmonics; the second is absent.
    path = _write(tmp_path / "square.wav",
                  _tone(300.0, harmonics=((3, 1 / 3), (5, 1 / 5))))
    report = analyse(str(path))

    assert report.fundamental_hz == pytest.approx(300.0, abs=0.5)
    levels = {round(freq / 300): level for freq, level in report.harmonics}
    assert levels[3] == pytest.approx(1 / 3, abs=0.02)
    assert levels[5] == pytest.approx(1 / 5, abs=0.02)
    assert levels[2] < 0.01
    assert levels[4] < 0.01


def test_the_requested_frequency_is_reported(tmp_path):
    for frequency in (220.0, 660.0, 1000.0):
        path = _write(tmp_path / f"{frequency}.wav", _tone(frequency))
        assert analyse(str(path)).fundamental_hz == pytest.approx(
            frequency, abs=0.5
        )


def test_silence_is_reported_without_dividing_by_zero(tmp_path):
    path = _write(tmp_path / "silence.wav", np.zeros(SAMPLE_RATE))
    report = analyse(str(path))
    assert report.peak == 0
    assert report.rms == 0.0
    assert report.fundamental_hz == 0.0
    assert report.partials == ()


def test_a_channel_can_be_selected(tmp_path):
    t = np.arange(SAMPLE_RATE) / SAMPLE_RATE
    stereo = np.stack([
        32767 * np.sin(2 * math.pi * 300 * t),
        32767 * np.sin(2 * math.pi * 700 * t),
    ], axis=1)
    path = _write(tmp_path / "stereo.wav", stereo, channels=2)

    assert analyse(str(path), channel=0).fundamental_hz == pytest.approx(
        300.0, abs=0.5
    )
    assert analyse(str(path), channel=1).fundamental_hz == pytest.approx(
        700.0, abs=0.5
    )
    with pytest.raises(ValueError):
        analyse(str(path), channel=2)


def test_the_report_reads_like_a_sentence(tmp_path):
    path = _write(tmp_path / "sine.wav", _tone(440.0, seconds=0.5))
    text = analyse(str(path)).describe()
    assert "fundamental 440.0 Hz" in text
    assert "peak 32767" in text
