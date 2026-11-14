"""Guitar Pro 7/8 (.gp) importer. A .gp file is a ZIP holding Content/score.gpif (XML).

Pure standard library. Simplifications: repeats are not unrolled, tempo changes apply from
the start of their bar, grace notes are skipped.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .models import Event, Note, Song, Track

_VALUES = {"Whole": 4.0, "Half": 2.0, "Quarter": 1.0, "Eighth": 0.5, "16th": 0.25,
           "32nd": 0.125, "64th": 0.0625, "128th": 0.03125}
_TEMPO_UNIT = {1: 0.5, 2: 1.0, 3: 1.5, 4: 2.0, 5: 3.0}   # beat unit -> quarter notes
_STANDARD = [40, 45, 50, 55, 59, 64]                      # low -> high, GPIF order


def _ints(text: str | None) -> list[int]:
    return [int(x) for x in (text or "").split()]


def _index(parent: ET.Element | None, tag: str) -> dict[str, ET.Element]:
    return {el.get("id"): el for el in parent.findall(tag)} if parent is not None else {}


def _props(el: ET.Element | None) -> dict[str, ET.Element]:
    return {p.get("name"): p for p in el.findall("Properties/Property")} if el is not None else {}


def load_gpif(path: Path) -> Song:
    try:
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith("score.gpif"))
            root = ET.fromstring(z.read(name))
    except (zipfile.BadZipFile, StopIteration, ET.ParseError) as exc:
        raise ValueError(f"Not a valid Guitar Pro 7/8 file: {exc}") from exc

    bars = _index(root.find("Bars"), "Bar")
    voices = _index(root.find("Voices"), "Voice")
    beats = _index(root.find("Beats"), "Beat")
    notes = _index(root.find("Notes"), "Note")
    rhythms = _index(root.find("Rhythms"), "Rhythm")
    masters = root.findall("MasterBars/MasterBar")

    # ---- tempo + bar timeline
    tempo_at: dict[int, float] = {}
    for a in root.iter("Automation"):
        if a.findtext("Type") == "Tempo":
            val = (a.findtext("Value") or "120 2").split()
            bar = int(float(a.findtext("Bar") or 0))
            if float(a.findtext("Position") or 0) > 0:
                bar += 1                                   # mid-bar change: apply from next bar
            tempo_at[bar] = max(30.0, float(val[0]) * _TEMPO_UNIT.get(int(val[1]) if len(val) > 1 else 2, 1.0))
    bar_times, bar_bpm, bar_q, t, bpm = [], [], [], 0.0, 120.0
    for i, mb in enumerate(masters):
        bpm = tempo_at.get(i, bpm)
        num, den = (int(x) for x in (mb.findtext("Time") or "4/4").split("/"))
        q = num * 4.0 / den
        bar_times.append(t)
        bar_bpm.append(bpm)
        bar_q.append(q)
        t += q * 60.0 / bpm
    duration = t

    # ---- tracks (master-bar "Bars" lists one bar id per staff, in track order)
    tracks: list[Track] = []
    staff_base = 0
    for tr in root.findall("Tracks/Track"):
        staves = tr.findall("Staves/Staff")
        sp = _props(staves[0]) if staves else {}
        pitches = _ints(sp["Tuning"].findtext("Pitches")) if "Tuning" in sp else _STANDARD
        tuning = list(reversed(pitches))                      # high -> low
        capo = int(sp["CapoFret"].findtext("Fret") or 0) if "CapoFret" in sp else 0
        perc = tr.findtext("InstrumentSet/Type") == "drumKit"
        arts = [int(a.findtext("OutputMidiNumber") or 38)
                for a in tr.findall("InstrumentSet/Elements/Element/Articulations/Articulation")]
        program = int(tr.findtext("GeneralMidi/Program") or 25)

        events: dict[tuple[int, int], Event] = {}
        sustain: dict[int, Event] = {}
        for bi, mb in enumerate(masters):
            ids = _ints(mb.findtext("Bars"))
            if staff_base >= len(ids):
                continue
            bar = bars.get(str(ids[staff_base]))
            if bar is None:
                continue
            spq = 60.0 / bar_bpm[bi]
            for vid in _ints(bar.findtext("Voices")):
                voice = voices.get(str(vid))
                if voice is None:
                    continue
                offset = 0.0
                for beat_id in _ints(voice.findtext("Beats")):
                    beat = beats[str(beat_id)]
                    if beat.find("GraceNotes") is not None:
                        continue
                    r = rhythms[beat.find("Rhythm").get("ref")]
                    dq = _VALUES.get(r.findtext("NoteValue"), 1.0)
                    dots = int(r.find("AugmentationDot").get("count", 1)) if r.find("AugmentationDot") is not None else 0
                    dq *= 2.0 - 0.5 ** dots
                    tup = r.find("PrimaryTuplet")
                    if tup is not None:
                        dq *= int(tup.get("den", 1)) / max(1, int(tup.get("num", 1)))
                    start, dur = bar_times[bi] + offset * spq, dq * spq

                    for nid in _ints(beat.findtext("Notes")):
                        n = notes[str(nid)]
                        p = _props(n)
                        tie = n.find("Tie")
                        string = int(p["String"].findtext("String")) if "String" in p else 0
                        if tie is not None and tie.get("destination") == "true" and string in sustain:
                            sustain[string].duration += dur
                            continue
                        if perc:
                            art = int(n.findtext("InstrumentArticulation") or 0)
                            midi, fret, row = (arts[art] if art < len(arts) else 38), 0, 0
                        else:
                            fret = int(p["Fret"].findtext("Fret")) if "Fret" in p else 0
                            if string >= len(tuning):
                                continue
                            row = len(tuning) - 1 - string                # lowest string -> bottom row
                            midi = tuning[row] + fret + capo
                        ev = events.setdefault((bi, round(offset * 960)), Event(start, dur, [], bi))
                        ev.duration = max(ev.duration, dur)
                        ev.notes.append(Note(row, fret, midi, "Muted" in p or "Dead" in p))
                        sustain[string] = ev
                    offset += dq
        staff_base += max(1, len(staves))

        evs = sorted(events.values(), key=lambda e: e.time)
        for e in evs:
            e.notes.sort(key=lambda n: n.string)
        tracks.append(Track((tr.findtext("Name") or "Track").strip(), tuning, evs, perc, program))

    title = (root.findtext("Score/Title") or "").strip() or Path(path).stem
    return Song(title, (root.findtext("Score/Artist") or "").strip(), tracks, bar_times, duration,
                bar_bpm[0] if bar_bpm else 120.0)
