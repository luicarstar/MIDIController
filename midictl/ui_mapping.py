"""Dialog for editing one mapping: trigger (with MIDI learn) + action."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QMessageBox, QPushButton, QSpinBox, QStyle, QToolButton,
                               QVBoxLayout, QWidget, QScrollArea)

from . import actions, rules, win_input
from .config import clone, input_ports
from .midi_io import note_name, trigger_from_event
from .style import icon

KINDS = [("note", "音符 Note"), ("cc", "控制器 CC"), ("pitchwheel", "弯音轮"),
         ("program_change", "Program"), ("aftertouch", "通道触后 Aftertouch"),
         ("polytouch", "复音触后 Polytouch"), ("cc14", "14bit CC"),
         ("rpn", "RPN 参数"), ("nrpn", "NRPN 参数")]
EVENTS = [("press", "按下"), ("release", "松开"), ("both", "按下+松开"), ("change", "数值变化"),
          ("increase", "增大/右转"), ("decrease", "减小/左转")]
ENCODINGS = [("absolute", "绝对值"), ("rel_2c", "旧相对: 1-63 增 / 65-127 减"),
             ("rel_64", "旧相对: 65+ 增 / 63- 减"),
             ("rel_signed", "Sign magnitude: 1=+1 / 65=-1"),
             ("rel_offset", "Binary offset: 65=+1 / 63=-1"),
             ("rel_arturia3", "Arturia relative 3: 17=+1 / 15=-1")]
GESTURES = [("any", "普通（立即）"), ("short", "短按"), ("long", "长按"), ("double", "双击")]


def combo(items, current=None) -> QComboBox:
    c = QComboBox()
    for key, label in items:
        c.addItem(label, key)
    if current is not None:
        i = c.findData(current)
        if i >= 0:
            c.setCurrentIndex(i)
    return c


def spin(lo, hi, val, special=None) -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    if special:
        s.setSpecialValueText(special)
    s.setValue(int(val) if val not in (None, "") else lo)
    return s


class MappingDialog(QDialog):
    learned = Signal(object)

    def __init__(self, mapping: dict, engine, profile_names: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑映射")
        self.setMinimumWidth(560)
        self.resize(620, 760)
        self.engine = engine
        self.profile_names = profile_names
        self.m = clone(mapping)
        self.fields: dict[str, QWidget] = {}
        self.learned.connect(self._apply_learn)

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)
        lay = QVBoxLayout(content)
        top = QFormLayout()
        self.name = QLineEdit(self.m.get("name", ""))
        self.enabled = QCheckBox("启用")
        self.enabled.setChecked(self.m.get("enabled", True))
        top.addRow("名称", self.name)
        top.addRow("", self.enabled)
        lay.addLayout(top)

        tg = QGroupBox("触发")
        tf = QFormLayout(tg)
        t = self.m["trigger"]
        self.kind = combo(KINDS, t.get("kind"))
        self.channel = spin(0, 16, t.get("channel") or 0, "任意")
        self.number = spin(-1, 16383, t.get("number") if t.get("number") not in (None, "") else -1, "任意")
        self.number.valueChanged.connect(self._update_note_label)
        self.event = combo(EVENTS, t.get("event"))
        self.learn_btn = QPushButton("学习")
        self.learn_btn.setObjectName("learn")
        self.learn_btn.setIcon(icon("record", "#e9806e"))
        self.learn_btn.setCheckable(True)
        self.learn_btn.setToolTip("按下 MIDI 键盘上的控件自动填写")
        self.learn_btn.toggled.connect(self._toggle_learn)
        row = QHBoxLayout()
        row.addWidget(self.kind, 1)
        row.addWidget(self.learn_btn)
        tf.addRow("类型", row)
        sources = list(dict.fromkeys(input_ports(engine.cfg["settings"]) + list(engine.inputs)))
        if t.get("source") and t["source"] not in sources:
            sources.append(t["source"])
        self.source = combo([(None, "任意来源")] + [(n, engine.source_label(n)) for n in sources], t.get("source"))
        self.source.setToolTip("WinMM按精确端口名、WinRT按稳定设备ID绑定。学习记录来源；重学另一端口前可选任意来源。")
        for index in range(1, self.source.count()):
            self.source.setItemData(index, self.source.itemData(index), Qt.ToolTipRole)
        tf.addRow("来源", self.source)
        tf.addRow("通道", self.channel)
        tf.addRow("编号", self.number)
        tf.addRow("事件", self.event)
        self.value_mode = combo([("legacy", "旧弯音 -64..64（兼容已有动作/条件）"),
                                 ("full", "完整弯音 -8192..8191 / 14bit 0..16383")],
                                t.get("value_mode", "legacy"))
        tf.addRow("数值模式", self.value_mode)
        self.vmin = spin(-8193, 16383, t.get("value_min") if t.get("value_min") not in (None, "") else -8193, "不限")
        self.vmax = spin(-8193, 16383, t.get("value_max") if t.get("value_max") not in (None, "") else -8193, "不限")
        self.vmin.setToolTip("范围与所选数值模式一致。RPN/NRPN增减没有绝对值，设置范围会拒绝这类事件。")
        self.vmax.setToolTip(self.vmin.toolTip())
        vr = QHBoxLayout()
        vr.addWidget(self.vmin)
        vr.addWidget(QLabel("至"))
        vr.addWidget(self.vmax)
        tf.addRow("数值范围", vr)
        self.encoding = combo(ENCODINGS, t.get("encoding", "absolute"))
        self.encoding.setToolTip("学习仅捕获类型、通道和编号。请转动旋钮观察主窗口数值，手动确认绝对值或相对编码。")
        tf.addRow("旋钮编码", self.encoding)
        self.reverse = QCheckBox("反转旋钮方向")
        self.reverse.setChecked(bool(t.get("reverse")))
        tf.addRow("", self.reverse)
        self.relative_steps = QCheckBox("旧相对模式也保留报文步数")
        self.relative_steps.setChecked(bool(t.get("relative_steps")))
        self.relative_steps.setToolTip("新相对模式始终保留步数。保留步数时不再叠乘时间加速。")
        tf.addRow("", self.relative_steps)
        self.threshold = spin(0, 127, t.get("threshold", 64))
        self.invert = QCheckBox("踏板/CC按钮极性反向")
        self.invert.setChecked(bool(t.get("invert", False)))
        button_row = QHBoxLayout()
        button_row.addWidget(self.threshold)
        button_row.addWidget(self.invert)
        tf.addRow("按下阈值", button_row)
        self.edge_mode = combo([("edge", "边沿：跨阈值才按下/松开"),
                                ("level", "旧兼容：每条高/低值触发")], t.get("edge_mode", "level"))
        self.edge_mode.setToolTip("边沿首次高态触发按下，首次低态不制造松开；数值变化仍逐条处理。")
        tf.addRow("CC按钮触发", self.edge_mode)
        self.gesture = combo(GESTURES, t.get("gesture", "any"))
        self.long_ms = spin(100, 5000, t.get("long_ms") or 500)
        self.long_ms.setSuffix(" ms")
        gr = QHBoxLayout()
        gr.addWidget(self.gesture, 1)
        gr.addWidget(QLabel("长按阈值"))
        gr.addWidget(self.long_ms)
        tf.addRow("手势", gr)
        self._update_note_label()
        self.kind.currentIndexChanged.connect(self._update_note_label)
        self.value_mode.currentIndexChanged.connect(self._update_note_label)
        self.encoding.currentIndexChanged.connect(self._update_note_label)
        delta_hint = QLabel("RPN/NRPN 的增减仅支持旋钮步进按键、拖动、跟随方向滚轮；\n"
                            "没有绝对值，不用于范围/$value条件/音量跟随/MIDI数值转发。")
        delta_hint.setWordWrap(True)
        tf.addRow(delta_hint)
        lay.addWidget(tg)

        rg = QGroupBox("条件")
        rf = QFormLayout(rg)
        self.conditions = QLineEdit(self.m.get("conditions", ""))
        self.conditions.setPlaceholderText("shift==1, mode!=2, $value>64")
        self.conditions.setToolTip("逗号分隔，全部满足才触发。$value 为输入数值，其余为变量")
        rf.addRow("仅当", self.conditions)
        self.stop = QCheckBox("命中后不再处理后面的映射")
        self.stop.setChecked(bool(self.m.get("stop")))
        rf.addRow("", self.stop)
        self.hud = QCheckBox("触发时在鼠标旁显示提示")
        self.hud.setChecked(bool(self.m.get("hud")))
        rf.addRow("", self.hud)
        lay.addWidget(rg)

        ag = QGroupBox("动作")
        av = QVBoxLayout(ag)
        self.actions = clone(self.m.get("actions") or [actions.default_action("key")])
        self.cur = -1
        lh = QHBoxLayout()
        self.alist = QListWidget()
        self.alist.setMaximumHeight(96)
        self.alist.currentRowChanged.connect(self._select_action)
        lh.addWidget(self.alist, 1)
        bcol = QVBoxLayout()
        for name, tip, fn in (("plus-circle", "添加动作", self._add_action),
                              ("arrow-up", "上移", lambda: self._move_action(-1)),
                              ("arrow-down", "下移", lambda: self._move_action(1)),
                              ("trash", "删除动作", self._del_action)):
            b = QToolButton()
            b.setIcon(icon(name, "#d2725f" if name == "trash" else "#5b6e70"))
            b.setToolTip(tip)
            b.clicked.connect(fn)
            bcol.addWidget(b)
        lh.addLayout(bcol)
        av.addLayout(lh)
        self.atype = combo(list(actions.ACTION_TYPES.items()))
        self.atype.currentIndexChanged.connect(self._type_changed)
        self.delay = spin(0, 60000, 0)
        self.delay.setSuffix(" ms")
        self.delay.setToolTip("执行此动作前等待")
        th = QHBoxLayout()
        th.addWidget(self.atype, 1)
        th.addWidget(QLabel("延迟"))
        th.addWidget(self.delay)
        av.addLayout(th)
        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        self.form.setContentsMargins(0, 0, 0, 0)
        av.addWidget(self.form_host)
        lay.addWidget(ag)
        self._refresh_alist(0)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("保存")
        bb.button(QDialogButtonBox.Ok).setObjectName("primary")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

    # --- trigger ---
    def _update_note_label(self):
        kind = self.kind.currentData()
        self.number.setMaximum(16383 if kind in ("rpn", "nrpn") else 31 if kind == "cc14" else 127)
        n = self.number.value()
        self.number.setEnabled(kind not in ("pitchwheel", "aftertouch"))
        if hasattr(self, "gesture"):
            self.gesture.setEnabled(kind in ("note", "cc"))
        if kind in ("note", "polytouch") and n >= 0:
            self.number.setSuffix(f"  ({note_name(n)})")
        else:
            self.number.setSuffix("")
        if not hasattr(self, "value_mode"):
            return
        if kind in ("cc14", "rpn", "nrpn"):
            self.value_mode.setCurrentIndex(self.value_mode.findData("full"))
        self.value_mode.setEnabled(kind == "pitchwheel")
        low, high = ((-8192, 8191) if kind == "pitchwheel" and self.value_mode.currentData() == "full"
                     else (-64, 64) if kind == "pitchwheel" else
                     (0, 16383) if kind in ("cc14", "rpn", "nrpn") else (0, 127))
        for widget in (self.vmin, self.vmax):
            unset = widget.value() == widget.minimum()
            value = widget.value()
            widget.setRange(low - 1, high)
            widget.setValue(low - 1 if unset else max(low, min(high, value)))
        for widget in (self.threshold, self.invert, self.edge_mode):
            widget.setEnabled(kind == "cc")
        self.encoding.setEnabled(kind == "cc")
        self.relative_steps.setEnabled(kind == "cc" and self.encoding.currentData() in ("rel_2c", "rel_64"))
        self.reverse.setEnabled(kind in ("cc", "cc14", "rpn", "nrpn", "pitchwheel", "aftertouch", "polytouch"))
        if kind not in ("note", "cc", "program_change") and self.event.currentData() in ("press", "release", "both"):
            self.event.setCurrentIndex(self.event.findData("change"))

    def _toggle_learn(self, on: bool):
        if on:
            self.learn_btn.setText("等待 MIDI...")
            self.engine.learn_kind = self.kind.currentData() if self.kind.currentData() in ("cc14", "rpn", "nrpn") else None
            self.engine.learn_source = self.source.currentData()
            self.engine.learn_callback = lambda ev: self.learned.emit(ev)
        else:
            self.learn_btn.setText("学习")
            self.engine.learn_callback = None
            self.engine.learn_kind = self.engine.learn_source = None

    def _apply_learn(self, ev):
        t = trigger_from_event(ev)
        self.kind.setCurrentIndex(self.kind.findData(t["kind"]))
        source = t.get("source")
        if source and self.source.findData(source) < 0:
            self.source.addItem(self.engine.source_label(source), source)
        self.source.setCurrentIndex(max(0, self.source.findData(source)))
        if "value_mode" in t:
            self.value_mode.setCurrentIndex(self.value_mode.findData(t["value_mode"]))
        self._update_note_label()
        self.channel.setValue(t["channel"])
        self.number.setValue(t["number"])
        self.event.setCurrentIndex(self.event.findData(t["event"]))
        for key, widget in (("value_min", self.vmin), ("value_max", self.vmax)):
            widget.setValue(t[key] if t.get(key) is not None else widget.minimum())
        self.edge_mode.setCurrentIndex(self.edge_mode.findData(t.get("edge_mode", "edge")))
        if self.name.text() in ("", "新映射"):
            self.name.setText(ev.describe().split(" vel")[0].split(" =")[0])
        self.learn_btn.setChecked(False)
        self._update_note_label()

    def done(self, r):
        self.engine.learn_callback = None
        self.engine.learn_kind = self.engine.learn_source = None
        super().done(r)

    # --- action list ---
    def _store_current(self):
        if 0 <= self.cur < len(self.actions):
            a = self._collect_action()
            a["delay_ms"] = self.delay.value()
            self.actions[self.cur] = a

    def _refresh_alist(self, select: int):
        self.alist.blockSignals(True)
        self.alist.clear()
        for i, a in enumerate(self.actions):
            self.alist.addItem(f"{i + 1}. {actions.describe_actions([a])}")
        self.alist.blockSignals(False)
        self.cur = -1
        if self.actions:
            self.alist.setCurrentRow(max(0, min(select, len(self.actions) - 1)))

    def _select_action(self, row: int):
        self._store_current()
        self.cur = row
        if 0 <= row < len(self.actions):
            a = self.actions[row]
            self.atype.blockSignals(True)
            self.atype.setCurrentIndex(max(0, self.atype.findData(a.get("type"))))
            self.atype.blockSignals(False)
            self.delay.setValue(int(a.get("delay_ms", 0) or 0))
            self._build_action_form(a)
            self.alist.item(row).setText(f"{row + 1}. {actions.describe_actions([a])}")

    def _add_action(self):
        self._store_current()
        self.actions.append(actions.default_action("key"))
        self._refresh_alist(len(self.actions) - 1)

    def _del_action(self):
        if len(self.actions) <= 1 or self.cur < 0:
            return
        del self.actions[self.cur]
        self.cur = -1
        self._refresh_alist(0)

    def _move_action(self, d: int):
        self._store_current()
        r = self.cur
        if r < 0 or not 0 <= r + d < len(self.actions):
            return
        self.actions[r], self.actions[r + d] = self.actions[r + d], self.actions[r]
        self._refresh_alist(r + d)

    # --- action form ---
    def _type_changed(self):
        self._build_action_form(actions.default_action(self.atype.currentData()))
        self._store_current()
        if 0 <= self.cur < len(self.actions):
            self.alist.item(self.cur).setText(f"{self.cur + 1}. {actions.describe_actions([self.actions[self.cur]])}")

    def _clear_form(self):
        while self.form.rowCount():
            self.form.removeRow(0)
        self.fields = {}

    def _add(self, key, label, widget):
        self.fields[key] = widget
        self.form.addRow(label, widget)

    def _build_action_form(self, a: dict):
        self._clear_form()
        t = a.get("type")
        if t == "key":
            keys = QComboBox()
            keys.setEditable(True)
            keys.addItems(win_input.key_names())
            keys.setEditText(a.get("keys", ""))
            keys.setToolTip("例: ctrl+c, alt+tab, f5, space, ctrl+shift+s")
            self._add("keys", "按键", keys)
            self._add("mode", "方式", combo([("tap", "点按"), ("hold", "跟随按住（按下/松开同步）"),
                                            ("down", "仅按下"), ("up", "仅松开")], a.get("mode")))
        elif t == "mouse_button":
            self._add("button", "按键", combo([("left", "左键"), ("right", "右键"), ("middle", "中键")], a.get("button")))
            self._add("mode", "方式", combo([("click", "单击"), ("hold", "跟随按住"),
                                            ("down", "仅按下"), ("up", "仅松开")], a.get("mode")))
        elif t == "mouse_move":
            self._add("dx", "X 位移", spin(-2000, 2000, a.get("dx", 0)))
            self._add("dy", "Y 位移", spin(-2000, 2000, a.get("dy", 0)))
            cb = QCheckBox("按所选数值范围缩放（中点为零位移）")
            cb.setChecked(bool(a.get("scale_by_value")))
            self._add("scale_by_value", "", cb)
        elif t == "mouse_wheel":
            self._add("amount", "格数", spin(-20, 20, a.get("amount", 1)))
            self._add("modifiers", "修饰键", self._keys_combo(a.get("modifiers", ""), "如 ctrl / alt / shift"))
            cb = QCheckBox("水平滚动")
            cb.setChecked(bool(a.get("horizontal")))
            self._add("horizontal", "", cb)
            self._add_check("follow_direction", "跟随旋钮方向（右转/左转）", a)
            self._add_check("accel", "旋钮加速（保步数编码不叠乘）", a)
        elif t == "mouse_drag":
            self._add("modifiers", "修饰键", self._keys_combo(a.get("modifiers", ""), "如 alt / ctrl+alt"))
            self._add("button", "拖动键", combo([("left", "左键"), ("right", "右键"), ("middle", "中键"),
                                                ("none", "不按鼠标")], a.get("button")))
            self._add("dx", "每格 X", spin(-500, 500, a.get("dx", 4)))
            self._add("dy", "每格 Y", spin(-500, 500, a.get("dy", 0)))
            idle = spin(50, 3000, a.get("idle_ms", 400))
            idle.setSuffix(" ms")
            idle.setToolTip("旋钮停止多久后松开按键")
            self._add("idle_ms", "停止后松开", idle)
            self._add_check("accel", "旋钮加速", a)
        elif t == "knob_keys":
            self._add("keys_inc", "右转/增大", self._keys_combo(a.get("keys_inc", "")))
            self._add("keys_dec", "左转/减小", self._keys_combo(a.get("keys_dec", "")))
            self._add_check("accel", "旋钮加速", a)
        elif t == "media":
            self._add("key", "媒体键", combo(list(actions.MEDIA_KEYS.items()), a.get("key")))
        elif t == "volume":
            self._add("mode", "方式", combo([("set_from_value", "跟随旋钮/推子数值"), ("up", "增加"),
                                            ("down", "减少"), ("mute", "静音切换")], a.get("mode")))
            self._add("step", "步进 %", spin(1, 50, a.get("step", 5)))
        elif t == "launch":
            path = QLineEdit(a.get("path", ""))
            browse = QToolButton()
            browse.setIcon(icon("folder-open"))
            browse.setToolTip("浏览")
            browse.clicked.connect(lambda: self._browse(path))
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(path, 1)
            h.addWidget(browse)
            self.fields["path"] = path
            self.form.addRow("路径", row)
            self._add("args", "参数", QLineEdit(a.get("args", "")))
        elif t == "url":
            self._add("url", "网址", QLineEdit(a.get("url", "")))
        elif t == "command":
            self._add("command", "命令", QLineEdit(a.get("command", "")))
        elif t == "midi_out":
            self._add("message", "消息", combo([("note_on", "Note On"), ("note_off", "Note Off"),
                                               ("control_change", "CC"), ("program_change", "Program")],
                                              a.get("message")))
            self._add("channel", "通道", spin(1, 16, a.get("channel", 1)))
            self._add("number", "编号", spin(0, 127, a.get("number", 60)))
            self._add("value", "数值", spin(0, 127, a.get("value", 127)))
            cb = QCheckBox("使用输入的力度/数值")
            cb.setChecked(bool(a.get("pass_value")))
            self._add("pass_value", "", cb)
        elif t == "profile":
            self._add("name", "配置", combo([(n, n) for n in ["自动"] + self.profile_names], a.get("name")))
        elif t == "set_var":
            self._add("name", "变量名", QLineEdit(a.get("name", "")))
            self._add("op", "操作", combo([("set", "设为"), ("add", "加上"), ("toggle", "0/值 切换"),
                                          ("from_value", "设为输入数值")], a.get("op")))
            self._add("value", "值", spin(-10000, 10000, a.get("value", 1)))
            self._add("min", "下限(加上时)", QLineEdit(str(a.get("min", ""))))
            self._add("max", "上限(加上时)", QLineEdit(str(a.get("max", ""))))
            cb = QCheckBox("超出范围时循环")
            cb.setChecked(bool(a.get("wrap")))
            self._add("wrap", "", cb)
        elif t == "repeat_key":
            keys = QComboBox()
            keys.setEditable(True)
            keys.addItems(win_input.key_names())
            keys.setEditText(a.get("keys", ""))
            self._add("keys", "按键", keys)
            self._add("interval_ms", "间隔 ms", spin(10, 5000, a.get("interval_ms", 100)))

    def _keys_combo(self, text: str, tip: str = "例: ctrl+c, alt+tab, ]") -> QComboBox:
        c = QComboBox()
        c.setEditable(True)
        c.addItems([""] + win_input.key_names())
        c.setEditText(text)
        c.setToolTip(tip)
        return c

    def _add_check(self, key: str, label: str, a: dict):
        cb = QCheckBox(label)
        cb.setChecked(bool(a.get(key)))
        self._add(key, "", cb)

    def _browse(self, edit: QLineEdit):
        f, _ = QFileDialog.getOpenFileName(self, "选择程序或文件")
        if f:
            edit.setText(f.replace("/", "\\"))

    def _collect_action(self) -> dict:
        a = {"type": self.atype.currentData()}
        for k, w in self.fields.items():
            if isinstance(w, QComboBox):
                a[k] = w.currentText().strip() if w.isEditable() else w.currentData()
            elif isinstance(w, QSpinBox):
                a[k] = w.value()
            elif isinstance(w, QCheckBox):
                a[k] = w.isChecked()
            elif isinstance(w, QLineEdit):
                a[k] = w.text().strip()
        return a

    def _accept(self):
        self._store_current()
        try:
            for a in self.actions:
                if a["type"] in ("key", "repeat_key"):
                    win_input.parse_combo(a["keys"])
                for k in ("modifiers", "keys_inc", "keys_dec"):
                    if a.get(k):
                        win_input.parse_combo(a[k])
            rules.parse_conditions(self.conditions.text())
        except ValueError as e:
            QMessageBox.warning(self, "设置无效", str(e))
            return
        ch, num = self.channel.value(), self.number.value()
        lo, hi = self.vmin.value(), self.vmax.value()
        lo = None if lo == self.vmin.minimum() else lo
        hi = None if hi == self.vmax.minimum() else hi
        if lo is not None and hi is not None and lo > hi:
            QMessageBox.warning(self, "设置无效", "数值范围下限不能大于上限")
            return
        self.m["name"] = self.name.text().strip() or "未命名"
        self.m["enabled"] = self.enabled.isChecked()
        self.m["trigger"].update({"kind": self.kind.currentData(), "channel": ch or None,
                             "number": None if num < 0 else num, "event": self.event.currentData(),
                             "value_min": lo, "value_max": hi,
                             "source": self.source.currentData(), "value_mode": self.value_mode.currentData(),
                             "encoding": self.encoding.currentData(),
                             "relative_steps": self.relative_steps.isChecked(), "reverse": self.reverse.isChecked(),
                             "threshold": self.threshold.value(), "invert": self.invert.isChecked(),
                             "edge_mode": self.edge_mode.currentData(),
                             "gesture": self.gesture.currentData() if self.kind.currentData() in ("note", "cc") else "any",
                             "long_ms": self.long_ms.value()})
        self.m["conditions"] = self.conditions.text().strip()
        self.m["stop"] = self.stop.isChecked()
        self.m["hud"] = self.hud.isChecked()
        self.m["actions"] = self.actions
        self.m.pop("action", None)
        self.accept()

    def result_mapping(self) -> dict:
        return self.m
