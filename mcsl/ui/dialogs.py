"""对话框。

**重要**：这里的槽函数全部是 QObject 的绑定方法，不用 lambda。
原因（踩过的坑）：PySide6 里把信号连到 lambda 时，lambda 没有 QObject 归属，
PySide6 会把它的执行线程判定为**发射信号的线程**，于是工作线程里的信号
直接在非 GUI 线程上改控件 → Qt 原生崩溃（段错误，没有任何 Python 回溯）。
连到主线程 QObject 的绑定方法时，Qt 才会用队列投递把调用排到主线程。
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDialog,
                               QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView,
                               QLabel, QMessageBox, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..config import AppConfig
from ..auth.manager import AccountManager
from ..launch.java import (JavaInstall, find_broken_java_dirs, find_java_installations,
                           pick_best, read_install)
from .workers import LoginWorker, track_object, track_thread

log = logging.getLogger(__name__)

# 对话框也要活到它的工作线程结束：否则 Python 包装对象先被回收，
# worker 的 Python 引用随之消失 → C++ 对象被提前删除（同上）。
_LIVE_DIALOGS: list[QDialog] = []


class LoginDialog(QDialog):
    """微软登录（设备码 / 浏览器授权）。

    对话框自己持有工作线程；成功后发出 succeeded(name, uuid)，由主窗口刷新账号栏。
    """

    succeeded = Signal(str, str)

    def __init__(self, cfg: AppConfig, accounts: AccountManager,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.accounts = accounts
        self._cancelled = False
        self._done = False

        self.setWindowTitle("登录微软账号")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        self.head = QLabel("正在向微软请求授权…")
        self.head.setWordWrap(True)
        layout.addWidget(self.head)

        self.code_label = QLabel("")
        code_font = QFont("Consolas")
        code_font.setPointSize(22)
        code_font.setBold(True)
        self.code_label.setFont(code_font)
        self.code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.code_label)

        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setOpenExternalLinks(True)
        layout.addWidget(self.detail)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.worker = track_object(LoginWorker(cfg, accounts, cfg.auth.flow, cfg.auth.client_id))
        # 线程不挂父对象：即使对话框先被回收，也由 _ACTIVE_THREADS 持住，
        # 不会被 Qt 当成"运行中却被销毁"而 abort
        self.thread = track_thread(QThread())
        self.worker.moveToThread(self.thread)
        # ↓ 全部用绑定方法（本对话框在主线程），lambda 会跑在工作线程里 → 崩
        self.worker.status.connect(self.on_status)
        self.worker.device_code.connect(self.on_device_code)
        self.worker.failed.connect(self.on_failed)
        self.worker.success.connect(self.on_success)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.started.connect(self.worker.run)
        self.thread.finished.connect(self._on_thread_finished)
        _LIVE_DIALOGS.append(self)
        self.thread.start()

    @Slot()
    def _on_thread_finished(self) -> None:
        """工作线程结束后才允许回收本对话框。"""
        for index, dialog in enumerate(_LIVE_DIALOGS):
            if dialog is self:
                del _LIVE_DIALOGS[index]
                break

    # ------------------------------------------------------------ 槽
    @Slot(str)
    def on_status(self, message: str) -> None:
        log.info("%s", message)
        self.head.setText(message)

    @Slot(str, str, int)
    def on_device_code(self, code: str, uri: str, expires_in: int) -> None:
        self.code_label.setText(code)
        minutes = max(1, int(expires_in) // 60)
        self.detail.setText(
            f"在浏览器打开 <a href='{uri}'>{uri}</a> 并输入上面的代码（{minutes} 分钟内有效）。\n"
            "登录成功后这个窗口会自己关闭；以后不用再登。")

    @Slot(str)
    def on_failed(self, message: str) -> None:
        if self._cancelled:
            log.info("登录已取消：%s", message)
            return
        self._done = True
        log.warning("登录失败：%s", message)
        QMessageBox.critical(self, "登录失败", message)
        self.reject()

    @Slot(str, str)
    def on_success(self, name: str, uuid: str) -> None:
        self._done = True
        log.info("登录成功：%s", name)
        self.succeeded.emit(name, uuid)
        self.accept()

    # ------------------------------------------------------------ 生命周期
    def _cancel_and_wait(self, timeout_ms: int = 20000) -> None:
        """取消登录并等线程收尾，避免"线程还在跑就把对象销毁"。"""
        self._cancelled = True
        self.worker.cancel.cancel()
        self.thread.quit()
        if not self.thread.wait(timeout_ms):
            # 请求可能正卡在网络上（DNS/代理）。这里不能销毁它：
            # 交给 _ACTIVE_THREADS 持有，等它自己结束后再释放。
            log.warning("登录线程未在 %d ms 内结束，转由后台收尾（不会再被销毁）",
                        timeout_ms)

    def reject(self) -> None:  # noqa: D102 - 覆盖 QDialog
        self._cancel_and_wait()
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        self._cancel_and_wait()
        super().closeEvent(event)


class JavaPickerDialog(QDialog):
    """选择 Java 运行环境：列出本机所有 Java，标出与当前实例是否匹配。

    为什么要有这个对话框：旧的"自动检测本机 Java"是把找到的候选项按**目录名字符串**
    排序取最后一个（"zulu-8" 排在 "zulu-25" 之后），于是永远挑中 zulu-8；正确做法是
    读出版本号，再按实例声明的 Java 大版本匹配，最后由人确认。
    """

    def __init__(self, *, instance: str, required_major: int | None, current_path: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择 Java 运行环境")
        self.resize(980, 520)
        self.instance = instance
        self.required_major = required_major
        self.current_path = (current_path or "").strip()
        self.selected: JavaInstall | None = None
        self._installs: list[JavaInstall] = []
        self._broken: list[Path] = []

        layout = QVBoxLayout(self)

        need = QLabel(
            f"实例 <b>{instance}</b> 声明需要 <b>Java {required_major}</b>"
            if required_major else
            f"实例 <b>{instance}</b> 没有声明 Java 版本要求（任意 Java 8+ 都可以）")
        need.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(need)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.only_match = QCheckBox("只显示与当前实例匹配的版本")
        self.only_match.toggled.connect(self._refresh_table)
        layout.addWidget(self.only_match)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["版本", "是否匹配", "厂商", "架构", "路径", "来源"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        for column in (0, 1, 2, 3, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.cellDoubleClicked.connect(self._on_row_activated)
        layout.addWidget(self.table, 1)

        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setVisible(False)
        layout.addWidget(self.note)

        buttons = QHBoxLayout()
        self.use_button = QPushButton("使用所选")
        self.use_button.setDefault(True)
        self.browse_button = QPushButton("手动选择 java.exe…")
        self.rescan_button = QPushButton("重新检测")
        cancel_button = QPushButton("取消")
        self.use_button.clicked.connect(self._on_use)
        self.browse_button.clicked.connect(self._on_browse)
        self.rescan_button.clicked.connect(self._on_rescan)
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.use_button)
        buttons.addWidget(self.browse_button)
        buttons.addStretch(1)
        buttons.addWidget(self.rescan_button)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)

        self._rescan()

    # ------------------------------------------------------------------ 扫描
    @Slot()
    def _on_rescan(self) -> None:
        """按钮槽：重新扫一遍（与 _rescan 分开，避免信号名不一致的坑）。"""
        self._rescan()

    @Slot()
    def _rescan(self) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._installs = find_java_installations([self.current_path])
            self._broken = find_broken_java_dirs()
        finally:
            QApplication.restoreOverrideCursor()
        log.info("Java 检测：%d 个可用，%d 个可疑目录", len(self._installs), len(self._broken))
        self._refresh_table()

    @Slot()
    def _refresh_table(self) -> None:
        installs = self._installs
        if self.only_match.isChecked() and self.required_major:
            installs = [i for i in installs if i.matches(self.required_major)]
        installs = sorted(installs, key=lambda i: i.sort_key(self.required_major))

        current = str(Path(self.current_path)).lower() if self.current_path else ""
        self.table.setRowCount(len(installs))
        for row, inst in enumerate(installs):
            matched = inst.matches(self.required_major)
            label = inst.version_label
            if current and str(inst.path).lower() == current:
                label += "（当前使用）"
            if self.required_major:
                match_text = "✓ 匹配" if matched else f"✗ 需要 Java {self.required_major}"
            else:
                match_text = "—"
            cells = [label, match_text, inst.vendor_label, inst.arch, str(inst.path), inst.source]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, inst)
                if not matched:
                    item.setForeground(QBrush(QColor("#8a8a8a")))
                self.table.setItem(row, col, item)

        self._update_summary()

        best = pick_best(installs, self.required_major)
        if best is not None:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item is not None and item.data(Qt.ItemDataRole.UserRole) == best:
                    self.table.selectRow(row)
                    break
        self._on_selection_changed()

    def _update_summary(self) -> None:
        total = len(self._installs)
        if not total:
            self.summary.setText(
                "没有在本机找到可用的 Java。请点“手动选择 java.exe…”指定，或先装一个"
                "（1.16.1 需要 Java 8，26.x 需要 Java 21/25）。")
        else:
            matched = sum(1 for i in self._installs if i.matches(self.required_major))
            text = f"共检测到 <b>{total}</b> 个 Java"
            if self.required_major:
                text += f"，其中 <b>{matched}</b> 个匹配 Java {self.required_major}"
                if not matched:
                    text += "。没有匹配的版本，直接启动很可能失败。"
            self.summary.setText(text + "。列表按“匹配优先、版本从高到低”排序。")
        if self._broken:
            names = "、".join(str(p) for p in self._broken[:4])
            self.note.setText(f"⚠ 这些目录看着像 Java 但里面没有 bin\\java.exe（可能装坏了）：{names}")
            self.note.setVisible(True)
        else:
            self.note.setVisible(False)

    # ------------------------------------------------------------------ 交互
    @Slot()
    def _on_selection_changed(self) -> None:
        self.use_button.setEnabled(self.table.currentRow() >= 0)

    @Slot(int, int)
    def _on_row_activated(self, row: int, _column: int) -> None:
        self.table.selectRow(row)
        self._on_use()

    @Slot()
    def _on_use(self) -> None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        install = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if install is None:
            return
        self.selected = install
        log.info("用户选择 Java %s（%s）", install.version_label, install.path)
        self.accept()

    @Slot()
    def _on_browse(self) -> None:
        start = str(Path(self.current_path).parent) if self.current_path else "C:/Program Files"
        chosen, _ = QFileDialog.getOpenFileName(
            self, "选择 java.exe", start, "Java 可执行文件 (java.exe);;所有文件 (*)")
        if not chosen:
            return
        install = read_install(Path(chosen), "手动选择")
        if install is None:
            QMessageBox.warning(self, "不是可用的 Java",
                                f"读不出这个文件的版本信息，它可能不是 java.exe：\n{chosen}")
            return
        if all(str(i.path).lower() != str(install.path).lower() for i in self._installs):
            self._installs.insert(0, install)
        self._refresh_table()
        self.selected = install
        self.accept()
