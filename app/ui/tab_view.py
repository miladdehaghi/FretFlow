"""Scrolling TAB renderer: notes travel right -> left towards a fixed target line."""
from __future__ import annotations

import time
from bisect import bisect_left, bisect_right

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..engine import HIT, MISS, PENDING, SKIP, Mode
from ..models import midi_name
from . import theme


class TabView(QWidget):
    LEFT, TOP, BOTTOM, NOTE_H = 64, 56, 34, 22

    def __init__(self, engine, settings, parent=None):
        super().__init__(parent)
        self.e, self.s = engine, settings
        self.setMinimumHeight(280)
        self._font = QFont("Segoe UI", 10, QFont.Bold)
        self._small = QFont("Segoe UI", 9)

    def paintEvent(self, _):
        e, p = self.e, QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        p.fillRect(self.rect(), theme.BG)
        w, h = self.width(), self.height()
        if not e.events:
            p.setPen(theme.MUTED)
            p.setFont(QFont("Segoe UI", 13))
            p.drawText(self.rect(), Qt.AlignCenter, "Open a Guitar Pro file (.gp / .gp5 / .gp4 / .gp3) to start practising")
            return

        n = len(e.tuning)
        top, bottom = self.TOP, h - self.BOTTOM
        gap = (bottom - top) / max(1, n - 1)
        tx = self.LEFT + (w - self.LEFT) * self.s.target_pos / 100
        pps, now = (w - tx) / max(0.5, self.s.lookahead_s), e.now
        t_lo, t_hi = now - (tx - self.LEFT) / pps - 0.6, now + (w - tx) / pps + 0.6
        x_of = lambda t: tx + (t - now) * pps
        y_of = lambda s: top + s * gap

        # strings + names
        p.setFont(self._small)
        for s in range(n):
            y = y_of(s)
            p.setPen(QPen(theme.STRING, 1.3))
            p.drawLine(self.LEFT, int(y), w, int(y))
            p.setPen(theme.MUTED)
            p.drawText(QRectF(8, y - 10, self.LEFT - 18, 20), Qt.AlignRight | Qt.AlignVCenter, midi_name(e.tuning[s]))

        # bar lines + numbers
        bt = e.bar_times
        for i in range(bisect_left(bt, t_lo), len(bt)):
            if bt[i] > t_hi:
                break
            x = x_of(bt[i])
            if x < self.LEFT:
                continue
            p.setPen(QPen(theme.BAR, 1.5))
            p.drawLine(int(x), int(top - 12), int(x), int(bottom + 12))
            p.setPen(theme.MUTED)
            p.drawText(int(x + 5), int(top - 22), str(i + 1))

        # target zone (flashes green / red on hit / wrong note)
        flash = e.flash_kind if time.monotonic() < e.flash_until else ""
        zone = {"hit": theme.HIT, "wrong": theme.MISS}.get(flash, theme.ACCENT)
        glow = QColor(zone)
        glow.setAlpha(70 if flash else 28)
        p.fillRect(QRectF(tx - 20, top - 16, 40, bottom - top + 32), glow)
        p.setPen(QPen(zone, 2.5))
        p.drawLine(int(tx), int(top - 20), int(tx), int(bottom + 20))

        # notes
        p.setFont(self._font)
        fm = QFontMetrics(self._font)
        follow = e.mode is Mode.FOLLOW
        lo, hi = bisect_left(e.times, t_lo), bisect_right(e.times, t_hi)
        for i in range(lo, hi):
            ev, st = e.events[i], e.status[i]
            x = x_of(ev.time)
            if x < self.LEFT - 20:
                continue
            current = follow and i == e.index
            fill, border, txt = theme.NOTE_BG, theme.ACCENT, theme.TEXT
            if st == HIT:
                fill, border, txt = theme.HIT, theme.HIT, QColor("#052e16")
            elif st == MISS:
                fill, border, txt = QColor("#4a1d24"), theme.MISS, theme.TEXT
            elif st == SKIP:
                fill, border, txt = QColor("#1c2130"), theme.STRING, theme.MUTED
            elif current:
                fill, border, txt = theme.CURRENT, theme.CURRENT, QColor("#2a1a00")

            if len(ev.notes) > 1:                                  # chord bracket
                ys = [y_of(nt.string) for nt in ev.notes]
                p.setPen(QPen(QColor(border.red(), border.green(), border.blue(), 90), 2))
                p.drawLine(int(x), int(min(ys)), int(x), int(max(ys)))
            res = e.chord_result.get(i) if st in (PENDING, MISS) else None
            for note in ev.notes:
                y = y_of(note.string)
                label = "x" if note.dead else (str(note.fret - self.s.capo) if note.fret >= self.s.capo else "–")
                bw = max(24, fm.horizontalAdvance(label) + 12)
                rect = QRectF(x - bw / 2, y - self.NOTE_H / 2, bw, self.NOTE_H)
                if current:
                    halo = QColor(theme.CURRENT)
                    halo.setAlpha(60)
                    p.setPen(Qt.NoPen)
                    p.setBrush(halo)
                    p.drawRoundedRect(rect.adjusted(-5, -5, 5, 5), 10, 10)
                p.setBrush(fill)
                nb = border if res is None or note.dead else (theme.HIT if res.get(note.midi) else theme.MISS)
                p.setPen(QPen(nb, 1.4 if res is None else 2.6))
                p.drawRoundedRect(rect, 7, 7)
                p.setPen(txt)
                p.drawText(rect, Qt.AlignCenter, label)

        # message banner
        if time.monotonic() < e.message_until and e.message:
            p.setFont(QFont("Segoe UI", 11, QFont.DemiBold))
            tw = QFontMetrics(p.font()).horizontalAdvance(e.message) + 36
            r = QRectF((w - tw) / 2, 8, tw, 32)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(251, 191, 36, 230))
            p.drawRoundedRect(r, 16, 16)
            p.setPen(QColor("#2a1a00"))
            p.drawText(r, Qt.AlignCenter, e.message)
