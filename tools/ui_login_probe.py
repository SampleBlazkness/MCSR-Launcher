"""回归探针：真实点击界面上的"登录微软账号"，走完设备码那一段信号往返。

    python tools/ui_login_probe.py

为什么需要它：PySide6 里把信号连到 **lambda** 时，lambda 没有 QObject 归属，
PySide6 会按"发射信号的线程"来执行它 —— 工作线程里的信号直接在非 GUI 线程
改控件，结果是 **段错误**（没有任何 Python 回溯，日志也断在那里）。
这类 bug 只有真的走一遍信号往返才会暴露，普通 import / offscreen 冒烟都发现不了。

本探针会：连网请求一次设备码 → 等对话框显示出来 → 关掉它 → 退出。
不完成登录（不会碰你的账号），退出码 0 = 没有崩溃。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 段错误没有 Python 回溯，faulthandler 能在崩溃时打出 C 层的调用栈
import faulthandler

faulthandler.enable()

# 独立数据目录 + 不提权，避免动到真实配置与账号
os.environ.setdefault("MCSL_DATA_DIR", str(Path(tempfile.gettempdir()) / "mcsl-loginprobe"))
os.environ["MCSL_ELEVATION_DECLINED"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer                                  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog                # noqa: E402

from mcsl.config import load_config                                # noqa: E402
from mcsl.logging_setup import setup                               # noqa: E402
from mcsl.ui.main_window import MainWindow                         # noqa: E402

MARK = {}


def main() -> int:
    early = "--early" in sys.argv        # 请求还没返回就取消（曾经的崩溃场景）
    setup()
    app = QApplication(sys.argv[:1])
    window = MainWindow(load_config())
    window.show()

    def click_login() -> None:
        print(">>> 点击登录按钮", flush=True)
        window.login_button.click()

    def check_dialog() -> None:
        dialogs = [w for w in app.topLevelWidgets()
                   if isinstance(w, QDialog) and w.isVisible()]
        if not dialogs:
            print(">>> [FAIL] 没有出现登录对话框（可能已经崩了）", flush=True)
            MARK["failed"] = True
            app.quit()
            return
        dialog = dialogs[0]
        code = getattr(dialog, "code_label", None)
        text = code.text() if code is not None else ""
        if text:
            print(f">>> [ok] 设备码对话框正常显示：{text}", flush=True)
        else:
            print(">>> [FAIL] 对话框出现了但没拿到设备码", flush=True)
            MARK["failed"] = True
        print(">>> 关闭对话框（取消登录）", flush=True)
        dialog.reject()
        print(">>> reject 返回", flush=True)
        QTimer.singleShot(1500, probe_alive)

    def cancel_early() -> None:
        """请求还在网络上时就取消：不能让线程被提前销毁。"""
        dialogs = [w for w in app.topLevelWidgets()
                   if isinstance(w, QDialog) and w.isVisible()]
        if dialogs:
            print(">>> 设备码还没回来就取消登录", flush=True)
            dialogs[0].reject()
            print(">>> reject 返回（请求仍在后台）", flush=True)
            QTimer.singleShot(3000, probe_alive)
        else:
            print(">>> [FAIL] 没有对话框可取消", flush=True)
            MARK["failed"] = True

    def probe_alive() -> None:
        print(">>> [ok] 取消之后进程仍存活", flush=True)

    def finish() -> None:
        print(">>> 结束探针", flush=True)
        app.quit()

    QTimer.singleShot(500, click_login)
    if early:
        QTimer.singleShot(2000, cancel_early)
        QTimer.singleShot(12000, finish)
    else:
        QTimer.singleShot(12000, check_dialog)
        QTimer.singleShot(20000, finish)
    app.exec()
    return 1 if MARK.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
