"""Backing track: offline synthesis of the other tracks + a sample-accurate player."""
from __future__ import annotations

import time

import numpy as np

try:
    import sounddevice as sd
except Exception:
    sd = None

from .models import Song

SR = 44100


# --------------------------------------------------------------- synthesis
def _pluck(midi: int, dur: float, bass: bool) -> np.ndarray:
    f = 440.0 * 2 ** ((midi - 69) / 12)
    dur = min(dur, 3.0)
    t = np.arange(int((dur + 0.25) * SR)) / SR
    w = 2 * np.pi * f * t
    y = (np.sin(w) + 0.5 * np.sin(2 * w) * np.exp(-3 * t)
         + 0.25 * np.sin(3 * w) * np.exp(-5 * t) + 0.1 * np.sin(4 * w) * np.exp(-8 * t))
    y *= np.exp(-t * (1.6 if bass else 2.4)) * np.minimum(1.0, t / 0.004)
    y *= np.where(t < dur, 1.0, np.exp(-(t - dur) * 20))     # release
    return (y * 0.16).astype(np.float32)


def _drum(midi: int, rng: np.random.Generator) -> np.ndarray:
    if midi <= 36:                                           # kick
        t = np.arange(int(0.25 * SR)) / SR
        phase = 45 * t + 60 * (1 - np.exp(-25 * t)) / 25      # pitch drops 105 -> 45 Hz
        return (np.sin(2 * np.pi * phase) * np.exp(-t * 14) * 0.5).astype(np.float32)
    n = int((0.16 if midi <= 40 else 0.05) * SR)             # snare / hats
    t = np.arange(n) / SR
    noise = rng.standard_normal(n) * np.exp(-t * (25 if midi <= 40 else 90))
    return (noise * (0.18 if midi <= 40 else 0.08)).astype(np.float32)


def render_backing(song: Song, skip_track: int | None) -> np.ndarray:
    mix = np.zeros(int((song.duration + 3.0) * SR), dtype=np.float32)
    rng = np.random.default_rng(1)
    for ti, tr in enumerate(song.tracks):
        if ti == skip_track:
            continue
        bass = 32 <= tr.program <= 39
        for ev in tr.events:
            start = int(ev.time * SR)
            for n in ev.notes:
                if n.dead:
                    continue
                y = _drum(n.midi, rng) if tr.percussion else _pluck(n.midi, ev.duration, bass)
                end = min(start + y.size, mix.size)
                if end > start:
                    mix[start:end] += y[: end - start]
    return np.tanh(mix * 1.3).astype(np.float32)


# ------------------------------------------------------------------ player
class BackingPlayer:
    """Plays a mono buffer and exposes a smooth, latency-corrected clock."""

    def __init__(self):
        self.volume = 0.7
        self._buf = np.zeros(0, dtype=np.float32)
        self._pos = 0
        self._t_cb = 0.0
        self._playing = False
        self._stream = None
        self._fb_base = 0.0      # fallback clock (no output device)
        self._fb_t0 = 0.0

    def open(self) -> str | None:
        if sd is None:
            return "Audio library (sounddevice) unavailable."
        try:
            self._stream = sd.OutputStream(samplerate=SR, channels=1, dtype="float32",
                                           callback=self._callback)
            self._stream.start()
        except Exception as exc:
            self._stream = None
            return f"Cannot open audio output: {exc}"
        return None

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def set_buffer(self, buf: np.ndarray) -> None:
        self._buf = buf

    def play(self) -> None:
        self._fb_t0 = time.perf_counter()
        self._playing = True

    def pause(self) -> None:
        if self._stream is None and self._playing:
            self._fb_base += time.perf_counter() - self._fb_t0
        self._playing = False

    def seek(self, t: float) -> None:
        self._pos = int(t * SR)          # t may be negative: silent lead-in
        self._fb_base, self._fb_t0 = t, time.perf_counter()

    def position(self) -> float:
        if self._stream is None:
            return self._fb_base + (time.perf_counter() - self._fb_t0 if self._playing else 0.0)
        pos = self._pos / SR
        if self._playing:
            pos += time.perf_counter() - self._t_cb - float(self._stream.latency)
        return pos

    def _callback(self, outdata, frames, time_info, status) -> None:
        outdata.fill(0)
        if self._playing:
            a = self._pos
            off = max(0, -a)                                   # silence before time 0
            if off < frames:
                chunk = self._buf[max(a, 0):max(a, 0) + frames - off]
                outdata[off:off + chunk.size, 0] = chunk * self.volume
            self._pos = a + frames
        self._t_cb = time.perf_counter()
