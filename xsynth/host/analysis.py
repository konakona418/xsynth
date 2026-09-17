"""Offline analysis of a capture-card recording.

Every audio acceptance gate in this project is judged by what the HDMI sink
actually received, not by what the RTL intended, so this turns a WAV into the
few numbers that decide it: the peak and RMS, the fundamental, and each harmonic
relative to the fundamental.

A correct single tone is one peak with every harmonic at 0.000 and an RMS of
``peak / sqrt(2)``. A square wave should show odd harmonics at 1/3, 1/5, 1/7 and
even harmonics at zero. Anything else means the sample format, the sample rate
or the waveform is wrong.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass

import numpy as np

DEFAULT_SAMPLE_RATE = 48_000
DEFAULT_HARMONICS = 8
WINDOW_SECONDS = 1.0


@dataclass(frozen=True)
class ToneReport:
    """What a recording turned out to contain.

    ``partials`` holds the fundamental and its harmonics in order, each relative
    to the strongest component, so ``partials[0]`` is the fundamental at 1.000.
    """

    path: str
    frames: int
    channels: int
    sample_rate: int
    peak: int
    rms: float
    mean: float
    fundamental_hz: float
    partials: tuple[tuple[float, float], ...]
    window_rms: tuple[float, float]

    @property
    def harmonics(self) -> tuple[tuple[float, float], ...]:
        """The partials above the fundamental."""
        return self.partials[1:]

    @property
    def crest_factor(self) -> float:
        return self.peak / self.rms if self.rms else float("inf")

    def describe(self) -> str:
        low, high = self.window_rms
        lines = [
            self.path,
            f"  {self.frames} frames, {self.channels} channels, "
            f"{self.sample_rate} Hz",
            f"  peak {self.peak}  rms {self.rms:.0f}  mean {self.mean:.1f}  "
            f"crest {self.crest_factor:.3f}",
            f"  steady-state rms {high:.0f} "
            f"({WINDOW_SECONDS:g} s windows span {low:.0f}..{high:.0f})",
            f"  fundamental {self.fundamental_hz:.1f} Hz",
            "  partials relative to the fundamental:",
        ]
        for index, (frequency, level) in enumerate(self.partials, start=1):
            lines.append(f"    {index:2d}  {frequency:9.1f} Hz  {level:6.3f}")
        return "\n".join(lines)


def analyse(path: str, *, channel: int = 0,
            harmonics: int = DEFAULT_HARMONICS) -> ToneReport:
    """Measure one channel of a WAV file."""
    with wave.open(path) as handle:
        sample_rate = handle.getframerate()
        channels = handle.getnchannels()
        frames = handle.getnframes()
        if channel >= channels:
            raise ValueError(
                f"{path} has {channels} channels, so channel {channel} does not "
                f"exist"
            )
        raw = np.frombuffer(handle.readframes(frames), dtype="<i2")

    if channels > 1:
        raw = raw.reshape(-1, channels)[:, channel]
    samples = raw.astype(float)

    peak = int(np.abs(samples).max()) if samples.size else 0
    rms = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
    mean = float(samples.mean()) if samples.size else 0.0

    window = max(1, int(WINDOW_SECONDS * sample_rate))
    if samples.size >= 2 * window:
        levels = [
            float(np.sqrt(np.mean(samples[start:start + window] ** 2)))
            for start in range(0, samples.size - window, window)
        ]
        window_rms = (min(levels), max(levels))
    else:
        window_rms = (rms, rms)

    spectrum = np.abs(np.fft.rfft(samples * np.hanning(samples.size)))
    freqs = np.fft.rfftfreq(samples.size, 1 / sample_rate)
    if spectrum.size == 0 or spectrum.max() == 0:
        return ToneReport(path, frames, channels, sample_rate, peak, rms, mean,
                          0.0, (), window_rms)

    spectrum /= spectrum.max()
    fundamental = float(freqs[int(np.argmax(spectrum))])

    measured: list[tuple[float, float]] = []
    for index in range(1, harmonics + 1):
        target = index * fundamental
        if target >= sample_rate / 2:
            break
        centre = int(np.argmin(np.abs(freqs - target)))
        band = spectrum[max(0, centre - 3):centre + 4].max()
        measured.append((target, float(band)))

    return ToneReport(path, frames, channels, sample_rate, peak, rms, mean,
                      fundamental, tuple(measured), window_rms)
