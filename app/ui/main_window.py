"""Main window: toolbar, Songsterr bar, TAB view, HUD and a collapsible settings dock."""
from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, Qt, QTimer, QThread, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDockWidget, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QProgressBar, QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget)

from .. import exporter, songsterr, songsterr_convert
from ..audio_in import PitchTracker
from ..audio_out import BackingPlayer, render_backing
from ..engine import Mode, PracticeEngine
from ..gp_loader import load_song
from ..models import midi_name
from ..settings import Settings
from .tab_view import TabView


class _FetchThread(QThread):
    """Downloads Songsterr metadata + note JSON off the UI thread."""

    done = Signal(object, object)
    fail = Signal(str)

    def __init__(self, url, parent=None):
        super().__init__(parent)
        self.url = url

    def run(self):
        try:
            meta, parts = songsterr.fetch_song(self.url)
            self.done.emit(meta, parts)
        except Exception as exc:
            self.fail.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Guitar Practice")
        self.resize(1280, 740)
        self.s = Settings.load()
        self.tracker = PitchTracker(self.s)
        self.player = BackingPlayer()
        self.player.volume = self.s.backing_volume / 100
        self.engine = PracticeEngine(self.s, self.player, self.tracker)
        self.song = None
        self._track_ids: list[int] = []
        self._backing_dirty = True
        self._source_path: str | None = None      # local file the song came from
        self._st_meta = self._st_parts = None     # Songsterr fetch results
        self._st_url: str | None = None
        self._st_for_song = None                  # song object the fetch belongs to
        self._fetch: _FetchThread | None = None
        self._pending_load = False
        self._track_checks: list[QCheckBox] = []

        self._build_ui()
        self._build_settings_dock()
        QShortcut(QKeySequence(Qt.Key_Space), self, activated=self._toggle_play)
        QShortcut(QKeySequence(Qt.Key_Home), self, activated=self._restart)

        errors = [e for e in (self.tracker.start(), self.player.open()) if e]
        self.statusBar().showMessage("  ".join(errors) or "Ready - open a Guitar Pro file to begin.")

        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self, interval=16, timeout=self._on_tick)
        self._timer.start()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 8)
        root.setSpacing(10)

        top = QFrame(objectName="bar")
        row = QHBoxLayout(top)
        self.btn_open = QPushButton("Open TAB…", clicked=self._open_file)
        self.lbl_song = QLabel("No song loaded", objectName="muted")
        self.combo_track = QComboBox(minimumWidth=190)
        self.combo_track.currentIndexChanged.connect(self._on_track_changed)
        self.combo_mode = QComboBox()
        self.combo_mode.addItems([m.value for m in Mode])
        self.combo_mode.currentIndexChanged.connect(self._on_mode_changed)
        self.spin_speed = QSpinBox(minimum=25, maximum=150, singleStep=5, value=100, suffix=" %")
        self.spin_speed.valueChanged.connect(self._on_speed_changed)
        self.btn_play = QPushButton("▶  Play", objectName="primary", clicked=self._toggle_play)
        self.btn_restart = QPushButton("⟲  Restart", clicked=self._restart)
        for wdg in (self.btn_open, self.lbl_song):
            row.addWidget(wdg)
        row.addStretch(1)
        for wdg in (QLabel("Track"), self.combo_track, QLabel("Mode"), self.combo_mode, QLabel("Speed"), self.spin_speed, self.btn_restart, self.btn_play):
            row.addWidget(wdg)
        root.addWidget(top)

        # ------------------------------------------------ Songsterr bar
        st = QFrame(objectName="bar")
        srow = QHBoxLayout(st)
        self.edit_url = QLineEdit()
        self.edit_url.setPlaceholderText("Songsterr link or song id - e.g. https://www.songsterr.com/a/wsa/\u2026-s4540t2")
        self.edit_url.returnPressed.connect(self._songsterr_to_practice)
        self.btn_fetch = QPushButton("\u2913  Fetch", clicked=self._fetch_songsterr)
        self.btn_fetch.setToolTip("Download the song info and track list from Songsterr")
        self.lbl_st_info = QLabel("", objectName="muted")
        self.btn_load_st = QPushButton("Load for practice", clicked=self._songsterr_to_practice)
        self.btn_load_st.setToolTip("Convert the Songsterr tab to Guitar Pro and open it for practising")
        srow.addWidget(QLabel("Songsterr"))
        srow.addWidget(self.edit_url, 1)
        srow.addWidget(self.btn_fetch)
        srow.addWidget(self.lbl_st_info)
        srow.addWidget(self.btn_load_st)
        root.addWidget(st)

        # ------------------------------------------------ export bar
        self.ex_row = QFrame(objectName="bar")
        erow = QHBoxLayout(self.ex_row)
        erow.addWidget(QLabel("Export tracks"))
        erow.addStretch(1)
        erow.addWidget(QLabel("Export"))
        self.btn_export = QPushButton("Save as\u2026")
        menu = QMenu(self.btn_export)
        menu.addAction("Guitar Pro 5  (.gp5)", lambda: self._export("gp5"))
        menu.addAction("Guitar Pro 7/8  (.gp)", lambda: self._export("gp"))
        menu.addAction("MIDI  (.mid)", lambda: self._export("mid"))
        menu.addAction("JSON  (.json)", lambda: self._export("json"))
        self.btn_export.setMenu(menu)
        self.btn_export.setEnabled(False)
        erow.addWidget(self.btn_export)
        self.ex_row.setVisible(False)
        root.addWidget(self.ex_row)

        self.view = TabView(self.engine, self.s)
        root.addWidget(self.view, 1)

        self.slider = QSlider(Qt.Horizontal, maximum=1000)
        self.slider.sliderReleased.connect(self._on_seek)
        root.addWidget(self.slider)

        hud = QFrame(objectName="bar")
        h = QHBoxLayout(hud)
        self.lbl_note = QLabel("—", objectName="note", minimumWidth=90)
        self.level = QProgressBar(textVisible=False, maximumWidth=140, maximum=100)
        self.lbl_stats = QLabel("")
        self.lbl_pos = QLabel("", objectName="muted")
        h.addWidget(QLabel("Heard", objectName="muted"))
        h.addWidget(self.lbl_note)
        h.addWidget(self.level)
        h.addSpacing(24)
        h.addWidget(self.lbl_stats, 1)
        h.addWidget(self.lbl_pos)
        root.addWidget(hud)
        self.setCentralWidget(central)

    def _spin(self, lo, hi, value, setter, suffix=""):
        sp = QSpinBox(minimum=lo, maximum=hi, value=value, suffix=suffix)
        sp.valueChanged.connect(setter)
        return sp

    def _build_settings_dock(self):
        s = self.s
        body = QWidget()
        lay = QVBoxLayout(body)

        g1 = QGroupBox("Input")
        f1 = QFormLayout(g1)
        self.combo_dev = QComboBox()
        self.combo_dev.addItem("System default", -1)
        for idx, name in PitchTracker.input_devices():
            self.combo_dev.addItem(name[:34], idx)
        self.combo_dev.setCurrentIndex(max(0, self.combo_dev.findData(s.input_device)))
        self.combo_dev.currentIndexChanged.connect(self._on_device_changed)
        f1.addRow("Microphone", self.combo_dev)
        f1.addRow("Noise gate", self._spin(-70, -20, int(s.gate_db), lambda v: setattr(s, "gate_db", float(v)), " dB"))
        f1.addRow("Pitch tolerance", self._spin(10, 100, s.cents_tolerance, lambda v: setattr(s, "cents_tolerance", v), " ¢"))
        oct_ = QCheckBox("Accept any octave", checked=s.ignore_octave)
        oct_.toggled.connect(lambda v: setattr(s, "ignore_octave", v))
        f1.addRow(oct_)
        lay.addWidget(g1)

        gc = QGroupBox("Chords")
        fc = QFormLayout(gc)
        chk = QCheckBox("Verify every chord note", checked=s.chord_check)
        chk.setToolTip("On: the strum's spectrum is checked against each note of the chord.\nOff: playing any one note of the chord counts.")
        chk.toggled.connect(lambda v: setattr(s, "chord_check", v))
        fc.addRow(chk)
        fc.addRow("Notes required", self._spin(40, 100, s.chord_required, lambda v: setattr(s, "chord_required", v), " %"))
        lay.addWidget(gc)

        g2 = QGroupBox("Auto Play")
        f2 = QFormLayout(g2)
        f2.addRow("Timing window ±", self._spin(60, 500, s.tolerance_ms, lambda v: setattr(s, "tolerance_ms", v), " ms"))
        f2.addRow("Latency offset", self._spin(0, 400, s.latency_ms, lambda v: setattr(s, "latency_ms", v), " ms"))
        f2.addRow("Start lead-in", self._spin(0, 10, s.lead_in_s, lambda v: setattr(s, "lead_in_s", v), " s"))
        f2.addRow("Mistake limit", self._spin(1, 50, s.max_mistakes, lambda v: setattr(s, "max_mistakes", v)))
        f2.addRow("Rewind span", self._spin(1, 16, s.rewind_bars, lambda v: setattr(s, "rewind_bars", v), " bars"))
        lay.addWidget(g2)

        g3 = QGroupBox("Playback & display")
        f3 = QFormLayout(g3)
        vol = QSlider(Qt.Horizontal, maximum=100, value=s.backing_volume)
        vol.valueChanged.connect(self._on_volume)
        f3.addRow("Backing volume", vol)
        inc = QCheckBox("Include selected track", checked=s.backing_include_selected)
        inc.toggled.connect(self._on_include_toggled)
        f3.addRow(inc)
        look = QDoubleSpinBox(minimum=1.5, maximum=15.0, singleStep=0.5, value=s.lookahead_s, suffix=" s")
        look.valueChanged.connect(lambda v: setattr(s, "lookahead_s", v))
        f3.addRow("View range (FOV)", look)
        f3.addRow("Target line pos.", self._spin(10, 60, s.target_pos, lambda v: setattr(s, "target_pos", v), " %"))
        f3.addRow("Capo fret", self._spin(0, 12, s.capo, lambda v: setattr(s, "capo", v)))
        lay.addWidget(g3)
        lay.addStretch(1)

        self.dock = QDockWidget("Settings", self)
        self.dock.setWidget(body)
        self.dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.addDockWidget(Qt.RightDockWidgetArea, self.dock)
        self.dock.setMinimumWidth(270)

    # ------------------------------------------------------------- actions
    @staticmethod
    def _track_label(name: str, kind: str) -> str:
        name = (name or "").strip() or kind
        return name if kind.lower() in name.lower() else f"{name}  ({kind})"

    def _set_track_checkboxes(self, labels: list[str]) -> None:
        prev = [cb.isChecked() for cb in self._track_checks]
        if getattr(self, "_track_holder", None) is not None:
            self._track_holder.deleteLater()
        holder = QWidget()
        h = QHBoxLayout(holder)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        self._track_checks = []
        for i, text in enumerate(labels):
            cb = QCheckBox(text, checked=(prev[i] if i < len(prev) else True))
            self._track_checks.append(cb)
            h.addWidget(cb)
        h.addStretch(1)
        self._track_holder = holder
        self.ex_row.layout().insertWidget(1, holder)
        self.ex_row.setVisible(True)

    def _selected_track_ids(self) -> list[int]:
        return [i for i, cb in enumerate(self._track_checks) if cb.isChecked()]

    def _set_song(self, song):
        """Install a loaded song: practice UI, track combo, export checkboxes."""
        self.song = song
        self.btn_export.setEnabled(True)
        self._track_ids = [i for i, t in enumerate(song.tracks) if not t.percussion and t.events]
        self.lbl_song.setText(f"{song.title}  ·  {song.artist}" if song.artist else song.title)
        self.combo_track.blockSignals(True)
        self.combo_track.clear()
        for i in self._track_ids:
            t = song.tracks[i]
            tag = "" if t.is_standard else "  (non-standard tuning)"
            self.combo_track.addItem(f"{t.name}{tag}", i)
        self.combo_track.blockSignals(False)
        if self._track_ids:
            self.combo_track.setCurrentIndex(0)
            self._on_track_changed()
        else:
            QMessageBox.information(self, "No guitar tracks",
                                    "No playable guitar/bass track found - exporting is still available.")
        self._set_track_checkboxes([self._track_label(t.name, exporter.track_kind(t))
                                    for t in song.tracks])

    def _open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open guitar TAB", "", "Guitar Pro files (*.gp *.gp3 *.gp4 *.gp5)")
        if not path:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        err = None
        try:
            song = load_song(path)
        except Exception as exc:
            err = exc
        finally:
            QApplication.restoreOverrideCursor()
        if err:
            QMessageBox.warning(self, "Cannot open file", str(err))
            return
        self._source_path = path
        self._st_for_song = None
        self._set_song(song)

    # ---------------------------------------------------------- songsterr
    def _fetch_songsterr(self):
        url = self.edit_url.text().strip()
        if not url:
            QMessageBox.information(self, "Songsterr", "Paste a Songsterr link or song id first.")
            return
        if self._fetch is not None and self._fetch.isRunning():
            return
        self._st_url = url
        self.btn_fetch.setEnabled(False)
        self.statusBar().showMessage("Fetching from Songsterr…")
        self._fetch = _FetchThread(url, self)
        self._fetch.done.connect(self._on_fetched)
        self._fetch.fail.connect(self._on_fetch_failed)
        self._fetch.start()

    def _on_fetched(self, meta, parts):
        self.btn_fetch.setEnabled(True)
        self._st_meta, self._st_parts = meta, parts
        title = meta.get("title") or "?"
        artist = meta.get("artist") or ""
        self.lbl_st_info.setText(f"{artist} — {title}" if artist else title)
        self._set_track_checkboxes(
            [self._track_label(t.get("name") or t.get("instrument") or f"Track {i + 1}",
                               songsterr.track_kind(t)) for i, t in enumerate(meta.get("tracks", []))])
        failed = sum(1 for p in parts if p is None)
        msg = f"Fetched '{title}' — {len(parts)} tracks."
        if failed:
            msg += f" ({failed} track(s) could not be downloaded.)"
        self.statusBar().showMessage(msg + " Pick tracks, then Load for practice or Export.", 10000)
        if self._pending_load:
            self._pending_load = False
            self._load_songsterr_song()

    def _on_fetch_failed(self, msg):
        self.btn_fetch.setEnabled(True)
        self._pending_load = False
        self.statusBar().showMessage("Songsterr fetch failed.", 6000)
        QMessageBox.warning(self, "Songsterr",
                            f"Could not fetch the song.\n\n{msg}\n\nCheck the link and your internet connection.")

    def _songsterr_to_practice(self):
        url = self.edit_url.text().strip()
        if not url:
            QMessageBox.information(self, "Songsterr", "Paste a Songsterr link or song id first.")
            return
        if self._st_meta is not None and self._st_url == url:
            self._load_songsterr_song()
            return
        self._pending_load = True
        self._fetch_songsterr()

    def _load_songsterr_song(self):
        if not self._st_meta:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        gp5_path = err = song = None
        try:
            cache = Path(tempfile.gettempdir()) / "FretFlow"
            cache.mkdir(exist_ok=True)
            name = (f"{self._st_meta.get('artist', '')} - {self._st_meta.get('title', '')}"
                    if self._st_meta.get("artist") else self._st_meta.get("title", "song"))
            gp5_path = cache / f"{exporter.safe_filename(name)}.gp5"
            songsterr_convert.write_gp5(self._st_meta, self._st_parts, gp5_path)
            song = load_song(gp5_path)
        except Exception as exc:
            err = exc
        finally:
            QApplication.restoreOverrideCursor()
        if err:
            QMessageBox.warning(self, "Cannot convert Songsterr tab", str(err))
            return
        self._source_path = None
        self._set_song(song)
        self._st_for_song = self.song
        self.statusBar().showMessage(f"Loaded from Songsterr - GP5 cache: {gp5_path}", 10000)

    # ------------------------------------------------------------- export
    def _export(self, kind: str):
        if not self.song:
            QMessageBox.information(self, "Export", "Load a song first (Open TAB… or Load for practice).")
            return
        ids = self._selected_track_ids()
        if not ids:
            QMessageBox.information(self, "Export", "Tick at least one track to export.")
            return
        base = exporter.safe_filename(
            f"{self.song.artist} - {self.song.title}" if self.song.artist else self.song.title)
        ext, filt = {"gp5": ("gp5", "Guitar Pro 5 (*.gp5)"), "gp": ("gp", "Guitar Pro 7/8 (*.gp)"),
                     "mid": ("mid", "MIDI file (*.mid)"), "json": ("json", "JSON file (*.json)")}[kind]
        out, _ = QFileDialog.getSaveFileName(self, "Export", str(Path.home() / "Downloads" / f"{base}.{ext}"), filt)
        if not out:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        err = None
        try:
            if kind == "gp5":
                mine = self._st_meta if self._st_for_song is self.song else None
                parts = self._st_parts if mine is not None else None
                exporter.export_gp5(self.song, out, ids, source_path=self._source_path,
                                    st_meta=mine, st_parts=parts)
            elif kind == "gp":
                exporter.export_gp(self.song, out, ids)
            elif kind == "mid":
                exporter.export_midi(self.song, out, ids)
            else:
                exporter.export_json(self.song, out, ids)
        except Exception as exc:
            err = exc
        finally:
            QApplication.restoreOverrideCursor()
        if err:
            QMessageBox.warning(self, "Export failed", str(err))
        else:
            self.statusBar().showMessage(f"Exported: {out}", 10000)

    def _on_track_changed(self):
        if not self.song or self.combo_track.currentIndex() < 0:
            return
        self.engine.load(self.song, self.combo_track.currentData())
        self._backing_dirty = True
        self.btn_play.setText("▶  Play")
        tr = self.song.tracks[self.combo_track.currentData()]
        if not tr.is_standard:
            self.statusBar().showMessage("Note: this track is not in standard tuning - pitches follow the file's tuning.")

    def _on_mode_changed(self):
        self.engine.set_mode(list(Mode)[self.combo_mode.currentIndex()])
        self._backing_dirty = True

    def _on_speed_changed(self, v):
        self.engine.set_speed(v / 100)
        self._backing_dirty = True

    def _ensure_backing(self):
        if self._backing_dirty and self.song:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                skip = None if self.s.backing_include_selected else self.engine.track_index
                self.player.set_buffer(render_backing(self.engine.song, skip))
                self._backing_dirty = False
            finally:
                QApplication.restoreOverrideCursor()

    def _toggle_play(self):
        if not self.song:
            self._open_file()
            return
        if not self.engine.running and self.engine.mode is Mode.AUTO:
            self._ensure_backing()
        self.engine.toggle()

    def _restart(self):
        if self.song:
            self.engine.reset()

    def _on_seek(self):
        if self.song and self.engine.mode is Mode.AUTO:
            self.engine.seek(self.slider.value() / 1000 * self.engine.duration)

    def _on_device_changed(self):
        self.s.input_device = self.combo_dev.currentData()
        self.statusBar().showMessage(self.tracker.start() or "Microphone ready.")

    def _on_volume(self, v):
        self.s.backing_volume = v
        self.player.volume = v / 100

    def _on_include_toggled(self, v):
        self.s.backing_include_selected = v
        self._backing_dirty = True

    # ---------------------------------------------------------------- tick
    def _on_tick(self):
        e = self.engine
        e.tick(self._clock.restart() / 1000.0)
        self.view.update()

        st = e.detected
        self.lbl_note.setText(midi_name(round(st.midi)) if st.midi is not None else "—")
        self.level.setValue(int(max(0.0, min(1.0, (st.level_db + 70) / 70)) * 100))
        self.btn_play.setText("⏸  Pause" if e.running else "▶  Play")

        if not self.slider.isSliderDown():
            self.slider.setValue(int(e.progress * 1000))
            self.slider.setEnabled(e.mode is Mode.AUTO)
        if e.events:
            acc = e.accuracy
            acc_txt = f"{acc:.0%}" if acc is not None else "–"
            extra = f"   Rewinds {e.rewinds}" if e.mode is Mode.AUTO else f"   Note {min(e.index + 1, len(e.events))}/{len(e.events)}"
            self.lbl_stats.setText(f"Hits {e.hits}   Misses {e.misses}   Accuracy {acc_txt}   Streak {e.streak}{extra}")
            bar = e.song.bar_at(e.now) + 1 if e.song else 1
            self.lbl_pos.setText(f"Bar {bar} / {len(e.bar_times)}")

    def closeEvent(self, ev):
        self.s.save()
        self.tracker.stop()
        self.player.close()
        if self._fetch is not None and self._fetch.isRunning():
            self._fetch.wait(3000)
        super().closeEvent(ev)
