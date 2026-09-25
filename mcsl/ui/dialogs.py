"""对话框。

**重要**：这里的槽函数全部是 QObject 的绑定方法，不用 lambda。
原因（踩过的坑）：PySide6 里把信号连到 lambda 时，lambda 没有 QObject 归属，
PySide6 会把它的执行线程判定为**发射信号的线程**，于是工作线程里的信号
直接在非 GUI 线程上改控件 → Qt 原生崩溃（段错误，没有任何 Python 回溯）。
连到主线程 QObject 的绑定方法时，Qt 才会用队列投递把调用排到主线程。
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QMessageBox,
                               QVBoxLayout, QWidget)

from ..config import AppConfig
from ..auth.manager import AccountManager
from .workers import LoginWorker, track_thread

log = logging.getLogger(__name__)


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

        self.worker = LoginWorker(cfg, accounts, cfg.auth.flow, cfg.auth.client_id)
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
        self.thread.start()

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
