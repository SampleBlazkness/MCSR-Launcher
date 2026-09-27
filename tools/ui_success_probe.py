"""临时探针：伪造一次成功的登录，跑完"登录成功"之后的全部界面路径。

    python tools/ui_success_probe.py

用于定位"浏览器显示登录成功后启动器崩溃"的问题：真实登录要用户操作，
这里把 AccountManager 换成一个立即成功的假对象，其余代码路径（对话框成功回调、
账号栏刷新、令牌预热、窗口关闭）都是真的。
"""
from __future__ import annotations

import faulthandler
import os
import sys
import tempfile
import time
from pathlib import Path

faulthandler.enable()

os.environ["MCSL_DATA_DIR"] = str(Path(tempfile.gettempdir()) / "mcsl-successprobe")
os.environ["MCSL_NO_ELEVATE"] = "1"   # 测试用：跳过提权自重启
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer                                  # noqa: E402
from PySide6.QtWidgets import QApplication                         # noqa: E402

from mcsl.auth.store import Account                                # noqa: E402
from mcsl.config import load_config                                # noqa: E402
from mcsl.logging_setup import setup                               # noqa: E402
from mcsl.ui.main_window import MainWindow                         # noqa: E402


class FakeSession:
    def __init__(self) -> None:
        self.uuid = "f86a3ab6163c420197e5dfc196f6fd91"
        self.name = "FakePlayer"
        self.access_token = "FAKE-TOKEN"
        self.token_type = "Bearer"
        self.client_id = "00000000402b5328"
        self.xuid = "2535405000000000"
        self.expires_at = time.time() + 3600
        self.refresh_token = "FAKE-REFRESH"


class FakeAccounts:
    """只实现主窗口/登录对话框真正用到的那几个方法。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._acc: Account | None = None
        self.acquire_calls = 0

    @property
    def selected(self):
        return self._acc

    @property
    def accounts(self):
        return [self._acc] if self._acc else []

    def select(self, uuid: str) -> None:
        pass

    def logout(self, uuid: str) -> None:
        self._acc = None

    def login(self, **kwargs):
        on_status = kwargs.get("on_status") or (lambda m: None)
        on_status("模拟：正在登录 Xbox Live 与 Minecraft…")
        acc = Account(uuid="f86a3ab6163c420197e5dfc196f6fd91", name="FakePlayer",
                      refresh_token="FAKE-REFRESH", client_id="00000000402b5328",
                      session={"access_token": "FAKE-TOKEN", "token_type": "Bearer",
                               "xuid": "2535405000000000", "expires_at": time.time() + 3600})
        self._acc = acc
        on_status("模拟：登录完成")
        return acc

    def acquire_session(self, *args, **kwargs):
        self.acquire_calls += 1
        on_status = kwargs.get("on_status") or (lambda m: None)
        on_status("模拟：复用缓存令牌")
        return FakeSession()


def main() -> int:
    setup()
    app = QApplication(sys.argv[:1])
    cfg = load_config()
    window = MainWindow(cfg)
    window.accounts = FakeAccounts(cfg.auth)     # 换成假账号管理器
    window.show()
    marks: list[str] = []

    def step_login() -> None:
        marks.append("clicked")
        print(">>> 点击登录", flush=True)
        window.login_button.click()

    def check_state() -> None:
        marks.append("checked")
        print(">>> 账号栏:", window.account_label.text(), flush=True)
        print(">>> 令牌栏:", window.token_label.text(), flush=True)
        print(">>> acquire_session 调用次数:", window.accounts.acquire_calls, flush=True)
        print(">>> 日志窗格末尾:", window.log_view.text().strip().splitlines()[-1:], flush=True)
        print(">>> [ok] 登录成功后进程仍存活", flush=True)

    def step_close() -> None:
        print(">>> 关闭主窗口", flush=True)
        window.close()

    def finish() -> None:
        print(">>> 结束探针", flush=True)
        app.quit()

    QTimer.singleShot(500, step_login)
    QTimer.singleShot(4000, check_state)
    QTimer.singleShot(7000, step_close)
    QTimer.singleShot(10000, finish)
    app.exec()
    print(">>> 标记:", marks, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
