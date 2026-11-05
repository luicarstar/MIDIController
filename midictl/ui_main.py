"""Main window: devices, profiles, mapping table, monitor, tray."""
from __future__ import annotations

import time

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QListWidget, QMainWindow, QMenu, QMessageBox,
                               QPlainTextEdit, QPushButton, QSplitter, QStyle, QSystemTrayIcon,
                               QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from . import midi_io, win_input
from .style import icon
from .actions import describe_actions
from .config import GLOBAL_PROFILE, default_mapping, new_id, save_config, clone
from .engine import Engine
from .foreground import foreground_exe
from .hud import Hud
from .ui_mapping import MappingDialog


class Bridge(QObject):
    """Marshal engine-thread callbacks onto the UI thread."""
    midi = Signal(object)
    log = Signal(str)
    profile = Signal(str, str)
    variables = Signal(dict)
    hud = Signal(str)


def icon_btn(widget, name, tip, slot, color="#5b6e70") -> QToolButton:
    b = QToolButton()
    b.setIcon(icon(name, color))
    b.setToolTip(tip)
    b.setAutoRaise(True)
    b.clicked.connect(slot)
    return b


def title(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setObjectName("sectionTitle")
    return lb


def panel() -> tuple[QWidget, QVBoxLayout]:
    w = QWidget()
    w.setObjectName("panel")
    w.setAttribute(Qt.WA_StyledBackground, True)
    lay = QVBoxLayout(w)
    lay.setContentsMargins(12, 10, 12, 12)
    lay.setSpacing(8)
    return w, lay


class MainWindow(QMainWindow):
    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.bridge = Bridge()
        self.engine = Engine(cfg, log=self.bridge.log.emit)
        self.engine.on_midi = self.bridge.midi.emit
        self.engine.on_profile_changed = self.bridge.profile.emit
        self.engine.on_variables = self.bridge.variables.emit
        self.engine.on_hud = self.bridge.hud.emit
        self.hud = Hud()
        self.bridge.hud.connect(self.hud.show_text)
        self.bridge.midi.connect(self._on_midi)
        self.bridge.log.connect(self._log)
        self.bridge.profile.connect(self._on_profile_changed)
        self.bridge.variables.connect(self._on_variables)
        self._quitting = False
        self._last_midi_key = None

        admin = " [管理员]" if win_input.is_admin() else ""
        self.setWindowTitle(f"MIDI 控制器{admin}")
        self.resize(1000, 680)
        self._build_ui()
        self._build_tray()
        self._refresh_ports()
        self._refresh_profiles()
        self.engine.start()

    # ---------- layout ----------
    def _build_ui(self):
        root = QWidget()
        v = QVBoxLayout(root)
        v.setContentsMargins(12, 12, 12, 8)
        v.setSpacing(10)

        topbar = QWidget()
        topbar.setObjectName("topbar")
        topbar.setAttribute(Qt.WA_StyledBackground, True)
        bar = QHBoxLayout(topbar)
        bar.setContentsMargins(12, 8, 12, 8)
        bar.setSpacing(8)
        self.in_combo = QComboBox()
        self.out_combo = QComboBox()
        self.in_combo.setMinimumWidth(200)
        self.out_combo.setMinimumWidth(200)
        self.in_combo.activated.connect(self._ports_changed)
        self.out_combo.activated.connect(self._ports_changed)
        dev = QLabel()
        dev.setPixmap(icon("plugs-connected", "#3a9e8f").pixmap(18, 18))
        bar.addWidget(dev)
        bar.addWidget(QLabel("输入"))
        bar.addWidget(self.in_combo)
        bar.addWidget(QLabel("输出"))
        bar.addWidget(self.out_combo)
        bar.addWidget(icon_btn(self, "arrows-clockwise", "刷新设备", self._refresh_ports))
        bar.addStretch(1)
        s = self.cfg["settings"]
        self.enabled_cb = QCheckBox("启用映射")
        self.enabled_cb.setChecked(s.get("enabled", True))
        self.enabled_cb.toggled.connect(lambda on: self._set_setting("enabled", on))
        self.switch_cb = QCheckBox("按前台程序切换")
        self.switch_cb.setChecked(s.get("app_switching", True))
        self.switch_cb.toggled.connect(lambda on: self._set_setting("app_switching", on))
        bar.addWidget(self.enabled_cb)
        bar.addWidget(self.switch_cb)
        v.addWidget(topbar)

        split = QSplitter(Qt.Horizontal)
        # profiles
        left, lv = panel()
        ph = QHBoxLayout()
        ph.addWidget(title("配置"))
        ph.addStretch(1)
        ph.addWidget(icon_btn(self, "folder-simple-plus", "新建配置", self._add_profile))
        ph.addWidget(icon_btn(self, "app-window", "编辑绑定程序", self._edit_apps))
        ph.addWidget(icon_btn(self, "trash", "删除配置", self._del_profile, "#d2725f"))
        lv.addLayout(ph)
        self.profile_list = QListWidget()
        self.profile_list.currentRowChanged.connect(self._profile_selected)
        lv.addWidget(self.profile_list)
        self.apps_label = QLabel()
        self.apps_label.setObjectName("hint")
        self.apps_label.setWordWrap(True)
        lv.addWidget(self.apps_label)
        split.addWidget(left)

        # mappings
        right, rv = panel()
        mh = QHBoxLayout()
        mh.addWidget(title("映射"))
        mh.addStretch(1)
        mh.addWidget(icon_btn(self, "pencil-simple", "编辑", self._edit_mapping))
        mh.addWidget(icon_btn(self, "copy", "复制", self._dup_mapping))
        mh.addWidget(icon_btn(self, "arrow-up", "上移", lambda: self._move_mapping(-1)))
        mh.addWidget(icon_btn(self, "arrow-down", "下移", lambda: self._move_mapping(1)))
        mh.addWidget(icon_btn(self, "trash", "删除", self._del_mapping, "#d2725f"))
        add = QPushButton("新建映射")
        add.setObjectName("primary")
        add.setIcon(icon("plus", "#ffffff"))
        add.clicked.connect(self._add_mapping)
        mh.addWidget(add)
        rv.addLayout(mh)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["启用", "名称", "触发", "条件", "动作"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Interactive)
        hh.setSectionResizeMode(2, QHeaderView.Interactive)
        hh.setSectionResizeMode(3, QHeaderView.Interactive)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setColumnWidth(1, 160)
        self.table.setColumnWidth(2, 220)
        self.table.setColumnWidth(3, 110)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setFrameShape(QTableWidget.NoFrame)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.cellDoubleClicked.connect(lambda r, c: self._edit_mapping())
        self.table.itemChanged.connect(self._item_changed)
        rv.addWidget(self.table)
        split.addWidget(right)
        split.setSizes([250, 750])

        vsplit = QSplitter(Qt.Vertical)
        vsplit.addWidget(split)
        bottom, bv = panel()
        bh = QHBoxLayout()
        bh.addWidget(title("MIDI 监视"))
        bh.addStretch(1)
        bh.addWidget(icon_btn(self, "eraser", "清空", lambda: self.monitor.clear()))
        bv.addLayout(bh)
        self.monitor = QPlainTextEdit()
        self.monitor.setObjectName("monitor")
        self.monitor.setFrameShape(QPlainTextEdit.NoFrame)
        self.monitor.setReadOnly(True)
        self.monitor.setMaximumBlockCount(500)
        bv.addWidget(self.monitor)
        vsplit.addWidget(bottom)
        vsplit.setSizes([480, 180])
        v.addWidget(vsplit)
        self.setCentralWidget(root)

        self.status_profile = QLabel()
        self.status_vars = QLabel()
        self.status_fg = QLabel()
        self.statusBar().addWidget(self.status_profile)
        self.statusBar().addWidget(self.status_vars)
        self.statusBar().addPermanentWidget(self.status_fg)

    def _build_tray(self):
        self.tray = QSystemTrayIcon(icon("sliders-horizontal", "#3a9e8f"), self)
        self.setWindowIcon(self.tray.icon())
        menu = QMenu()
        menu.addAction("显示窗口", self._show)
        self.tray_enabled = QAction("启用映射", menu, checkable=True)
        self.tray_enabled.setChecked(self.cfg["settings"].get("enabled", True))
        self.tray_enabled.toggled.connect(self.enabled_cb.setChecked)
        self.enabled_cb.toggled.connect(self.tray_enabled.setChecked)
        menu.addAction(self.tray_enabled)
        menu.addSeparator()
        menu.addAction("退出", self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self._show() if r in (
            QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.setToolTip("MIDI 控制器")
        self.tray.show()

    # ---------- helpers ----------
    def save(self):
        save_config(self.cfg)

    def _set_setting(self, key, val):
        self.cfg["settings"][key] = val
        self.save()
        if key == "app_switching":
            self.engine._q.put("fg")

    def _log(self, text: str):
        self._last_midi_key = None
        self.monitor.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")

    def _on_midi(self, ev):
        # Continuous controls (CC / pitch bend) update the last line instead of flooding
        key = ev.key if ev.kind in ("cc", "pitchwheel") else None
        line = f"{time.strftime('%H:%M:%S')}  {ev.describe()}"
        if key is not None and key == self._last_midi_key:
            block = self.monitor.document().lastBlock()
            cur = self.monitor.textCursor()
            cur.setPosition(block.position())
            cur.movePosition(cur.MoveOperation.EndOfBlock, cur.MoveMode.KeepAnchor)
            cur.insertText(line)
        else:
            self.monitor.appendPlainText(line)
        self._last_midi_key = key

    def _on_variables(self, v: dict):
        self.status_vars.setText("  变量: " + ", ".join(f"{k}={val}" for k, val in sorted(v.items())))

    def _on_profile_changed(self, name, exe):
        self.status_profile.setText(f"当前配置: {name}")
        self.status_fg.setText(f"前台: {exe or '-'}")
        self._log(f"切换配置 -> {name} ({exe})")

    def _profile(self) -> dict:
        row = max(0, self.profile_list.currentRow())
        return self.cfg["profiles"][row]

    def _show(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ---------- ports ----------
    def _refresh_ports(self):
        s = self.cfg["settings"]
        for combo, names, cur in ((self.in_combo, midi_io.list_inputs(), s.get("input_port")),
                                  (self.out_combo, midi_io.list_outputs(), s.get("output_port"))):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("(无)", "")
            for n in names:
                combo.addItem(n, n)
            if cur and cur not in names:
                combo.addItem(f"{cur} (未连接)", cur)
            combo.setCurrentIndex(max(0, combo.findData(cur or "")))
            combo.blockSignals(False)
        self.engine.open_ports()

    def _ports_changed(self):
        self.cfg["settings"]["input_port"] = self.in_combo.currentData() or ""
        self.cfg["settings"]["output_port"] = self.out_combo.currentData() or ""
        self.save()
        self.engine.open_ports()

    # ---------- profiles ----------
    def _refresh_profiles(self, select: int = 0):
        self.profile_list.blockSignals(True)
        self.profile_list.clear()
        for p in self.cfg["profiles"]:
            apps = ", ".join(p["apps"])
            self.profile_list.addItem(p["name"] + (f"  [{apps}]" if apps else ""))
        self.profile_list.blockSignals(False)
        self.profile_list.setCurrentRow(min(select, len(self.cfg["profiles"]) - 1))
        self._profile_selected()

    def _profile_selected(self, *_):
        p = self._profile()
        if p["name"] == GLOBAL_PROFILE:
            self.apps_label.setText("全局配置：其他配置未命中时使用")
        else:
            self.apps_label.setText("绑定程序: " + (", ".join(p["apps"]) or "(未绑定)"))
        self._refresh_table()

    def _add_profile(self):
        name, ok = QInputDialog.getText(self, "新建配置", "配置名称")
        name = name.strip()
        if not ok or not name:
            return
        if any(p["name"] == name for p in self.cfg["profiles"]):
            QMessageBox.warning(self, "重名", "已存在同名配置")
            return
        self.cfg["profiles"].append({"name": name, "apps": [], "mappings": []})
        self.save()
        self._refresh_profiles(len(self.cfg["profiles"]) - 1)
        self._edit_apps()

    def _edit_apps(self):
        p = self._profile()
        if p["name"] == GLOBAL_PROFILE:
            return
        hint = foreground_exe() or self.engine.foreground
        text, ok = QInputDialog.getText(
            self, "绑定程序", f"进程名，逗号分隔（如 chrome.exe）。最近前台: {self.engine.foreground or hint}",
            text=", ".join(p["apps"]))
        if ok:
            p["apps"] = [a.strip().lower() for a in text.split(",") if a.strip()]
            self.save()
            self._refresh_profiles(self.profile_list.currentRow())
            self.engine._q.put("fg")

    def _del_profile(self):
        p = self._profile()
        if p["name"] == GLOBAL_PROFILE:
            return
        if QMessageBox.question(self, "删除配置", f"删除配置“{p['name']}”？") == QMessageBox.Yes:
            self.cfg["profiles"].remove(p)
            self.save()
            self._refresh_profiles()
            self.engine._q.put("fg")

    # ---------- mappings ----------
    def _refresh_table(self):
        p = self._profile()
        self.table.blockSignals(True)
        self.table.setRowCount(len(p["mappings"]))
        for r, m in enumerate(p["mappings"]):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            chk.setCheckState(Qt.Checked if m.get("enabled", True) else Qt.Unchecked)
            self.table.setItem(r, 0, chk)
            self.table.setItem(r, 1, QTableWidgetItem(m.get("name", "")))
            self.table.setItem(r, 2, QTableWidgetItem(midi_io.describe_trigger(m["trigger"])))
            cond = m.get("conditions", "") + (" [停止后续]" if m.get("stop") else "")
            self.table.setItem(r, 3, QTableWidgetItem(cond))
            self.table.setItem(r, 4, QTableWidgetItem(describe_actions(m.get("actions", []))))
        self.table.blockSignals(False)

    def _item_changed(self, item):
        if item.column() == 0:
            self._profile()["mappings"][item.row()]["enabled"] = item.checkState() == Qt.Checked
            self.save()

    def _row(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _open_dialog(self, m: dict):
        names = [p["name"] for p in self.cfg["profiles"]]
        dlg = MappingDialog(m, self.engine, names, self)
        return dlg.result_mapping() if dlg.exec() else None

    def _add_mapping(self):
        m = self._open_dialog(default_mapping())
        if m:
            self._profile()["mappings"].append(m)
            self.save()
            self._refresh_table()
            self.table.selectRow(self.table.rowCount() - 1)

    def _edit_mapping(self):
        r = self._row()
        if r < 0:
            return
        maps = self._profile()["mappings"]
        m = self._open_dialog(maps[r])
        if m:
            maps[r] = m
            self.save()
            self._refresh_table()
            self.table.selectRow(r)

    def _dup_mapping(self):
        r = self._row()
        if r < 0:
            return
        maps = self._profile()["mappings"]
        m = clone(maps[r])
        m["id"] = new_id()
        m["name"] += " 副本"
        maps.insert(r + 1, m)
        self.save()
        self._refresh_table()
        self.table.selectRow(r + 1)

    def _move_mapping(self, d: int):
        r = self._row()
        maps = self._profile()["mappings"]
        if r < 0 or not 0 <= r + d < len(maps):
            return
        maps[r], maps[r + d] = maps[r + d], maps[r]
        self.save()
        self._refresh_table()
        self.table.selectRow(r + d)

    def _del_mapping(self):
        r = self._row()
        if r >= 0:
            del self._profile()["mappings"][r]
            self.save()
            self._refresh_table()

    # ---------- lifecycle ----------
    def changeEvent(self, e):
        super().changeEvent(e)
        if self.isMinimized() and self.tray.isVisible() and QSystemTrayIcon.isSystemTrayAvailable():
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self.hide)

    def closeEvent(self, e):
        if (self.cfg["settings"].get("close_to_tray", True) and not self._quitting
                and QSystemTrayIcon.isSystemTrayAvailable()):
            e.ignore()
            self.hide()
            self.tray.showMessage("MIDI 控制器", "已最小化到托盘，映射仍在运行", QSystemTrayIcon.Information, 2000)
            return
        self.engine.stop()
        self.tray.hide()
        e.accept()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()

    def _quit(self):
        self._quitting = True
        self.close()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()
