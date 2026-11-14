"""Microphone capture + note tracking (runs on a worker thread)."""
from __future__ import annotations

import math
import queue
import threading
import time
from dataclasses import dataclass, field

import numpy as np

try:
    import sounddevice as sd
except Exception:  # missing PortAudio etc. - the app still runs without a mic
    sd = None

from .chords import N as SPEC_N, compute_spectrum
from .pitch import detect_pitch

SAMPLE_RATE = 44100
BLOCK = 512          # ~11.6 ms hop
FRAME = 2048         # analysis window (fits E2 comfortably, even drop-D)
STABLE_FRAMES = 3    # consecutive agreeing frames before a note counts
SNAP_HOPS = 20       # chord spectrum is taken ~230 ms after an onset (attack noise has died down)


@dataclass(frozen=True)
class PitchState:
    midi: float | None = None   # fractional MIDI note (69 = A4)
    stable: bool = False
    note_id: int = 0            # increments for every new pluck
    level_db: float = -90.0
    spec: np.ndarray | None = field(default=None, compare=False)  # spectrum of the last strum
    spec_id: int = 0
    onset_mono: float = 0.0     # time.monotonic() of the onset that produced `spec`


class PitchTracker:
    def __init__(self, settings):
        self.settings = settings
        self._state = PitchState()
        self._queue: queue.Queue = queue.Queue(maxsize=64)
        self._stream = None
        self._thread: threading.Thread | None = None
        self._running = False

    # -- public --------------------------------------------------------
    @staticmethod
    def input_devices() -> list[tuple[int, str]]:
        if sd is None:
            return []
        out = []
        try:
            for i, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0:
                    out.append((i, d["name"]))
        except Exception:
            pass
        return out

    def state(self) -> PitchState:
        return self._state

    def start(self) -> str | None:
        """Start listening. Returns an error message, or None on success."""
        self.stop()
        if sd is None:
            return "Audio library (sounddevice) unavailable."
        device = self.settings.input_device if self.settings.input_device >= 0 else None
        try:
            self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                          blocksize=BLOCK, device=device, callback=self._on_audio)
            self._stream.start()
        except Exception as exc:
            self._stream = None
            return f"Cannot open microphone: {exc}"
        self._running = True
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        return None

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._state = PitchState()

    # -- internals -----------------------------------------------------
    def _on_audio(self, indata, frames, time_info, status) -> None:
        try:
            self._queue.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def _worker(self) -> None:
        buf = np.zeros(FRAME, dtype=np.float32)
        big = np.zeros(SPEC_N, dtype=np.float32)
        run, cand, silent = 0, None, 0
        stable, note_id, env_db = False, 0, -90.0
        spec, spec_id, spec_onset = None, 0, 0.0
        wait, pending_onset, since_onset = 0, 0.0, 99
        while self._running:
            try:
                block = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            n = block.size
            buf[:-n] = buf[n:]
            buf[-n:] = block
            big[:-n] = big[n:]
            big[-n:] = block

            rms = float(np.sqrt(np.mean(buf[-1024:] ** 2)))
            db = 20 * math.log10(rms + 1e-9)
            freq = None
            if db > self.settings.gate_db:
                freq, _ = detect_pitch(buf, SAMPLE_RATE)

            # A sudden level jump = a (new) pluck or strum.
            since_onset += 1
            if db - env_db > 6.0 and db > self.settings.gate_db + 3.0 and since_onset > 10:
                since_onset = 0
                if wait:                                   # previous strum still waiting: analyse it now
                    spec, spec_id, spec_onset = compute_spectrum(big), spec_id + 1, pending_onset
                wait, pending_onset = SNAP_HOPS, time.monotonic()
                run, cand, stable = 0, None, False         # re-evaluate the single-note tracker
            env_db = max(db, env_db - 1.5)
            if wait:
                wait -= 1
                if wait == 0:
                    spec, spec_id, spec_onset = compute_spectrum(big), spec_id + 1, pending_onset

            midi = None
            if freq:
                midi = 69.0 + 12.0 * math.log2(freq / 440.0)
                silent = 0
                if cand is not None and abs(midi - cand) < 0.7:
                    run += 1
                else:
                    run, stable = 1, False
                cand = midi
                if run >= STABLE_FRAMES and not stable:
                    stable, note_id = True, note_id + 1
            else:
                silent += 1
                if silent >= 3:
                    run, cand, stable = 0, None, False

            self._state = PitchState(midi if stable else None, stable, note_id, db, spec, spec_id, spec_onset)
