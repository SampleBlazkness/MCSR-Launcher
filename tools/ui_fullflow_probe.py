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

faulthandler.enable()
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
    app = QApplication(sys.argv[:1])
    window = MainWindow(load_config())
    window.show()
    state: dict[str, object] = {}

    def click_login() -> None:
        print(">>> 点击登录（走真实线程，链路上全是假数据）", flush=True)
        window.login_button.click()

    def report() -> None:
        acc = window.accounts.selected
        print(">>> 账号栏:", window.account_label.text(), flush=True)
        print(">>> 已保存账号:", acc.name if acc else None,
              "| 有 refresh_token:", bool(acc.refresh_token) if acc else None, flush=True)
        print(">>> 日志末行:", window.log_view.text().strip().splitlines()[-1:], flush=True)
        print(">>> [ok] 全程存活", flush=True)
        state["checked"] = True

    def finish() -> None:
        print(">>> 结束探针", flush=True)
        app.quit()

    QTimer.singleShot(500, click_login)
    QTimer.singleShot(6000, report)
    QTimer.singleShot(9000, finish)
    app.exec()
    print(">>> 是否走到检查点:", bool(state.get("checked")), flush=True)
    return 0 if state.get("checked") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:
        traceback.print_exc()
        raise
