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
    _install_icon(app)

    if not _maybe_relaunch_elevated(cfg, QMessageBox):
        return 0

    window = MainWindow(cfg)
    window.show()
    return app.exec()


def _install_icon(app) -> None:
    """设置窗口/任务栏图标（来自 Minecraft 客户端自带的 pack.png）。"""
    from PySide6.QtGui import QIcon

    from ..paths import icon_path

    path = icon_path()
    if path.is_file():
        icon = QIcon(str(path))
        if not icon.isNull():
            app.setWindowIcon(icon)
            log.debug("已设置窗口图标：%s", path)
        else:
            log.warning("图标文件无法解析：%s", path)
    else:
        log.info("没有图标文件（可运行 tools/make_icon.py 生成）：%s", path)


def _maybe_relaunch_elevated(cfg, message_box) -> bool:
    """始终以管理员身份启动（不再询问）。

    返回 False 表示本进程应该直接退出（提权被拒且用户选择退出）。
    """
    if not winproc.is_windows() or winproc.is_self_elevated():
        return True
    # 测试/无头调试用逃生口（正常使用永远不会设置它）
    if os.environ.get("MCSL_NO_ELEVATE") == "1":
        log.debug("MCSL_NO_ELEVATE=1，跳过提权")
        cfg.elevate = False
        return True

    # 直接请求提权重启，不弹询问框
    pid, error = winproc.shell_execute(
        _python_launcher(), ["-m", "mcsl"],
        cwd=str(project_root()), elevate=True)
    if pid != 0:
        log.info("已以管理员身份重启（PID %d），本进程退出", pid)
        return False

    # 提权失败：常见原因是用户在 UAC 上点了"否"（错误码 1223）
    log.warning("提权启动失败：%s", error)
    denied = "1223" in error or "取消" in error
    detail = ("你在 UAC 提示上选择了“否”。" if denied else error)
    box = message_box()
    box.setIcon(message_box.Icon.Critical)
    box.setWindowTitle("需要管理员权限")
    box.setText("启动器需要管理员权限")
    box.setInformativeText(
        f"{detail}\n\n"
        "没有管理员权限时：\n"
        "· 无法关闭 Jingle / OBS 这类以管理员身份运行的工具（Windows 的 UIPI 会拒绝）\n"
        "· 游戏本身仍可启动，但退出后的清理会不完整\n\n"
        "请选择“重试”重新请求提权，或“仍以普通权限启动”。")
    retry = box.addButton("重试", message_box.ButtonRole.AcceptRole)
    limited = box.addButton("仍以普通权限启动", message_box.ButtonRole.DestructiveRole)
    box.addButton("退出", message_box.ButtonRole.RejectRole)
    box.setDefaultButton(retry)
    box.exec()

    chosen = box.clickedButton()
    if chosen is retry:
        return _maybe_relaunch_elevated(cfg, message_box)
    if chosen is limited:
        log.warning("用户选择以普通权限继续运行（清理功能受限）")
        cfg.elevate = False
        return True
    log.info("用户选择退出（未取得管理员权限）")
    return False


def _python_launcher() -> str:
    """优先用 pythonw.exe 启动自己（不额外弹控制台窗口）。"""
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        candidate = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.isfile(candidate):
            return candidate
    return exe
