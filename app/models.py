"""Plain data classes shared by every module (no Qt, no audio)."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field

# String 0 is the thinnest string (high E), matching how a TAB is drawn.
STANDARD_TUNING = [64, 59, 55, 50, 45, 40]  # E4 B3 G3 D3 A2 E2

_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def midi_name(m: int) -> str:
    return f"{_NAMES[m % 12]}{m // 12 - 1}"


@dataclass(slots=True)
class Note:
    string: int          # 0 = highest string
    fret: int
    midi: int            # sounding pitch (tuning + fret + capo)
    dead: bool = False   # muted "x" note - shown but never judged


@dataclass(slots=True)
class Event:
    """One or more notes starting at the same moment."""
    time: float          # seconds from song start
    duration: float
    notes: list[Note]
    bar: int             # 0-based bar index

    @property
    def playable(self) -> bool:
        return any(not n.dead for n in self.notes)


@dataclass
class Track:
    name: str
    tuning: list[int]                    # open-string MIDI notes, high -> low
    events: list[Event] = field(default_factory=list)
    percussion: bool = False
    program: int = 25                    # General MIDI program (for backing timbre)

    @property
    def is_standard(self) -> bool:
        return self.tuning == STANDARD_TUNING


@dataclass
class Song:
    title: str
    artist: str
    tracks: list[Track]
    bar_times: list[float]               # start time of each bar
    duration: float
    tempo: float = 120.0                 # initial tempo (BPM) - used by MIDI/GP export

    def scaled(self, k: float) -> "Song":
        """Copy with all times divided by k (k=0.5 -> half speed). Pitch is unaffected."""
        if k == 1.0:
            return self
        tracks = [Track(t.name, t.tuning, [Event(e.time / k, e.duration / k, e.notes, e.bar) for e in t.events],
                        t.percussion, t.program) for t in self.tracks]
        return Song(self.title, self.artist, tracks, [b / k for b in self.bar_times], self.duration / k,
                    self.tempo)

    def bar_at(self, t: float) -> int:
        return max(0, bisect_right(self.bar_times, t) - 1)
