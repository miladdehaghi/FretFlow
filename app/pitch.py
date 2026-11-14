"""Monophonic pitch detection (YIN algorithm, FFT-accelerated)."""
from __future__ import annotations

import numpy as np


def detect_pitch(frame: np.ndarray, sr: int, fmin: float = 70.0, fmax: float = 1400.0,
                 threshold: float = 0.15) -> tuple[float | None, float]:
    """Return (frequency_hz, confidence) or (None, 0.0) when no clear pitch."""
    n = frame.size
    tau_max = min(int(sr / fmin), n // 2 - 1)
    tau_min = max(2, int(sr / fmax))
    w = n - tau_max
    x = frame - frame.mean()

    nfft = 1 << (n + w - 1).bit_length()
    corr = np.fft.irfft(np.conj(np.fft.rfft(x[:w], nfft)) * np.fft.rfft(x, nfft), nfft)[: tau_max + 1]
    cs = np.concatenate(([0.0], np.cumsum(x * x)))
    taus = np.arange(tau_max + 1)
    d = cs[w] + (cs[w + taus] - cs[taus]) - 2.0 * corr   # difference function
    d[0] = 0.0

    cmnd = np.ones_like(d)                               # cumulative mean normalised
    cmnd[1:] = d[1:] * taus[1:] / np.maximum(np.cumsum(d[1:]), 1e-12)

    below = np.nonzero(cmnd[tau_min: tau_max + 1] < threshold)[0]
    if below.size:
        t = int(below[0]) + tau_min
        while t + 1 <= tau_max and cmnd[t + 1] < cmnd[t]:
            t += 1
    else:
        t = int(np.argmin(cmnd[tau_min: tau_max + 1])) + tau_min
        if cmnd[t] > 0.35:
            return None, 0.0

    shift = 0.0
    if 1 < t < tau_max:
        a, b, c = cmnd[t - 1], cmnd[t], cmnd[t + 1]
        denom = a - 2 * b + c
        if denom != 0:
            shift = 0.5 * (a - c) / denom
    freq = sr / (t + shift)
    if not fmin <= freq <= fmax:
        return None, 0.0
    return float(freq), float(1.0 - cmnd[t])
