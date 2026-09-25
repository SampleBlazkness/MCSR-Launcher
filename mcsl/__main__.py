"""python -m mcsl —— 启动图形界面。

用 pythonw 启动时没有控制台，万一启动阶段就崩了（比如依赖没装全），
屏幕上什么都不会出现。所以这里兜底一层：任何异常都写进日志，
并弹一个原生 MessageBox（不依赖 Qt，Qt 没初始化成功也能显示）。
"""
from __future__ import annotations

import logging
import sys
import traceback


def main() -> int:
    from . import APP_NAME, APP_VERSION
    from .logging_setup import log_path, setup

    try:
        setup()
        _banner(APP_NAME, APP_VERSION)
        from .ui.app import run

        return run(sys.argv[1:])
    except BaseException as exc:  # noqa: BLE001 - 启动阶段任何错误都要可见
        _crash_report(exc, locals().get("log_path"))
        return 1


def _banner(app_name: str, app_version: str) -> None:
    from .paths import config_file, data_root
    from .tools import winproc

    log = logging.getLogger("mcsl")
    log.info("%s %s 启动", app_name, app_version)
    log.info("数据目录：%s｜配置文件：%s", data_root(), config_file())
    log.info("日志文件：%s", log_path())
    log.info("是否提权：%s｜Python：%s（%s）",
             winproc.is_self_elevated(), sys.executable,
             sys.version.split()[0])


def _crash_report(exc: BaseException, log_path_fn=None) -> None:
    """把启动失败写到日志，并用原生对话框告诉用户去哪儿看。"""
    log = logging.getLogger("mcsl")
    log.error("启动失败：%s", exc)
    log.error("完整回溯：\n%s", traceback.format_exc())

    path_text = "（日志文件未能创建）"
    try:
        if log_path_fn is None:
            from .logging_setup import log_path as log_path_fn  # type: ignore[assignment]
        resolved = log_path_fn() if log_path_fn else None
        if resolved:
            path_text = str(resolved)
    except Exception:  # noqa: BLE001
        pass

    message = (
        f"MCSR-Launcher 启动失败：\n\n{type(exc).__name__}: {exc}\n\n"
        f"完整日志：{path_text}\n\n"
        "想看详细报错：双击 run-console.cmd（会保留控制台窗口）。"
    )
    _show_dialog(message)


def _show_dialog(message: str) -> None:
    """用 Win32 原生 MessageBox 显示（不依赖 Qt，也能显示中文）。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "MCSR-Launcher 启动失败", 0x10)
    except Exception:  # noqa: BLE001
        print(message, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
