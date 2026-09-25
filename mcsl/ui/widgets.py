"""通用小控件。"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QWidget)

FILE_FILTERS = {
    "jar": "Java 程序 (*.jar)",
    "exe": "可执行文件 (*.exe)",
    "dir": "",
}


class PathPicker(QWidget):
    """一行：标签 + 输入框 + 浏览按钮。"""

    changed = Signal(str)

    def __init__(self, label: str, *, mode: str = "file", filter_key: str = "jar",
                 placeholder: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode = mode
        self.filter_key = filter_key
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.label = QLabel(label)
        self.label.setMinimumWidth(96)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(True)
        self.browse = QPushButton("浏览…")
        self.browse.setFixedWidth(64)

        layout.addWidget(self.label)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.browse)

        self.edit.textChanged.connect(self.changed.emit)
        self.browse.clicked.connect(self._browse)

    def _browse(self) -> None:
        start = self.edit.text().strip() or str(Path.home())
        if self.mode == "dir":
            chosen = QFileDialog.getExistingDirectory(self, "选择目录", start)
        else:
            chosen, _ = QFileDialog.getOpenFileName(
                self, "选择文件", start, FILE_FILTERS.get(self.filter_key, "所有文件 (*)"))
        if chosen:
            self.edit.setText(str(Path(chosen)))

    def value(self) -> str:
        return self.edit.text().strip()

    def set_value(self, text: str) -> None:
        if self.edit.text() != (text or ""):
            self.edit.setText(text or "")

    def set_enabled_state(self, enabled: bool) -> None:
        for widget in (self.edit, self.browse, self.label):
            widget.setEnabled(enabled)


class LogView(QWidget):
    """日志窗格：等宽字体、自动滚到底。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QPlainTextEdit

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(5000)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)
        self.view.setFont(font)
        self.view.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        layout.addWidget(self.view)

    def append(self, text: str) -> None:
        from PySide6.QtGui import QTextCursor

        self.view.appendPlainText(text)
        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.view.setTextCursor(cursor)

    def clear(self) -> None:
        self.view.clear()

    def text(self) -> str:
        return self.view.toPlainText()
