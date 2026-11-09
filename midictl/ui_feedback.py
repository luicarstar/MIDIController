"""Editor for the active keyboard's application-state feedback bindings."""
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)

from .config import clone, default_feedback_binding


STATES = [("enabled", "本软件启用状态"), ("keyboard", "当前键盘配置"),
          ("profile", "当前应用 Profile"), ("variable", "本软件变量 / 层开关")]


def choices(items):
    widget = QComboBox()
    for key, text in items:
        widget.addItem(text, key)
    return widget


def number(low, high):
    widget = QSpinBox()
    widget.setRange(low, high)
    return widget


class FeedbackDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("反馈绑定 — 当前键盘配置")
        self.resize(740, 540)
        self.cfg = cfg
        self.bindings = clone(cfg.get("feedback_bindings", []))
        self.current = -1
        layout = QVBoxLayout(self)
        hint = QLabel("将本软件状态发送到当前选中的唯一 MIDI 输出端口，用于设备指示灯或程序选择。\n"
                      "不读取任意第三方软件状态，不支持厂商灯屏、电动推子或 SysEx。\n"
                      "请给反馈分配独立通道/编号；近期同值回流会被忽略，避免触发输入映射。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        body = QHBoxLayout()
        layout.addLayout(body)
        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._select)
        left.addWidget(self.list)
        buttons = QHBoxLayout()
        add = QPushButton("添加")
        delete = QPushButton("删除")
        add.clicked.connect(self._add)
        delete.clicked.connect(self._delete)
        buttons.addWidget(add)
        buttons.addWidget(delete)
        left.addLayout(buttons)
        body.addLayout(left, 1)
        self.editor = QWidget()
        form = QFormLayout(self.editor)
        self.name = QLineEdit()
        self.enabled = QCheckBox("启用此反馈")
        self.state = choices(STATES)
        self.variable = QLineEdit()
        self.variable.setPlaceholderText("例：shift、mode")
        self.mode = choices([("on_off", "开 / 关值"), ("value", "直接数值（限制至0..127）")])
        self.target = QComboBox()
        self.on_value, self.off_value = number(0, 127), number(0, 127)
        self.message = choices([("note_on", "Note（关值0即松开）"),
                                ("control_change", "CC"), ("program_change", "Program")])
        self.channel, self.control = number(1, 16), number(0, 127)
        for label, widget in (("名称", self.name), ("", self.enabled), ("状态类型", self.state),
                              ("来源变量", self.variable), ("值映射", self.mode),
                              ("匹配目标 / 值", self.target), ("开值", self.on_value),
                              ("关值", self.off_value), ("输出类型", self.message),
                              ("通道", self.channel), ("Note / CC编号", self.control)):
            form.addRow(label, widget)
        detail = QLabel("变量开关：匹配值留空表示非零为开，也可填精确数值。\n"
                        "键盘/Profile按所选目标判断开关。Program用映射值作为程序编号，\n"
                        "包括关值；其余类型使用上面的固定Note/CC编号。\n"
                        "绑定只属于当前键盘；新建为空，复制键盘时一并复制。最多128项。")
        detail.setWordWrap(True)
        form.addRow(detail)
        body.addWidget(self.editor, 2)
        self.state.currentIndexChanged.connect(lambda: self._state_fields())
        self.mode.currentIndexChanged.connect(self._mode_fields)
        self.message.currentIndexChanged.connect(lambda: self.control.setEnabled(
            self.message.currentData() != "program_change"))
        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self._refresh(0)

    def _state_fields(self, target=""):
        kind = self.state.currentData()
        self.variable.setEnabled(kind == "variable")
        self.target.clear()
        self.target.setEditable(kind == "variable")
        items = ([(k["id"], k["name"]) for k in self.cfg["keyboards"]] if kind == "keyboard" else
                 [(p["name"], p["name"]) for p in self.cfg["profiles"]] if kind == "profile" else [])
        for key, text in items:
            self.target.addItem(text, key)
        if kind == "variable":
            self.target.setEditText(str(target))
        elif target:
            index = self.target.findData(target)
            if index < 0:
                self.target.addItem(str(target) + "（当前不存在）", target)
                index = self.target.count() - 1
            self.target.setCurrentIndex(index)
        if kind in ("keyboard", "profile"):
            self.mode.setCurrentIndex(0)
        self.mode.setEnabled(kind in ("enabled", "variable"))
        self._mode_fields()

    def _mode_fields(self):
        on_off = self.mode.currentData() == "on_off"
        self.on_value.setEnabled(on_off)
        self.off_value.setEnabled(on_off)
        self.target.setEnabled(on_off and self.state.currentData() != "enabled")

    def _store(self):
        if not 0 <= self.current < len(self.bindings):
            return
        binding = self.bindings[self.current]
        binding.update(name=self.name.text().strip() or "反馈", enabled=self.enabled.isChecked(),
                       state=self.state.currentData(), variable=self.variable.text().strip(),
                       mode=self.mode.currentData(), match=(self.target.currentText().strip()
                       if self.target.isEditable() else self.target.currentData() or ""),
                       on_value=self.on_value.value(), off_value=self.off_value.value(),
                       message=self.message.currentData(), channel=self.channel.value(), number=self.control.value())
        self.list.item(self.current).setText(binding["name"])

    def _select(self, row):
        self._store()
        self.current = row
        self.editor.setEnabled(0 <= row < len(self.bindings))
        if not self.editor.isEnabled():
            return
        binding = self.bindings[row]
        self.name.setText(binding.get("name", "反馈"))
        self.enabled.setChecked(binding.get("enabled", True))
        self.state.blockSignals(True)
        self.state.setCurrentIndex(max(0, self.state.findData(binding.get("state", "enabled"))))
        self.state.blockSignals(False)
        self.variable.setText(binding.get("variable", ""))
        self.mode.setCurrentIndex(max(0, self.mode.findData(binding.get("mode", "on_off"))))
        self._state_fields(binding.get("match", ""))
        self.on_value.setValue(int(binding.get("on_value", 127)))
        self.off_value.setValue(int(binding.get("off_value", 0)))
        self.message.setCurrentIndex(max(0, self.message.findData(binding.get("message", "control_change"))))
        self.control.setEnabled(self.message.currentData() != "program_change")
        self.channel.setValue(int(binding.get("channel", 1)))
        self.control.setValue(int(binding.get("number", 20)))

    def _refresh(self, row):
        self.current = -1
        self.list.blockSignals(True)
        self.list.clear()
        self.list.addItems([b.get("name", "反馈") for b in self.bindings])
        self.list.blockSignals(False)
        if self.bindings:
            self.list.setCurrentRow(min(row, len(self.bindings) - 1))
        else:
            self.editor.setEnabled(False)

    def _add(self):
        self._store()
        if len(self.bindings) >= 128:
            QMessageBox.information(self, "反馈绑定", "最多128项反馈绑定")
            return
        self.bindings.append(default_feedback_binding())
        self._refresh(len(self.bindings) - 1)

    def _delete(self):
        if 0 <= self.current < len(self.bindings):
            del self.bindings[self.current]
            self._refresh(0)

    def _accept(self):
        self._store()
        if any(b.get("state") == "variable" and not b.get("variable") for b in self.bindings):
            QMessageBox.warning(self, "反馈绑定", "变量反馈需要填写来源变量名")
            return
        self.accept()
