"""GUI 入口：QApplication 初始化 + 提权自重启。"""
from __future__ import annotations

import logging
import os
import sys

from .. import APP_NAME, APP_VERSION
from ..config import load_config
from ..paths import project_root
from ..tools import winproc

log = logging.getLogger(__name__)


def run(argv: list[str] | None = None) -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .main_window import MainWindow

    cfg = load_config()
    app = QApplication(argv or sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("MCSR-Launcher")

    if not _maybe_relaunch_elevated(cfg, QMessageBox):
        return 0

    window = MainWindow(cfg)
    window.show()
    return app.exec()


def _maybe_relaunch_elevated(cfg, message_box) -> bool:
    """需要提权时询问并自重启。返回 False 表示"本进程应该直接退出"。"""
    if not winproc.is_windows() or not cfg.elevate or winproc.is_self_elevated():
        return True
    if os.environ.get("MCSL_ELEVATION_DECLINED") == "1":
        return True

    answer = message_box.question(
        None, "是否以管理员身份运行？",
        "关闭 Jingle 这类“以管理员身份启动”的辅助工具需要本程序也具备管理员权限\n"
        "（否则 Windows 的 UIPI 会直接拒绝关闭请求，就像你原来的脚本那样）。\n\n"
        "现在以管理员身份重新启动？（UAC 已设为始终允许的话不会弹窗）\n\n"
        "选“否”会以普通权限继续运行，退出时只能靠提权 taskkill 补刀。",
        message_box.StandardButton.Yes | message_box.StandardButton.No,
        message_box.StandardButton.Yes,
    )
    if answer != message_box.StandardButton.Yes:
        os.environ["MCSL_ELEVATION_DECLINED"] = "1"
        return True

    pid, error = winproc.shell_execute(
        _python_launcher(), ["-m", "mcsl"],
        cwd=str(project_root()), elevate=True)
    if pid == 0:
        message_box.warning(None, "提权失败", f"无法以管理员身份重启：{error}\n将以普通权限继续运行。")
        return True
    log.info("已请求以管理员身份重启（PID %d），本进程退出", pid)
    return False


def _python_launcher() -> str:
    """优先用 pythonw.exe 启动自己（不额外弹控制台窗口）。"""
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        candidate = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.isfile(candidate):
            return candidate
    return exe
