"""临时探针：把认证链全部换成假数据，跑真实的登录线程 + 对话框 + 账号保存流程。

复现"浏览器登录成功后启动器窗口消失"的时序问题，不联网、不需要用户操作。
"""
from __future__ import annotations

import faulthandler
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
# 段错误没有 Python 回溯，faulthandler 能在崩溃时打出 C 层的调用栈。
# 必须指定文件：pythonw 下没有有效 stderr 时 faulthandler.enable() 会直接抛异常，
# 表现成"莫名其妙 exit 1"（真程序里用的是 logs/faulthandler.log）。
_fault_file = open(Path(tempfile.gettempdir()) / "mcsl-fullflow-faults.txt", "a",
                   encoding="utf-8", errors="replace")
faulthandler.enable(file=_fault_file, all_threads=True)
os.environ["MCSL_DATA_DIR"] = str(Path(tempfile.gettempdir()) / "mcsl-fullflow")
os.environ["MCSL_ELEVATION_DECLINED"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer                                  # noqa: E402
from PySide6.QtWidgets import QApplication                         # noqa: E402

import mcsl.auth.minecraft as mc_mod                               # noqa: E402
import mcsl.auth.oauth as oauth_mod                                # noqa: E402
import mcsl.auth.xbox as xbox_mod                                  # noqa: E402
from mcsl.auth.minecraft import McToken, Profile                   # noqa: E402
from mcsl.auth.oauth import DeviceCode, Tokens                     # noqa: E402
from mcsl.auth.xbox import XblToken, XstsToken                     # noqa: E402
from mcsl.config import load_config                                # noqa: E402
from mcsl.logging_setup import setup                               # noqa: E402
from mcsl.ui.main_window import MainWindow                         # noqa: E402

UUID = "f86a3ab6163c420197e5dfc196f6fd91"
LOG_FILE = Path(tempfile.gettempdir()) / "mcsl-fullflow-probe.txt"


def note(text: str) -> None:
    """探针自己的输出：写文件。

    不用 print —— pythonw 下没有 stdout/stderr，进度和异常都会无声丢掉，
    之前正是因此把探针自身的错误看成了"程序莫名其妙退出"。
    """
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError:
        pass


def fake_patches() -> None:
    def request_device_code(session, client_id, endpoints, scope=None):
        time.sleep(0.3)
        return DeviceCode(device_code="DC", user_code="FAKE1234",
                          verification_uri="https://www.microsoft.com/link",
                          interval=1, expires_in=900)

    def poll_device_code(session, client_id, endpoints, info, **kwargs):
        time.sleep(0.6)                      # 模拟浏览器里完成登录
        return Tokens("msa-access-token", "msa-refresh-token", 3600)

    oauth_mod.request_device_code = request_device_code
    oauth_mod.poll_device_code = poll_device_code
    xbox_mod.xbl_authenticate = lambda tok, session: XblToken("xbl-token", "uhs-1")
    xbox_mod.xsts_authorize = lambda xbl, session: XstsToken("xsts-token", "uhs-1", "2535405000000000")
    mc_mod.login_with_xbox = lambda xsts, session: McToken("mc-access-token", "Bearer",
                                                           time.time() + 86400, "hash")
    mc_mod.fetch_profile = lambda mc, session: Profile(UUID, "FakePlayer")


def main() -> int:
    fake_patches()
    setup()
    LOG_FILE.unlink(missing_ok=True)
    app = QApplication(sys.argv[:1])
    window = MainWindow(load_config())
    window.show()
    state: dict[str, object] = {}

    def note_safe(text: str) -> None:
        note(text)

    def click_login() -> None:
        note(">>> 点击登录（走真实线程，链路上全是假数据）")
        window.login_button.click()

    def report() -> None:
        # 这里出异常必须看得见：pythonw 下没有 stderr，异常会被无声吞掉
        try:
            acc = window.accounts.selected
            note(f">>> 账号栏: {window.account_label.text()}")
            note(f">>> 已保存账号: {acc.name if acc else None}"
                 f" | 有 refresh_token: {bool(acc.refresh_token) if acc else None}")
            note(f">>> 日志末行: {window.log_view.text().strip().splitlines()[-1:]}")
            note(">>> [ok] 全程存活")
            state["checked"] = True
        except BaseException:
            note(">>> [FAIL] 检查点抛异常:\n" + traceback.format_exc())

    def finish() -> None:
        note(">>> 结束探针")
        app.quit()

    QTimer.singleShot(500, click_login)
    QTimer.singleShot(6000, report)
    QTimer.singleShot(9000, finish)
    app.exec()
    note_safe(f">>> 是否走到检查点: {bool(state.get('checked'))}")
    return 0 if state.get("checked") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        note(">>> [FAIL] 未捕获异常:\n" + traceback.format_exc())
        raise
