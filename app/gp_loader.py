"""Guitar Pro importer: GP3/GP4/GP5 (PyGuitarPro) and GP7/GP8 .gp (gpif_loader).

To add another format, write a function returning a `Song` and register it in LOADERS.
Legacy GPX (GP6) uses a different container and is not supported.
"""
from __future__ import annotations

from bisect import bisect_right
from pathlib import Path

from .gpif_loader import load_gpif
from .models import Event, Note, Song, Track

TICKS_PER_QUARTER = 960


class UnsupportedFormat(Exception):
    pass


class TempoMap:
    """Converts Guitar Pro ticks to seconds, honouring per-bar tempo changes."""

    def __init__(self, headers, song=None):
        changes = _tempo_changes(song)
        self.ticks: list[int] = []
        self.secs: list[float] = []
        self.bpms: list[float] = []
        t, last, bpm = 0.0, headers[0].start, max(30.0, changes.get(0, 120.0))
        for i, h in enumerate(headers):
            t += (h.start - last) / TICKS_PER_QUARTER * 60.0 / bpm
            last = h.start
            bpm = max(30.0, changes.get(i, bpm))
            self.ticks.append(h.start)
            self.secs.append(t)
            self.bpms.append(bpm)

    def time(self, tick: int) -> float:
        i = max(0, bisect_right(self.ticks, tick) - 1)
        return self.secs[i] + (tick - self.ticks[i]) / TICKS_PER_QUARTER * 60.0 / self.bpms[i]


def _tempo_changes(song) -> dict[int, float]:
    """Measure index -> BPM, read from wherever the installed pyguitarpro
    version stores tempo: on the measure headers (<= 0.9) or in beat
    mix-table changes (0.10+)."""
    out: dict[int, float] = {}
    if song is None:
        return out
    headers = getattr(song, "measureHeaders", [])
    if headers and getattr(headers[0], "tempo", None) is not None:
        for i, h in enumerate(headers):
            v = getattr(h.tempo, "value", None)
            if v:
                out[i] = float(v)
        if out:
            return out
    for tr in getattr(song, "tracks", []):
        for i, measure in enumerate(tr.measures):
            for voice in measure.voices:
                for beat in voice.beats:
                    item = getattr(getattr(beat.effect, "mixTableChange", None), "tempo", None)
                    v = getattr(item, "value", None)
                    if v:
                        out.setdefault(i, float(v))
        if out:
            break
    if 0 not in out and getattr(song, "tempo", None):
        out[0] = float(song.tempo)          # song.tempo is the initial tempo
    return out


def _load_gp(path: Path) -> Song:
    import guitarpro as gp   # imported lazily: only needed for GP3-GP5

    song = gp.parse(str(path))
    headers = song.measureHeaders
    tm = TempoMap(headers, song)
    bar_times = [tm.time(h.start) for h in headers]
    duration = tm.time(headers[-1].start + headers[-1].length)

    tracks: list[Track] = []
    for tr in song.tracks:
        strings = sorted(tr.strings, key=lambda s: s.number)
        tuning = [s.value for s in strings]
        perc = bool(tr.isPercussionTrack)
        events: dict[int, Event] = {}
        sustain: dict[int, Event] = {}            # last event per string (for ties)

        for bar, measure in enumerate(tr.measures):
            for voice in measure.voices:
                for beat in voice.beats:
                    if not beat.notes:
                        continue
                    t0 = tm.time(beat.start)
                    dur = tm.time(beat.start + beat.duration.time) - t0
                    for n in beat.notes:
                        if n.type == gp.NoteType.rest:
                            continue
                        if n.type == gp.NoteType.tie and n.string in sustain:
                            sustain[n.string].duration += dur
                            continue
                        idx = n.string - 1
                        if perc:
                            midi = n.value
                        elif 0 <= idx < len(tuning):
                            midi = tuning[idx] + n.value + tr.offset
                        else:
                            continue
                        ev = events.setdefault(beat.start, Event(t0, dur, [], bar))
                        ev.duration = max(ev.duration, dur)
                        ev.notes.append(Note(idx, n.value, midi, n.type == gp.NoteType.dead))
                        sustain[n.string] = ev

        evs = sorted(events.values(), key=lambda e: e.time)
        for e in evs:
            e.notes.sort(key=lambda n: n.string)
        program = getattr(getattr(tr, "channel", None), "instrument", 25) or 25
        tracks.append(Track(tr.name.strip() or "Track", tuning, evs, perc, program))

    return Song(song.title or Path(path).stem, song.artist or "", tracks, bar_times, duration,
                tm.bpms[0] if tm.bpms else 120.0)


LOADERS = {".gp3": _load_gp, ".gp4": _load_gp, ".gp5": _load_gp, ".gp": load_gpif}


def load_song(path: str | Path) -> Song:
    path = Path(path)
    loader = LOADERS.get(path.suffix.lower())
    if loader is None:
        raise UnsupportedFormat(
            f"'{path.suffix}' is not supported yet. Open the file in Guitar Pro or TuxGuitar "
            "and save it as .gp or .gp5."
        )
    return loader(path)
