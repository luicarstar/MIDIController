"""Soft light theme (QSS)."""

ACCENT = "#3a9e8f"
ACCENT_SOFT = "#e3f3f0"
CORAL = "#e9806e"

QSS = f"""
* {{
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 13px;
    color: #2f3a3d;
}}
QMainWindow, QDialog {{ background: #f5f6f4; }}
QLabel#sectionTitle {{ font-size: 14px; font-weight: 600; color: #34474a; padding: 2px 0; }}
QLabel#hint {{ color: #7b8a8c; }}

QWidget#panel {{
    background: #ffffff;
    border: 1px solid #e4e8e5;
    border-radius: 8px;
}}
QWidget#topbar {{
    background: #ffffff;
    border: 1px solid #e4e8e5;
    border-radius: 8px;
}}

QComboBox, QLineEdit, QSpinBox {{
    background: #fbfcfb;
    border: 1px solid #d9dfdb;
    border-radius: 6px;
    padding: 4px 8px;
    min-height: 20px;
    selection-background-color: {ACCENT_SOFT};
    selection-color: #2f3a3d;
}}
QComboBox:hover, QLineEdit:hover, QSpinBox:hover {{ border-color: #b9d8d2; }}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus {{ border-color: {ACCENT}; background: #ffffff; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: #ffffff; border: 1px solid #d9dfdb; border-radius: 6px;
    selection-background-color: {ACCENT_SOFT}; selection-color: #2f3a3d; outline: 0;
}}
QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; border: none; }}

QPushButton {{
    background: #ffffff;
    border: 1px solid #d3dbd7;
    border-radius: 6px;
    padding: 5px 14px;
}}
QPushButton:hover {{ background: {ACCENT_SOFT}; border-color: #a9d3cb; }}
QPushButton:pressed {{ background: #d3ebe6; }}
QPushButton#primary {{
    background: {ACCENT}; color: #ffffff; border: 1px solid {ACCENT}; font-weight: 600;
}}
QPushButton#primary:hover {{ background: #46ad9e; }}
QPushButton#learn:checked {{ background: {CORAL}; color: #ffffff; border-color: {CORAL}; }}

QToolButton {{
    background: transparent; border: 1px solid transparent; border-radius: 6px;
    padding: 4px; min-width: 22px; min-height: 22px;
}}
QToolButton:hover {{ background: {ACCENT_SOFT}; border-color: #cfe6e1; }}
QToolButton:pressed {{ background: #d3ebe6; }}

QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid #c4cec9; background: #ffffff;
}}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

QListWidget, QTableWidget, QPlainTextEdit {{
    background: #ffffff;
    border: 1px solid #e4e8e5;
    border-radius: 8px;
    outline: 0;
}}
QListWidget::item {{ padding: 7px 8px; border-radius: 6px; margin: 1px 4px; }}
QListWidget::item:hover {{ background: #f1f6f4; }}
QListWidget::item:selected {{ background: {ACCENT_SOFT}; color: #1f5c53; }}

QTableWidget {{ gridline-color: transparent; alternate-background-color: #fafbfa; }}
QTableWidget::item {{ padding: 4px 6px; border-bottom: 1px solid #f0f2f0; }}
QTableWidget::item:selected {{ background: {ACCENT_SOFT}; color: #1f5c53; }}
QHeaderView::section {{
    background: #ffffff; color: #7b8a8c; border: none;
    border-bottom: 1px solid #e4e8e5; padding: 6px; font-weight: 600;
}}
QTableCornerButton::section {{ background: #ffffff; border: none; }}

QPlainTextEdit#monitor {{
    font-family: "Cascadia Mono", "Consolas", "Microsoft YaHei UI";
    font-size: 12px; color: #48585b; background: #fbfcfb; padding: 4px;
}}

QGroupBox {{
    background: #ffffff; border: 1px solid #e4e8e5; border-radius: 8px;
    margin-top: 14px; padding: 12px 10px 8px 10px; font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {ACCENT};
}}

QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 8px; }}
QSplitter::handle:vertical {{ height: 8px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #d6dcd9; border-radius: 4px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: #b9c4bf; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #d6dcd9; border-radius: 4px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QStatusBar {{ background: #eef1ee; color: #5d6c6e; }}
QStatusBar QLabel {{ padding: 0 8px; }}
QMenu {{ background: #ffffff; border: 1px solid #dfe4e1; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 5px; }}
QMenu::item:selected {{ background: {ACCENT_SOFT}; color: #1f5c53; }}
QToolTip {{ background: #34474a; color: #ffffff; border: none; border-radius: 5px; padding: 4px 8px; }}
"""


def apply(app) -> None:
    app.setStyle("Fusion")
    app.setStyleSheet(QSS + _check_qss())


def _check_qss() -> str:
    """Render a white check mark PNG for checked checkboxes."""
    try:
        import tempfile
        from pathlib import Path
        import qtawesome as qta
        p = Path(tempfile.gettempdir()) / "midictl_check.png"
        qta.icon("ph.check-bold", color="#ffffff").pixmap(14, 14).save(str(p))
        url = p.as_posix()
        return f"QCheckBox::indicator:checked, QTableWidget::indicator:checked {{ image: url({url}); }}\n" \
               "QTableWidget::indicator { width: 16px; height: 16px; border-radius: 4px; " \
               "border: 1px solid #c4cec9; background: #ffffff; }\n" \
               f"QTableWidget::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}\n"
    except Exception:
        return ""


def icon(name: str, color: str = "#5b6e70"):
    """Phosphor icon via qtawesome; falls back to empty icon."""
    try:
        import qtawesome as qta
        return qta.icon(f"ph.{name}", color=color, color_active=ACCENT)
    except Exception:
        from PySide6.QtGui import QIcon
        return QIcon()
