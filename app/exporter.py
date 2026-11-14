"""Exports: Guitar Pro 5 (.gp5), Guitar Pro 7/8 (.gp), MIDI (.mid) and JSON.

Every format can be written from the internal `Song` model, so exporting works
for any loaded song (local file or Songsterr). When the song came from Songsterr
the raw note JSON is still available, and .gp5 export prefers it: the
songsterr_convert rebuild keeps bends, slides, tuplets, voices, ... intact.

GP5 paths, in order of fidelity:
  1. Songsterr note JSON       -> songsterr_convert (faithful rebuild)
  2. loaded .gp3/.gp4/.gp5     -> re-parsed, selected tracks kept, re-saved
  3. the internal Song model   -> rebuilt with PyGuitarPro (notes/rhythm/tuning)
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .models import Song

GM_FAMILIES = [  # (program range, label) - General MIDI instrument families
    ((0, 7), "Piano"), ((8, 15), "Chrom. Perc."), ((16, 23), "Organ"),
    ((24, 31), "Guitar"), ((32, 39), "Bass"), ((40, 47), "Strings"),
    ((48, 55), "Ensemble"), ((56, 63), "Brass"), ((64, 71), "Reed"),
    ((72, 79), "Pipe"), ((80, 87), "Synth Lead"), ((88, 95), "Synth Pad"),
    ((96, 103), "Synth FX"), ((104, 111), "Ethnic"), ((112, 119), "Percussion"),
    ((120, 127), "Sound FX"),
]

MIN_QN = 3 / 64            # shortest representable length: dotted 128th (quarters)
_EPS = 0.02
_MELODIC_CHANNELS = [c for c in range(16) if c != 9]   # ch 9 = drums
_NOTE_VALUES = {1: "Whole", 2: "Half", 4: "Quarter", 8: "Eighth",
                16: "16th", 32: "32nd", 64: "64th", 128: "128th"}


def track_kind(tr) -> str:
    """Human label for a Track of the internal model."""
    if tr.percussion:
        return "Drums"
    if 32 <= tr.program <= 39:
        return "Bass"
    for (lo, hi), name in GM_FAMILIES:
        if lo <= tr.program <= hi:
            return name
    return "Guitar"


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "song"


def _ids(song: Song, track_ids) -> list[int]:
    if track_ids is None:
        return list(range(len(song.tracks)))
    out = sorted({int(i) for i in track_ids})
    return [i for i in out if 0 <= i < len(song.tracks)]


# --------------------------------------------------------------- bar layout
def _bar_layout(song: Song):
    """(start, end) seconds of every bar."""
    n = len(song.bar_times)
    return [(song.bar_times[i],
             song.bar_times[i + 1] if i + 1 < n else max(song.duration, song.bar_times[i]))
            for i in range(n)]


def _time_sig(bar_qn: float) -> tuple[int, int]:
    """Guess (numerator, denominator) from the bar length in quarter notes."""
    for den in (4, 8, 2, 16, 32):
        num = bar_qn * den / 4
        rn = round(num)
        if 1 <= rn <= 32 and abs(num - rn) < 0.01:
            return int(rn), den
    return 4, 4


_TUPLET_RATIOS = ((1, 1), (2, 3), (3, 2))   # (enters, times): plain, duplet, triplet
_GP5_RATIOS = ((1, 1), (3, 2))  # GP5's binary format can only store triplets


def _quantize(qn: float, max_dots: int = 2, ratios=_TUPLET_RATIOS) -> tuple[int, int, tuple[int, int]]:
    """Nearest (GP note value, dots, tuplet) for a length in quarter notes.

    GP values: 1=whole ... 128=128th. Returns e.g. (4, 1, (1, 1)) = dotted
    quarter, or (4, 0, (3, 2)) = quarter triplet. Plain (untupleted) lengths
    win ties, so tuplets only appear when they genuinely fit better.
    """
    def best_for(ratios_):
        best, best_err = None, None
        for value in _NOTE_VALUES:
            base = 4.0 / value
            for dots in range(max_dots + 1):
                dotted = base * (2.0 - 0.5 ** dots)
                for enters, times in ratios_:
                    cand = dotted * times / enters
                    err = abs(cand - qn)
                    if best_err is None or err < best_err - 1e-9:
                        best, best_err = (value, dots, (enters, times)), err
        return best, best_err

    plain, plain_err = best_for(((1, 1),))
    tup, tup_err = best_for([r for r in ratios if r != (1, 1)])
    if tup is not None and (plain is None or tup_err < plain_err - 1e-9):
        return tup
    return plain


def _nominal_qn(bar_qn: float) -> float:
    """Musical bar length used by the GP exports, in quarter notes.

    Bars whose length matches a clean signature keep it; bars distorted by
    mid-song tempo changes (the model stores seconds, not a tempo map) are
    stretched to the next whole quarter so no notes have to be dropped.
    """
    num, den = _time_sig(bar_qn)
    if abs(num * 4.0 / den - bar_qn) < 0.06:
        return num * 4.0 / den
    import math
    return float(max(1, math.ceil(bar_qn - 0.02)))


def _dur_qn(dur: tuple[int, int, tuple[int, int]]) -> float:
    value, dots, (enters, times) = dur
    return 4.0 / value * (2.0 - 0.5 ** dots) * times / enters


def _plan_voice(song: Song, tr, bar: int, bar_start: float, bar_end: float,
                bar_qn: float, spq: float, max_dots: int = 2,
                ratios=_TUPLET_RATIOS):
    """One voice's beats for a bar: ("rest", dur) / ("notes", event, dur).

    GP formats are beat-sequential, so gaps become rests and every length is
    quantized to a representable note value. Beats are refitted so they never
    overflow the bar. MIDI/JSON exports use the exact times; only the two GP
    formats go through this. ``max_dots`` is 1 for GP5 (no double dots there)
    and GP5's binary format only stores triplets, hence the smaller ``ratios``.
    """
    evs = sorted((e for e in tr.events if e.bar == bar), key=lambda e: e.time)
    plan, cursor = [], 0.0
    for ev in evs:
        off = min(max(0.0, (ev.time - bar_start) / spq), max(0.0, bar_qn - _EPS))
        if off > cursor + _EPS:                          # gap -> rest beat
            dur = _quantize(off - cursor, max_dots, ratios)
            plan.append(("rest", dur))
            cursor += _dur_qn(dur)
        if cursor >= bar_qn - _EPS:
            break
        len_qn = min(ev.duration / spq, bar_qn - cursor)
        if len_qn < _EPS:
            continue
        dur = _quantize(len_qn, max_dots, ratios)
        if cursor + _dur_qn(dur) > bar_qn + _EPS:        # refit into the bar
            dur = _quantize_fitting(bar_qn - cursor, max_dots, ratios)
        plan.append(("notes", ev, dur))
        cursor += _dur_qn(dur)
    if cursor < bar_qn - _EPS:                           # fill the rest of the bar
        plan.append(("rest", _quantize_fitting(bar_qn - cursor, max_dots, ratios)))
    return plan


def _quantize_fitting(qn: float, max_dots: int = 2, ratios=_TUPLET_RATIOS) -> tuple[int, int, tuple[int, int]]:
    """Nearest representable length that does not exceed ``qn``."""
    best, best_err = None, None
    for value in _NOTE_VALUES:
        base = 4.0 / value
        for dots in range(max_dots + 1):
            dotted = base * (2.0 - 0.5 ** dots)
            for enters, times in ratios:
                cand = dotted * times / enters
                if cand > qn + _EPS:
                    continue
                err = abs(cand - qn)
                if best_err is None or err < best_err - 1e-9:
                    best, best_err = (value, dots, (enters, times)), err
    return best or (128, 0, (1, 1))

def _drum_layout(midis) -> tuple[dict, dict]:
    """Percussion map: midi -> articulation index and midi -> staff line."""
    pref = {35: 0, 36: 0,                                  # kick
            41: 1, 43: 1,                                  # low toms
            37: 2, 38: 2, 39: 2, 40: 2,                    # snare / cross-stick
            45: 3, 47: 3,                                  # mid toms
            42: 4, 44: 4, 46: 4,                           # hi-hats
            49: 5, 57: 5, 52: 5, 55: 5,                    # crashes
            51: 6, 53: 6, 59: 6}                           # rides
    art, lines, used = {}, {}, set()
    for k, m in enumerate(sorted(midis)):
        line = pref.get(m)
        if line is None or line in used:
            line = next((l for l in range(8) if l not in used), 0)
        art[m], lines[m], _ = k, line, used.add(line)
    return art, lines


# ------------------------------------------------------------------- MIDI
def _vlq(n: int) -> bytes:
    out = bytearray([n & 0x7F])
    n >>= 7
    while n:
        out.insert(0, 0x80 | (n & 0x7F))
        n >>= 7
    return bytes(out)


def _chunk(tag: bytes, data: bytes) -> bytes:
    return tag + len(data).to_bytes(4, "big") + data


def export_midi(song: Song, path, track_ids=None) -> None:
    """Standard MIDI file (format 1): tempo/meta track + one track per song track."""
    ids = _ids(song, track_ids)
    if not ids:
        raise ValueError("No tracks selected.")
    ppq = 480
    tps = ppq * max(1.0, song.tempo) / 60.0                # ticks per second

    def ticks(t: float) -> int:
        return max(0, int(round(t * tps)))

    name = (f"{song.artist} - {song.title}" if song.artist else song.title)[:120]
    data = bytearray(b"\x00\xff\x03" + _vlq(len(name.encode())) + name.encode("latin-1", "replace"))
    data += b"\x00\xff\x51\x03" + int(round(60_000_000 / max(1.0, song.tempo))).to_bytes(3, "big")
    data += b"\x00\xff\x58\x04" + bytes((4, 2, 24, 8))
    data += b"\x00\xff\x2f\x00"
    tracks = [bytes(data)]

    melodic_channels = [c for c in range(16) if c != 9]    # ch 9 = drums
    for ti in ids:
        tr = song.tracks[ti]
        ch = 9 if tr.percussion else melodic_channels[(len(tracks) - 1) % len(melodic_channels)]
        evs: list[tuple[int, int, bytes]] = []             # (tick, off-before-on, bytes)
        tname = (tr.name or f"Track {ti + 1}")[:120]
        data = bytearray(b"\x00\xff\x03" + _vlq(len(tname.encode())) + tname.encode("latin-1", "replace"))
        if not tr.percussion:
            data += b"\x00" + bytes((0xC0 | ch, max(0, min(127, tr.program))))
        for ev in tr.events:
            on, off_t = ticks(ev.time), ticks(ev.time + max(ev.duration, 0.005))
            for n in ev.notes:
                if n.dead:
                    continue
                pitch = max(0, min(127, n.midi))
                evs.append((on, 1, bytes((0x90 | ch, pitch, 100))))
                evs.append((max(on + 1, off_t), 0, bytes((0x80 | ch, pitch, 0))))
        last = 0
        for tick, _, msg in sorted(evs, key=lambda x: (x[0], x[1])):
            data += _vlq(tick - last) + msg
            last = tick
        data += b"\x00\xff\x2f\x00"
        tracks.append(bytes(data))

    with open(path, "wb") as fh:
        fh.write(_chunk(b"MThd", (1).to_bytes(2, "big") + len(tracks).to_bytes(2, "big")
                        + ppq.to_bytes(2, "big")))
        for t in tracks:
            fh.write(_chunk(b"MTrk", t))


# ------------------------------------------------------------------- JSON
def export_json(song: Song, path, track_ids=None) -> None:
    """Human-readable dump of the internal model (title, artist, tracks, notes)."""
    ids = _ids(song, track_ids)
    if not ids:
        raise ValueError("No tracks selected.")

    def r(x: float) -> float:
        return round(float(x), 4)

    data = {
        "title": song.title, "artist": song.artist,
        "tempo": r(song.tempo), "duration": r(song.duration),
        "bars": len(song.bar_times), "tracks": [],
    }
    for ti in ids:
        tr = song.tracks[ti]
        t = {
            "index": ti, "name": tr.name, "kind": track_kind(tr),
            "percussion": tr.percussion, "program": tr.program,
            "tuning": list(tr.tuning), "events": [],
        }
        for ev in tr.events:
            t["events"].append({
                "time": r(ev.time), "duration": r(ev.duration), "bar": ev.bar,
                "notes": [{"string": n.string, "fret": n.fret, "midi": n.midi,
                           "dead": n.dead} for n in ev.notes],
            })
        data["tracks"].append(t)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


# ---------------------------------------------------- Guitar Pro 7/8 (.gp)
_GP_COLORS = ["246 74 38", "117 201 227", "184 216 91", "223 104 43", "82 140 191",
              "129 206 189", "212 104 104", "94 89 158", "200 138 20", "32 116 178"]


def _et(parent, tag, text=None, **attrib):
    el = ET.SubElement(parent, tag, attrib)
    if text is not None:
        el.text = str(text)
    return el


def export_gp(song: Song, path, track_ids=None) -> None:
    """Guitar Pro 7/8 (.gp): a zip holding Content/score.gpif (XML), built with
    the standard library - the mirror image of gpif_loader."""
    ids = _ids(song, track_ids)
    if not ids:
        raise ValueError("No tracks selected.")
    spq = 60.0 / max(1.0, song.tempo)                      # seconds per quarter
    layout = _bar_layout(song)
    sel = [song.tracks[i] for i in ids]

    # percussion tracks: pre-compute articulation index + staff line per drum
    drum_maps: dict[int, tuple[dict, dict]] = {}
    for i, tr in zip(ids, sel):
        if tr.percussion:
            drum_maps[i] = _drum_layout({n.midi for ev in tr.events for n in ev.notes})

    rhythm_ids: dict[tuple, int] = {}
    rhythm_els, beat_els, note_els = [], [], []
    voice_els, bar_els, mbar_els = [], [], []
    n_rhythm = n_beat = n_note = n_voice = n_bar = 0

    for bi, (start, end) in enumerate(layout):
        bar_qn = _nominal_qn(max(_EPS, (end - start) / spq))
        num, den = _time_sig(bar_qn)
        mbar = ET.Element("MasterBar")
        key = _et(mbar, "Key")
        _et(key, "AccidentalCount", 0)
        _et(key, "Mode", "Major")
        _et(mbar, "Time", f"{num}/{den}")
        bars_of_bar = []
        for ti, tr in zip(ids, sel):
            beat_ids = []
            plan = _plan_voice(song, tr, bi, start, end, bar_qn, spq)
            for item in plan:
                dur = item[-1]
                if dur not in rhythm_ids:
                    rhythm_ids[dur] = n_rhythm
                    n_rhythm += 1
                    r = ET.Element("Rhythm", id=str(rhythm_ids[dur]))
                    _et(r, "NoteValue", _NOTE_VALUES[dur[0]])
                    if dur[1]:
                        _et(r, "AugmentationDot", count=str(dur[1]))
                    if dur[2] != (1, 1):
                        _et(r, "PrimaryTuplet", num=str(dur[2][0]), den=str(dur[2][1]))
                    rhythm_els.append(r)
                beat = ET.Element("Beat", id=str(n_beat))
                n_beat += 1
                _et(beat, "Rhythm", ref=str(rhythm_ids[dur]))
                note_refs = []
                if item[0] == "notes":
                    for n in item[1].notes:
                        note = ET.Element("Note", id=str(n_note))
                        n_note += 1
                        props = _et(note, "Properties")
                        if tr.percussion:
                            art, lines = drum_maps[ti]
                            art_idx, line = art[n.midi], lines[n.midi]
                            _et(note, "InstrumentArticulation", art_idx)
                            p = _et(props, "Property", name="String")
                            _et(p, "String", line)
                            p = _et(props, "Property", name="Midi")
                            _et(p, "Number", max(0, min(127, n.midi)))
                        else:
                            _et(note, "InstrumentArticulation", 0)
                            if n.dead:
                                _et(props, "Property", name="Muted")
                            p = _et(props, "Property", name="Fret")
                            _et(p, "Fret", max(0, min(40, n.fret)))
                            p = _et(props, "Property", name="Midi")
                            _et(p, "Number", max(0, min(127, n.midi)))
                            p = _et(props, "Property", name="String")
                            _et(p, "String", max(0, len(tr.tuning) - 1 - n.string))
                        note_els.append(note)
                        note_refs.append(note.get("id"))
                _et(beat, "Notes", " ".join(note_refs))
                beat_els.append(beat)
                beat_ids.append(beat.get("id"))
            voice = ET.Element("Voice", id=str(n_voice))
            n_voice += 1
            _et(voice, "Beats", " ".join(beat_ids))
            voice_els.append(voice)
            bars_of_bar.append(str(n_bar))
            bar = ET.Element("Bar", id=str(n_bar))
            n_bar += 1
            _et(bar, "Clef", "Neutral" if tr.percussion
                else ("F4" if track_kind(tr) == "Bass" else "G2"))
            _et(bar, "Voices", f"{voice.get('id')} -1 -1 -1")
            bar_els.append(bar)
        _et(mbar, "Bars", " ".join(bars_of_bar))
        mbar_els.append(mbar)

    root = ET.Element("GPIF")
    _et(root, "GPVersion", 7)
    _et(root, "GPRevision", 12020)
    enc = _et(root, "Encoding")
    _et(enc, "EncodingDescription", "GP7")
    score = _et(root, "Score")
    for tag, text in (("Title", song.title), ("SubTitle", ""), ("Artist", song.artist),
                      ("Album", ""), ("Words", ""), ("Music", ""), ("WordsAndMusic", ""),
                      ("Copyright", "Exported from Guitar Practice"), ("Tabber", ""),
                      ("Instructions", ""), ("Notices", "")):
        _et(score, tag, text or "")
    master = _et(root, "MasterTrack")
    _et(master, "Tracks", " ".join(str(k) for k in range(len(ids))))
    autos = _et(master, "Automations")
    a = _et(autos, "Automation")
    _et(a, "Type", "Tempo")
    _et(a, "Linear", "false")
    _et(a, "Bar", 0)
    _et(a, "Position", 0)
    _et(a, "Visible", "true")
    _et(a, "Value", f"{int(round(max(1.0, song.tempo)))} 2")

    tracks_el = _et(root, "Tracks")
    for k, (ti, tr) in enumerate(zip(ids, sel)):
        color = _GP_COLORS[k % len(_GP_COLORS)]
        t = _et(tracks_el, "Track", id=str(k))
        _et(t, "Name", tr.name or f"Track {k + 1}")
        _et(t, "ShortName", (tr.name or f"T{k + 1}")[:6])
        _et(t, "Color", color)
        _et(t, "SystemsDefautLayout", 3)
        _et(t, "PlayingStyle", "Default")
        iset = _et(t, "InstrumentSet")
        _et(iset, "Name", tr.name or f"Track {k + 1}")
        _et(iset, "Type", "drumKit" if tr.percussion
            else ("electricBass" if track_kind(tr) == "Bass" else "electricGuitar"))
        _et(iset, "LineCount", 5)
        elements = _et(iset, "Elements")
        element = _et(elements, "Element")
        arts = _et(element, "Articulations")
        if tr.percussion:
            art, lines = drum_maps[ti]
            for midi in sorted(art):
                a_el = _et(arts, "Articulation")
                _et(a_el, "StaffLine", lines[midi])
                _et(a_el, "OutputMidiNumber", max(0, min(127, midi)))
        else:
            a_el = _et(arts, "Articulation")
            _et(a_el, "StaffLine", 0)
            _et(a_el, "OutputMidiNumber", max(0, min(127, tr.program)))
        trans = _et(t, "Transpose")
        _et(trans, "Chromatic", 0)
        _et(trans, "Octave", 0)
        gm = _et(t, "GeneralMidi")
        prog = 0 if tr.percussion else max(0, min(127, tr.program))
        _et(gm, "Program", prog)
        _et(gm, "Programs", prog)
        mc = _et(t, "MidiConnection")
        _et(mc, "Port", 0)
        channel = 9 if tr.percussion else [c for c in range(16) if c != 9][k % 15]
        _et(mc, "PrimaryChannel", channel)
        _et(mc, "SecondaryChannel", channel)
        _et(mc, "ForeOneChannelPerString", "false")
        staves = _et(t, "Staves")
        staff = _et(staves, "Staff")
        props = _et(staff, "Properties")
        p = _et(props, "Property", name="CapoFret")
        _et(p, "Fret", 0)
        p = _et(props, "Property", name="FretCount")
        _et(p, "Number", 24)
        p = _et(props, "Property", name="Tuning")
        tuning = [0] * 6 if tr.percussion else sorted(tr.tuning)   # low -> high (GPIF)
        _et(p, "Pitches", " ".join(str(x) for x in tuning))

    mbars = ET.Element("MasterBars")
    for el in mbar_els:
        mbars.append(el)
    root.append(mbars)
    for name, group in (("Bars", bar_els), ("Voices", voice_els), ("Beats", beat_els),
                        ("Rhythms", rhythm_els), ("Notes", note_els)):
        holder = ET.Element(name)
        for item in group:
            holder.append(item)
        root.append(holder)

    xml = '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("VERSION", "7.0")
        z.writestr("Content/score.gpif", xml)


# ------------------------------------------------------- Guitar Pro 5 (.gp5)
def export_gp5(song: Song, path, track_ids=None, source_path=None,
               st_meta=None, st_parts=None) -> str:
    """Write a .gp5. Returns the path used: 'songsterr' | 'source' | 'model'."""
    ids = _ids(song, track_ids)
    if not ids:
        raise ValueError("No tracks selected.")
    if st_meta is not None and st_parts is not None:
        from . import songsterr_convert
        songsterr_convert.write_gp5(st_meta, st_parts, path, include=ids)
        return "songsterr"
    if source_path and Path(source_path).suffix.lower() in (".gp3", ".gp4", ".gp5"):
        filter_gp_file(source_path, path, ids)
        return "source"
    import guitarpro as gp
    gp.write(_song_to_gp5(song, ids), str(path))
    return "model"


def filter_gp_file(source_path, out_path, track_ids) -> None:
    """Re-save a .gp3/.gp4/.gp5 keeping only the given track indices."""
    import guitarpro as gp

    src = gp.parse(str(source_path))
    keep = sorted(int(i) for i in track_ids)
    tracks = [src.tracks[i] for i in keep if i < len(src.tracks)]
    if not tracks:
        raise ValueError("No selected track found in the file.")
    for k, tr in enumerate(tracks):
        tr.number = k + 1
    src.tracks = tracks
    gp.write(src, str(out_path))


def _song_to_gp5(song: Song, ids: list[int]):
    """Rebuild a PyGuitarPro song from the internal model (notes, rhythm, tuning)."""
    from guitarpro import models as m

    gsong = m.Song()
    gsong.tracks = []                     # Song() auto-creates a default track
    gsong.title = song.title or ""
    gsong.artist = song.artist or ""
    gsong.subtitle = ""
    gsong.tempo = int(round(max(1.0, song.tempo)))
    spq = 60.0 / gsong.tempo
    layout = _bar_layout(song)

    headers = []
    for start, end in layout:
        bar_qn = max(_EPS, (end - start) / spq)
        num, den = _time_sig(bar_qn)
        hdr = m.MeasureHeader(number=len(headers) + 1)
        hdr.timeSignature = m.TimeSignature(numerator=num, denominator=m.Duration(value=den))
        headers.append(hdr)
    gsong.measureHeaders = headers

    for k, ti in enumerate(ids):
        tr = song.tracks[ti]
        track = m.Track(gsong, number=k + 1)
        track.name = (tr.name or f"Track {k + 1}").strip() or f"Track {k + 1}"
        drum = tr.percussion
        track.isPercussionTrack = drum
        channel = 9 if drum else _MELODIC_CHANNELS[k % len(_MELODIC_CHANNELS)]
        track.channel.channel = channel
        track.channel.effectChannel = channel
        if drum:
            track.strings = [m.GuitarString(i + 1, 0) for i in range(6)]
            track.channel.instrument = 0
        else:
            tuning = list(tr.tuning)[:7]                   # GP5 supports up to 7 strings
            if len(tuning) < 4:                            # keep a playable staff
                tuning = tuning + [40, 45, 50, 55, 59, 64][len(tuning):]
            track.strings = [m.GuitarString(i + 1, v) for i, v in enumerate(tuning)]
            track.channel.instrument = max(0, min(127, tr.program))
        track.fretCount = 24

        track.measures = []
        n_strings = len(track.strings)
        for bi, hdr in enumerate(headers):
            start, end = layout[bi]
            bar_qn = _nominal_qn(max(_EPS, (end - start) / spq))
            measure = m.Measure(track, hdr)
            measure.voices = []
            voice = m.Voice(measure)
            voice.beats = []
            plan = _plan_voice(song, tr, bi, start, end, bar_qn, spq,
                               max_dots=1, ratios=_GP5_RATIOS)
            if not plan:
                beat = m.Beat(voice)                     # whole rest = full-bar rest
                beat.duration = _gp_duration(3840, 1, 0)
                beat.status = m.BeatStatus.rest
                voice.beats.append(beat)
            for item in plan:
                value, dots, tuplet = item[-1]
                beat = m.Beat(voice)
                beat.duration = _gp_duration(int(round(_dur_qn(item[-1]) * 960)),
                                             value, dots, tuplet)
                if item[0] == "notes":
                    beat.status = m.BeatStatus.normal
                    used: set[int] = set()
                    for n in item[1].notes:
                        gp_string = (n.midi % n_strings) + 1 if drum else n.string + 1
                        if gp_string in used or not 1 <= gp_string <= n_strings:
                            gp_string = next((c for c in range(1, n_strings + 1)
                                              if c not in used), 0)
                            if not gp_string:
                                continue
                        used.add(gp_string)
                        note = m.Note(beat=None)
                        note.value = max(0, min(40, n.fret))
                        note.string = gp_string
                        note.velocity = m.Velocities.default
                        note.type = m.NoteType.dead if n.dead else m.NoteType.normal
                        note.beat = beat
                        beat.notes.append(note)
                else:
                    beat.status = m.BeatStatus.rest
                voice.beats.append(beat)
            measure.voices.append(voice)
            for _ in range(m.Measure.maxVoices - 1):
                empty = m.Voice(measure)
                empty.beats = []
                measure.voices.append(empty)
            track.measures.append(measure)
        gsong.tracks.append(track)
    return gsong


def _gp_duration(ticks: int, value: int, dots: int, tuplet=(1, 1)):
    from guitarpro import models as m

    d = m.Duration(value=value, tuplet=m.Tuplet(*tuplet))
    d.isDotted = dots >= 1                      # pyguitarpro has no double dots
    if int(d.time) != int(ticks):
        try:
            alt = m.Duration.fromTime(int(ticks))
            if abs(int(alt.time) - int(ticks)) <= abs(int(d.time) - int(ticks)):
                d = alt
        except Exception:
            pass
    return d
