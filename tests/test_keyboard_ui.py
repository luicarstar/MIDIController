"""Offscreen keyboard configuration and pressure Learn without native actions."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import mido
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QInputDialog, QSystemTrayIcon
from midictl import config, midi_io
from midictl.engine import Engine
from midictl.ui_main import MainWindow
from midictl.ui_mapping import MappingDialog
from tests.test_lifecycle import drain, FakePort


class KeyboardUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(temp) / "config.json"
        self.stack.enter_context(patch("midictl.ui_main.save_config",
            side_effect=lambda cfg: config.save_config(cfg, self.path)))
        self.stack.enter_context(patch.object(Engine, "start"))
        self.stack.enter_context(patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False))
        self.stack.enter_context(patch("mido.get_input_names", return_value=[]))
        self.stack.enter_context(patch("mido.get_output_names", return_value=[]))
        self.cfg = config.default_config()
        self.win = MainWindow(self.cfg)
        self.addCleanup(self.win._quit)

    def test_create_copy_switch_and_rebind_save_independent_keyboards(self):
        self.assertTrue(hasattr(self.win, "keyboard_combo"))
        first = self.cfg["active_keyboard"]
        self.cfg["profiles"][0]["mappings"].append(config.default_mapping())
        with patch.object(QInputDialog, "getText", return_value=("B", True)):
            self.win._add_keyboard(copy_current=True)
        self.assertEqual(self.win.keyboard_combo.currentData(), self.cfg["active_keyboard"])
        self.assertNotEqual(self.cfg["active_keyboard"], first)
        self.cfg["profiles"][0]["mappings"][0]["trigger"]["number"] = 72
        self.win._populate_ports(["Keyboard B 0"], ["Output B 1"])
        self.win.in_list.item(0).setCheckState(Qt.Checked)
        self.win.out_combo.setCurrentIndex(self.win.out_combo.findData("Output B 1"))
        self.win._ports_changed()
        self.assertEqual(config.load_config(self.path)["settings"]["output_port"], "Output B 1")
        self.win.keyboard_combo.setCurrentIndex(self.win.keyboard_combo.findData(first))
        self.win._keyboard_changed()
        self.assertEqual(self.cfg["profiles"][0]["mappings"][0]["trigger"]["number"], 60)
        self.assertEqual(self.cfg["settings"]["input_port"], "")
        with patch.object(QInputDialog, "getText", return_value=("C", True)):
            self.win._add_keyboard(copy_current=False)
        self.assertEqual(self.cfg["profiles"][0]["mappings"], [])
        self.assertEqual(self.win.table.rowCount(), 0)

    def test_pressure_learn_save_and_monitor_keep_poly_note_and_zero(self):
        for kind in ("aftertouch", "polytouch"):
            with self.subTest(kind=kind):
                dlg = MappingDialog(config.default_mapping(), self.win.engine, ["全局"], self.win)
                self.addCleanup(dlg.close)
                self.assertGreaterEqual(dlg.kind.findData(kind), 0)
                dlg.learn_btn.setChecked(True)
                kwargs = {"channel": 3, "value": 0}
                if kind == "polytouch":
                    kwargs["note"] = 72
                ev = midi_io.normalize(mido.Message(kind, **kwargs))
                self.win.engine.inject(ev)
                drain(self.win.engine)
                self.app.processEvents()
                self.win._flush_monitor()
                self.assertFalse(dlg.learn_btn.isChecked())
                self.assertEqual(dlg.kind.currentData(), kind)
                self.assertEqual(dlg.number.isEnabled(), kind == "polytouch")
                self.assertFalse(dlg.gesture.isEnabled())
                dlg._accept()
                result = dlg.result_mapping()
                self.assertEqual(result["trigger"]["number"], 72 if kind == "polytouch" else 0)
                self.assertEqual(result["trigger"]["event"], "change")
                self.cfg["profiles"][0]["mappings"] = [result]
                self.win.save()
                loaded = config.load_config(self.path)["profiles"][0]["mappings"][0]
                self.assertTrue(midi_io.trigger_matches(loaded["trigger"], ev))
                self.assertIn(kind.lower(), self.win.monitor.toPlainText().lower())

    def test_multiple_inputs_and_composite_learn_save_source_type_and_range(self):
        sources = ["A 0", "B 0"]
        with patch("mido.get_input_names", return_value=sources), patch("mido.open_input",
                side_effect=lambda name, callback: FakePort(name, callback)):
            self.win._populate_ports(sources, [])
            self.win.in_list.item(0).setCheckState(Qt.Checked)
            self.win.in_list.item(1).setCheckState(Qt.Checked)
            self.assertEqual(config.load_config(self.path)["settings"]["input_ports"], sources)
            cases = [("cc14", 11, [(11, 2), (43, 3)], "change", 0, 16383),
                     ("rpn", 257, [(101, 2), (100, 1), (6, 4)], "change", 0, 16383),
                     ("nrpn", 16300, [(99, 127), (98, 44), (6, 4)], "change", 0, 16383),
                     ("rpn", 1, [(101, 0), (100, 1), (96, 127)], "increase", None, None)]
            for kind, number, messages, event, lo, hi in cases:
                with self.subTest(kind=kind, event=event):
                    self.win.engine.mappings_changed()
                    self.cfg["profiles"][0]["mappings"] = []
                    dlg = MappingDialog(config.default_mapping(), self.win.engine, ["全局"], self.win)
                    self.addCleanup(dlg.close)
                    dlg.kind.setCurrentIndex(dlg.kind.findData(kind))
                    dlg.learn_btn.setChecked(True)
                    for control, value in messages:
                        self.win.engine.inputs["A 0"].port.callback(mido.Message(
                            "control_change", channel=2, control=control, value=value))
                        drain(self.win.engine)
                    self.assertFalse(dlg.learn_btn.isChecked())
                    dlg._accept()
                    result = dlg.result_mapping()
                    trigger = result["trigger"]
                    self.assertEqual((trigger["kind"], trigger["number"], trigger["source"], trigger["channel"]),
                                     (kind, number, "A 0", 3))
                    self.assertEqual((trigger["event"], trigger["value_min"], trigger["value_max"]), (event, lo, hi))
                    self.cfg["profiles"][0]["mappings"] = [result]
                    self.win.save()
                    self.assertEqual(config.load_config(self.path)["profiles"][0]["mappings"][0], result)

    def test_editor_preserves_full_pitch_and_button_settings(self):
        for kind, mode, lo, hi in (("pitchwheel", "full", -8192, 8191),
                                  ("pitchwheel", "legacy", -64, 64), ("cc", "full", 0, 127)):
            with self.subTest(kind=kind, mode=mode):
                m = config.default_mapping()
                m["trigger"].update(kind=kind, number=20, event="change", value_mode=mode,
                                     value_min=lo, value_max=hi, source="offline exact name",
                                     threshold=97, invert=True, reverse=True, encoding="rel_signed")
                dlg = MappingDialog(m, self.win.engine, ["全局"], self.win)
                self.addCleanup(dlg.close)
                dlg._accept()
                trigger = dlg.result_mapping()["trigger"]
                self.assertEqual((trigger["value_mode"], trigger["value_min"], trigger["value_max"]), (mode, lo, hi))
                self.assertEqual((trigger["source"], trigger["threshold"], trigger["invert"],
                                  trigger["reverse"], trigger["encoding"]),
                                 ("offline exact name", 97, True, True, "rel_signed"))
