"""Dark theme: colour palette + Qt style sheet."""
from PySide6.QtGui import QColor

BG = QColor("#12141a")
PANEL = "#1a1d26"
STRING = QColor("#3a4256")
BAR = QColor("#2a3040")
TEXT = QColor("#e6e9f0")
MUTED = QColor("#7b859c")
ACCENT = QColor("#38bdf8")
HIT = QColor("#4ade80")
MISS = QColor("#f87171")
CURRENT = QColor("#fbbf24")
NOTE_BG = QColor("#232a3b")

STYLESHEET = f"""
* {{ font-family: "Segoe UI", sans-serif; font-size: 13px; color: #e6e9f0; }}
QMainWindow, QDialog {{ background: #12141a; }}
QFrame#bar {{ background: {PANEL}; border-radius: 10px; }}
QDockWidget {{ titlebar-close-icon: none; }}
QDockWidget::title {{ background: {PANEL}; padding: 8px; font-weight: 600; }}
QGroupBox {{ border: 1px solid #2a3040; border-radius: 8px; margin-top: 14px; padding: 10px 8px 6px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; color: #7b859c; }}
QPushButton {{ background: #262c3b; border: 1px solid #323a4e; border-radius: 8px; padding: 7px 16px; }}
QPushButton:hover {{ background: #2f3748; }}
QPushButton:pressed {{ background: #202635; }}
QPushButton#primary {{ background: #0ea5e9; border: none; color: #04121c; font-weight: 700; padding: 7px 22px; }}
QPushButton#primary:hover {{ background: #38bdf8; }}
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{ background: #1f2430; border: 1px solid #323a4e; border-radius: 6px; padding: 5px 8px; }}
QComboBox QAbstractItemView {{ background: #1f2430; selection-background-color: #0ea5e9; selection-color: #04121c; }}
QLineEdit:focus {{ border-color: #0ea5e9; }}
QMenu {{ background: #1f2430; border: 1px solid #323a4e; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 5px; }}
QMenu::item:selected {{ background: #0ea5e9; color: #04121c; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid #4a5570; background: #1f2430; }}
QCheckBox::indicator:checked {{ background: #0ea5e9; border-color: #0ea5e9; }}
QSlider::groove:horizontal {{ height: 4px; background: #2a3040; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: #0ea5e9; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: #e6e9f0; width: 14px; margin: -6px 0; border-radius: 7px; }}
QProgressBar {{ background: #1f2430; border: none; border-radius: 4px; height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: #4ade80; border-radius: 4px; }}
QStatusBar {{ background: #0e1015; color: #7b859c; }}
QLabel#note {{ font-size: 30px; font-weight: 700; }}
QLabel#muted {{ color: #7b859c; }}
QToolTip {{ background: #1f2430; color: #e6e9f0; border: 1px solid #323a4e; }}
"""
