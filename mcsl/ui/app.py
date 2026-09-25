"""GUI 入口：QApplication 初始化 + 提权自重启 + 崩溃/诊断信息的落地。

诊断三件套（用 pythonw 启动时没有控制台，任何 stderr 输出都看不到，
所以必须主动写文件，否则出问题只能靠猜）：
  1. faulthandler  → 原生崩溃（段错误/abort）的 C 层调用栈写进 logs/faulthandler.log
  2. sys.excepthook / threading.excepthook → Python 异常写进日志
  3. Qt 消息处理器 → Qt 自己的警告（跨线程调用控件、定时器线程错误等）写进日志
"""
from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
import traceback
from pathlib import Path

from .. import APP_NAME, APP_VERSION
from ..config import load_config
from ..paths import logs_dir, project_root
from ..tools import winproc

log = logging.getLogger(__name__)

# 保持引用：faulthandler 需要文件对象一直存活
_fault_stream = None


def _install_diagnostics() -> None:
    global _fault_stream
    try:
        logs_dir().mkdir(parents=True, exist_ok=True)
        path = logs_dir() / "faulthandler.log"
        _fault_stream = open(path, "a", encoding="utf-8", errors="replace")
        _fault_stream.write(f"\n===== {APP_NAME} {APP_VERSION} 启动 "
                            f"(pid={os.getpid()}, elevated={winproc.is_self_elevated()}) =====\n")
        _fault_stream.flush()
        faulthandler.enable(file=_fault_stream, all_threads=True)
    except OSError as exc:
        log.warning("无法启用 faulthandler：%s", exc)

    def _excepthook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        logging.getLogger("mcsl.crash").error("未捕获的异常：\n%s", text)
        try:
            if _fault_stream is not None:
                _fault_stream.write("\n----- Python 异常 -----\n" + text)
                _fault_stream.flush()
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = _excepthook
    threading.excepthook = lambda args: _excepthook(args.exc_type, args.exc_value, args.exc_traceback)

    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        levels = {QtMsgType.QtDebugMsg: logging.DEBUG,
                  QtMsgType.QtInfoMsg: logging.INFO,
                  QtMsgType.QtWarningMsg: logging.WARNING,
                  QtMsgType.QtCriticalMsg: logging.ERROR,
                  QtMsgType.QtFatalMsg: logging.CRITICAL}
        qt_log = logging.getLogger("qt")

        def _handler(mode, context, message) -> None:  # noqa: ANN001
            level = levels.get(mode, logging.INFO)
            qt_log.log(level, "%s", message)

        qInstallMessageHandler(_handler)
    except Exception as exc:  # noqa: BLE001
        log.warning("未能安装 Qt 消息处理器：%s", exc)


def run(argv: list[str] | None = None) -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .main_window import MainWindow

    cfg = load_config()
    app = QApplication(argv or sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("MCSR-Launcher")
    _install_diagnostics()

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
