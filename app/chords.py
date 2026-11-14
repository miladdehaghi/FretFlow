"""Score-informed chord verification.

Blind polyphonic transcription from a microphone is unreliable (harmonics of different
strings overlap). Instead we know which notes the tab *expects* and test each one for
evidence in the spectrum of the strum: energy at its fundamental and first harmonics,
relative to the local noise floor. Octave aliasing (a low note's harmonic faking the note
an octave above) is handled by a sub-octave penalty.
"""
from __future__ import annotations

import math

import numpy as np

SR = 44100
N = 8192                      # ~186 ms window, 5.4 Hz per bin
BIN = SR / N
_WIN = np.hanning(N).astype(np.float32)
_HARM_W = (1.0, 1.0, 0.8, 0.6, 0.4)   # weights for harmonics 1..5
PRESENT = 0.6                 # note score needed to count as played


def compute_spectrum(buf: np.ndarray) -> np.ndarray:
    return np.abs(np.fft.rfft(buf[-N:] * _WIN))


def _peak_present(mag: np.ndarray, f: float, tol_cents: float = 35.0, min_prom: float = 4.0) -> bool:
    """True if the spectrum has a real local peak within `tol_cents` of f."""
    if f > 5000:
        return False
    k0 = int(round(f / BIN))
    r = max(2, int(round(f * 0.0233 / BIN)) + 1)
    a, b = max(2, k0 - r), min(mag.size - 2, k0 + r + 1)
    if b <= a:
        return False
    k = a + int(np.argmax(mag[a:b]))
    if not (mag[k] >= mag[k - 1] and mag[k] >= mag[k + 1]):
        return False
    y0, y1, y2 = (math.log(float(mag[k + d]) + 1e-12) for d in (-1, 0, 1))
    denom = y0 - 2 * y1 + y2
    fk = (k + (0.5 * (y0 - y2) / denom if denom < 0 else 0.0)) * BIN
    if abs(1200 * math.log2(fk / f)) > tol_cents:
        return False
    lo, hi = max(1, int(f * 0.75 / BIN)), int(f * 1.33 / BIN) + 1
    floor = float(np.percentile(mag[lo:hi], 20))      # low percentile: other chord notes must not raise the floor
    return float(mag[k]) / (floor + 1e-9) >= min_prom


def note_score(mag: np.ndarray, midi: int, expected: set[int]) -> float:
    """0..1 evidence that `midi` is sounding (weighted share of harmonics 1-5 with a real peak)."""
    f = 440.0 * 2 ** ((midi - 69) / 12)
    hits = [_peak_present(mag, f * h) for h in range(1, len(_HARM_W) + 1)]
    score = sum(w for w, ok in zip(_HARM_W, hits) if ok) / sum(_HARM_W)
    if not hits[0] and not (hits[1] and hits[2] and hits[3]):
        score = min(score, 0.4)               # fundamental missing and no strong upper series
    # Octave guard: a sounding lower octave that the tab does not expect probably owns these harmonics.
    if midi - 12 not in expected and f / 2 > 70 and _peak_present(mag, f / 2, min_prom=8.0):
        score *= 0.4
    return score


def check_chord(mag: np.ndarray, midis: list[int], required: float = 0.7) -> tuple[bool, list[bool]]:
    """Returns (passed, per-note presence in the order of `midis`)."""
    exp = set(midis)
    found = [note_score(mag, m, exp) >= PRESENT for m in midis]
    need = max(1, math.ceil(required * len(midis) - 1e-9))
    return sum(found) >= need, found
