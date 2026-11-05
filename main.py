"""Entry point: python main.py"""
import sys

from PySide6.QtWidgets import QApplication



def main():
    from midictl.config import load_config, save_config, CONFIG_PATH
    from midictl import style
    from midictl.ui_main import MainWindow
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    style.apply(app)
    cfg = load_config()
    if not CONFIG_PATH.exists():
        save_config(cfg)
    win = MainWindow(cfg)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        import tempfile
        from pathlib import Path
        log_path = Path(tempfile.gettempdir()) / "MIDIController-error.log"
        log_path.write_text(traceback.format_exc(), encoding="utf-8")
        if getattr(sys, "frozen", False):
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f"启动失败，详细信息：{log_path}", "MIDI 控制器", 16)
        else:
            raise
