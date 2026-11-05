"""Tray lifecycle without opening MIDI devices or changing user settings."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest.mock import patch, MagicMock
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from midictl.config import default_config
from midictl.ui_main import MainWindow


class TrayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_hide_restore_and_exit(self):
        with patch('midictl.ui_main.Engine') as engine, \
             patch.object(MainWindow, '_refresh_ports'), \
             patch.object(QSystemTrayIcon, 'isSystemTrayAvailable', return_value=True):
            win = MainWindow(default_config())
            win.show()
            win.showMinimized()
            self.app.processEvents()
            self.assertFalse(win.isVisible())
            win._show()
            self.assertTrue(win.isVisible())
            win.close()
            self.assertFalse(win.isVisible())
            engine.return_value.stop.assert_not_called()
            win._show()
            win._quit()
            engine.return_value.stop.assert_called_once()
            self.assertFalse(win.tray.isVisible())

    def test_no_tray_closes_cleanly(self):
        with patch('midictl.ui_main.Engine') as engine, \
             patch.object(MainWindow, '_refresh_ports'), \
             patch.object(QSystemTrayIcon, 'isSystemTrayAvailable', return_value=False):
            win = MainWindow(default_config())
            win.show()
            win.close()
            engine.return_value.stop.assert_called_once()


if __name__ == '__main__':
    unittest.main()
