"""User settings, persisted with QSettings (registry on Windows)."""
from __future__ import annotations

from dataclasses import dataclass, fields

from PySide6.QtCore import QSettings


@dataclass
class Settings:
    input_device: int = -1            # -1 = system default
    gate_db: float = -48.0            # ignore input quieter than this
    tolerance_ms: int = 180           # Auto Play timing window (+/-)
    latency_ms: int = 90              # compensation for mic + detection delay
    cents_tolerance: int = 50         # allowed pitch error
    ignore_octave: bool = False       # accept the right note in any octave
    chord_check: bool = True          # verify every note of a chord (spectrum) instead of any-one-note
    chord_required: int = 70          # % of a chord's notes that must be found
    max_mistakes: int = 5             # Auto Play: mistakes before rewinding
    rewind_bars: int = 2              # ...counted/rewound over this many bars
    backing_volume: int = 70          # 0-100
    backing_include_selected: bool = False
    lead_in_s: int = 3                # Auto Play starts this many seconds early
    lookahead_s: float = 4.0          # view: seconds of tab visible ahead of the target line
    target_pos: int = 24              # view: target line position (% of width)
    capo: int = 0                     # capo fret: shows tab frets minus capo; pitches unchanged

    @classmethod
    def load(cls) -> "Settings":
        q, s = QSettings("GuitarPractice", "GuitarPractice"), cls()
        for f in fields(cls):
            if q.contains(f.name):
                raw, default = q.value(f.name), getattr(s, f.name)
                try:
                    if isinstance(default, bool):
                        val = raw in (True, "true", "1", 1)
                    else:
                        val = type(default)(raw)
                    setattr(s, f.name, val)
                except (TypeError, ValueError):
                    pass
        return s

    def save(self) -> None:
        q = QSettings("GuitarPractice", "GuitarPractice")
        for f in fields(self):
            q.setValue(f.name, getattr(self, f.name))
