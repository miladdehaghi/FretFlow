"""Practice logic for both modes. Pure Python - no Qt - so it is easy to test."""
from __future__ import annotations

import time
from bisect import bisect_left, bisect_right
from enum import Enum

from .audio_in import PitchState
from .chords import check_chord
from .models import Song

PENDING, HIT, MISS, SKIP = range(4)
LEAD_IN = 1.5   # seconds of music replayed before the section after a rewind
SPEC_DELAY = 0.26   # a chord is only analysed ~230 ms after the strum: don't call it missed before
ONSET_LAG = 0.05    # part of latency_ms that is detector lag (not present for onset time stamps)


class Mode(Enum):
    AUTO = "Auto Play"
    FOLLOW = "Follow Me"


class PracticeEngine:
    def __init__(self, settings, player, tracker):
        self.s, self.player, self.tracker = settings, player, tracker
        self.mode = Mode.AUTO
        self.song: Song | None = None       # speed-scaled copy used for play
        self.src_song: Song | None = None   # original
        self.speed = 1.0
        self._fresh_start = True
        self.track_index = 0
        self.events: list = []
        self.times: list[float] = []
        self.status: list[int] = []
        self.tuning: list[int] = []
        self.running = False
        self.detected = PitchState()
        self.hits = self.misses = self.streak = self.best_streak = self.rewinds = 0
        self.message, self.message_until = "", 0.0
        self.flash_kind, self.flash_until = "", 0.0
        self.index = 0               # Follow Me: current note
        self._vt = 0.0               # Follow Me: smoothed display time
        self._scan = 0               # Auto: first event not yet judged
        self._consumed = -1          # last pluck already used for a hit
        self._mistakes: list[float] = []
        self.chord_midis: list[list[int]] = []
        self.chord_result: dict[int, dict[int, bool]] = {}   # event -> {midi: found} of last attempt
        self._consumed_spec = 0
        self._ignore_before = 0.0    # strums that started before this (monotonic) were already used

    # ------------------------------------------------------------ setup
    def load(self, song: Song, track_index: int) -> None:
        self.src_song, self.track_index = song, track_index
        self._rebuild()
        self.reset()

    def _rebuild(self) -> None:
        """(Re)build event lists; Auto Play applies the practice speed, Follow Me ignores it."""
        self.song = self.src_song.scaled(self.speed if self.mode is Mode.AUTO else 1.0)
        tr = self.song.tracks[self.track_index]
        self.tuning = tr.tuning or [64, 59, 55, 50, 45, 40]
        self.events = [e for e in tr.events if e.playable]
        self.times = [e.time for e in self.events]
        self.chord_midis = [sorted({n.midi for n in e.notes if not n.dead}) for e in self.events]
        self.reset()

    @property
    def bar_times(self) -> list[float]:
        return self.song.bar_times if self.song else []

    @property
    def duration(self) -> float:
        return self.song.duration if self.song else 0.0

    def set_mode(self, mode: Mode) -> None:
        self.mode = mode
        if self.src_song:
            self._rebuild()
        self.reset()

    def set_speed(self, k: float) -> None:
        self.speed = k
        if self.src_song:
            self._rebuild()
            self.reset()

    def reset(self) -> None:
        self.running = False
        self.player.pause()
        self.status = [PENDING] * len(self.events)
        self.hits = self.misses = self.streak = self.best_streak = self.rewinds = 0
        self._mistakes.clear()
        self._seek_to(0.0, snap=True)
        self.message = ""
        self._ignore_before = time.monotonic()
        self._fresh_start = True

    # ---------------------------------------------------------- transport
    def start(self) -> None:
        if not self.events:
            return
        if self.mode is Mode.AUTO and self.player.position() >= self.duration:
            self.reset()
        if self.mode is Mode.AUTO and self._fresh_start and self.s.lead_in_s > 0:
            self._seek_to(self.player.position() - self.s.lead_in_s)     # start a few seconds early
        self._fresh_start = False
        self.running = True
        self._ignore_before = time.monotonic()
        if self.mode is Mode.AUTO:
            self.player.play()

    def pause(self) -> None:
        self.running = False
        self.player.pause()

    def toggle(self) -> None:
        self.pause() if self.running else self.start()

    def seek(self, t: float) -> None:
        self._mistakes.clear()
        self.streak = 0
        self._fresh_start = True
        self._seek_to(t)

    def _seek_to(self, t: float, snap: bool = False) -> None:
        tol = self.s.tolerance_ms / 1000
        self.chord_result.clear()
        for i, et in enumerate(self.times):
            if et >= t - tol:
                self.status[i] = PENDING
            elif self.status[i] == PENDING:
                self.status[i] = SKIP
        self._scan = bisect_left(self.times, t - tol)
        self.index = bisect_left(self.times, t)
        if self.mode is Mode.AUTO:
            self.player.seek(t)
        elif snap:
            self._vt = self.times[0] - 1.0 if self.times else 0.0
        self._recount()

    # -------------------------------------------------------------- state
    @property
    def now(self) -> float:
        return self.player.position() if self.mode is Mode.AUTO else self._vt

    @property
    def progress(self) -> float:
        if not self.events:
            return 0.0
        if self.mode is Mode.AUTO:
            return max(0.0, min(1.0, self.now / max(self.duration, 0.01)))
        return self.index / len(self.events)

    @property
    def accuracy(self) -> float | None:
        done = self.hits + self.misses
        return self.hits / done if done else None

    def _recount(self) -> None:
        self.hits = self.status.count(HIT)
        self.misses = self.status.count(MISS)

    def _say(self, text: str, secs: float = 2.5) -> None:
        self.message, self.message_until = text, time.monotonic() + secs

    def _flash(self, kind: str) -> None:
        self.flash_kind, self.flash_until = kind, time.monotonic() + 0.25

    def matches(self, event, midi: float) -> bool:
        tol = self.s.cents_tolerance / 100
        for n in event.notes:
            if n.dead:
                continue
            d = midi - n.midi
            if self.s.ignore_octave:
                d = (d + 6) % 12 - 6
            if abs(d) <= tol:
                return True
        return False

    def is_chord(self, i: int) -> bool:
        return self.s.chord_check and len(self.chord_midis[i]) >= 2

    def _register_hit(self, i: int, st: PitchState) -> None:
        self._ignore_before = time.monotonic()
        self.status[i] = HIT
        self.hits += 1
        self.streak += 1
        self.best_streak = max(self.best_streak, self.streak)
        self._consumed = st.note_id
        self._flash("hit")

    # --------------------------------------------------------------- tick
    def tick(self, dt: float) -> None:
        self.detected = st = self.tracker.state()
        if not self.events:
            return
        if self.mode is Mode.AUTO:
            if self.running:
                self._tick_auto(st)
        else:
            self._tick_follow(st, dt)

    def _fresh(self, st: PitchState) -> bool:
        return st.stable and st.midi is not None and st.note_id != self._consumed

    def _new_spec(self, st: PitchState) -> bool:
        """True once per strum spectrum that has not been judged yet."""
        if st.spec is None or st.spec_id == self._consumed_spec:
            return False
        self._consumed_spec = st.spec_id
        return st.onset_mono > self._ignore_before

    def _judge_chord(self, i: int, st: PitchState) -> bool:
        ok, found = check_chord(st.spec, self.chord_midis[i], self.s.chord_required / 100)
        self.chord_result[i] = dict(zip(self.chord_midis[i], found))
        return ok

    def _tick_auto(self, st: PitchState) -> None:
        t = self.player.position() - self.s.latency_ms / 1000
        tol = self.s.tolerance_ms / 1000

        if self._fresh(st):                                    # single plucked note
            lo, hi = bisect_left(self.times, t - tol), bisect_right(self.times, t + tol)
            best, chord_near = None, False
            for i in range(lo, hi):
                if self.status[i] != PENDING:
                    continue
                if self.is_chord(i):
                    chord_near = True
                elif self.matches(self.events[i], st.midi):
                    d = abs(self.times[i] - t)
                    if best is None or d < best[0]:
                        best = (d, i)
            if best:
                self._register_hit(best[1], st)
            elif not chord_near:                               # a strum also looks like a "wrong note"
                self._flash("wrong")

        if self._new_spec(st):                                 # strummed chord
            t_on = (self.player.position() - (time.monotonic() - st.onset_mono)
                    - max(0.0, self.s.latency_ms / 1000 - ONSET_LAG))
            best, tried = None, False
            for i in range(bisect_left(self.times, t_on - tol), bisect_right(self.times, t_on + tol)):
                if self.status[i] == PENDING and self.is_chord(i):
                    tried = True
                    if self._judge_chord(i, st):
                        d = abs(self.times[i] - t_on)
                        if best is None or d < best[0]:
                            best = (d, i)
            if best:
                self._register_hit(best[1], st)
            elif tried:
                self._flash("wrong")

        n = len(self.times)
        while self._scan < n:
            i = self._scan
            if self.times[i] >= t - tol - (SPEC_DELAY if self.is_chord(i) else 0.0):
                break
            self._scan += 1
            if self.status[i] == PENDING:
                self.status[i] = MISS
                self.misses += 1
                self.streak = 0
                self._mistakes.append(self.times[i])
                if self._check_rewind(self.times[i]):
                    return

        if t > self.duration + 0.5:
            self.pause()
            acc = self.accuracy
            self._say(f"Finished - accuracy {acc:.0%}" if acc is not None else "Finished", 6)

    def _check_rewind(self, miss_time: float) -> bool:
        bar = self.song.bar_at(miss_time)
        first_bar = max(0, bar - self.s.rewind_bars + 1)
        window_start = self.song.bar_times[first_bar]
        recent = [m for m in self._mistakes if m >= window_start]
        if len(recent) < self.s.max_mistakes:
            return False
        self.rewinds += 1
        self._mistakes.clear()
        self.streak = 0
        self._seek_to(window_start - LEAD_IN)
        self._say(f"Too many mistakes - back to bar {first_bar + 1}", 3)
        return True

    def _tick_follow(self, st: PitchState, dt: float) -> None:
        i = self.index
        if self.running and i < len(self.events):
            ok = None
            if self.is_chord(i):
                if self._new_spec(st):
                    ok = self._judge_chord(i, st)
            elif self._fresh(st):
                ok = self.matches(self.events[i], st.midi)
            if ok:
                self._register_hit(i, st)
                self.index += 1
                if self.index >= len(self.events):
                    self.running = False
                    self._say("Finished - well played!", 6)
            elif ok is False:
                self._flash("wrong")
        target = self.times[min(self.index, len(self.times) - 1)]
        if self.index >= len(self.times):
            target += 1.0
        self._vt += (target - self._vt) * min(1.0, dt * 10.0)
