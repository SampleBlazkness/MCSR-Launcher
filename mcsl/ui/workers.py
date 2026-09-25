"""把耗时的认证 / 启动 / 清理放到工作线程，通过 signal 回主线程更新界面。"""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QObject, Signal, Slot

from ..auth.errors import AuthError
from ..auth.manager import AccountManager
from ..auth.oauth import CancelToken
from ..config import AppConfig
from ..launch.launcher import LaunchService

log = logging.getLogger(__name__)

# 运行中的线程必须有人持有引用：QThread 对象在工作线程还没结束时被回收，
# Qt 会直接 abort（没有 Python 回溯，日志也断在半路，退出码 127/139）。
# 统一登记在这里，线程真正结束（finished）后才摘掉。
_ACTIVE_THREADS: set[QThread] = set()


def track_thread(thread: QThread) -> QThread:
    _ACTIVE_THREADS.add(thread)
    thread.finished.connect(lambda: _ACTIVE_THREADS.discard(thread))
    return thread


class LaunchWorker(QObject):
    """准备 + 启动游戏（在后台线程执行，避免界面卡死）。"""

    status = Signal(str)
    log_line = Signal(str)
    failed = Signal(str)
    service_ready = Signal(object)
    game_exited = Signal(int, float)
    finished = Signal()

    def __init__(self, cfg: AppConfig, accounts: AccountManager) -> None:
        super().__init__()
        self.cfg = cfg
        self.accounts = accounts
        self.service: LaunchService | None = None
        self.force_refresh = False

    @Slot()
    def run(self) -> None:
        try:
            service = LaunchService(self.cfg, self.accounts,
                                    on_log=self.log_line.emit,
                                    on_status=self.status.emit)
            self.service = service
            version, command = service.prepare(force_refresh=self.force_refresh)
            self.log_line.emit(f"实例 {version.id}｜Java 要求 {version.java_major}｜"
                               f"classpath {len(command.classpath)} 项")
            if command.missing:
                self.log_line.emit(f"⚠ 有 {len(command.missing)} 个库文件缺失，"
                                   "游戏可能启动失败（详情见日志文件）")
            self.status.emit("正在启动辅助工具…")
            service.start_tools()
            service.start_game(command, on_game_exit=self._on_game_exit)
            self.status.emit("Minecraft 运行中")
            self.service_ready.emit(service)
        except (AuthError, FileNotFoundError, ValueError, RuntimeError, OSError) as exc:
            log.exception("启动失败")
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜底，保证界面不会静默卡住
            log.exception("启动时出现未预期错误")
            self.failed.emit(f"未预期的错误：{exc}")
        finally:
            self.finished.emit()

    def _on_game_exit(self, code: int, duration: float) -> None:
        self.game_exited.emit(code, duration)


class GameExitWorker(QObject):
    """游戏退出后的清理（关闭工具可能耗时数秒，别卡界面）。"""

    log_line = Signal(str)
    status = Signal(str)
    reports_ready = Signal(object)     # list[CloseReport]
    finished = Signal()

    def __init__(self, service: LaunchService, ask_handler) -> None:
        super().__init__()
        self.service = service
        self.ask_handler = ask_handler    # 在主线程弹窗的回调（阻塞等待结果）

    @Slot()
    def run(self) -> None:
        try:
            self.status.emit("正在清理辅助工具…")
            reports = self.service.stop_tools(ask=self.ask_handler)
            self.reports_ready.emit(reports)
        except Exception as exc:  # noqa: BLE001
            log.exception("清理失败")
            self.log_line.emit(f"清理过程出错：{exc}")
        finally:
            self.status.emit("就绪")
            self.finished.emit()


class LoginWorker(QObject):
    """微软登录（设备码 / 浏览器），可取消。"""

    status = Signal(str)
    device_code = Signal(str, str, int)     # user_code, verification_uri, expires_in
    success = Signal(str, str)              # name, uuid
    failed = Signal(str)
    finished = Signal()

    def __init__(self, cfg: AppConfig, accounts: AccountManager,
                 flow: str, client_id: str) -> None:
        super().__init__()
        self.cfg = cfg
        self.accounts = accounts
        self.flow = flow
        self.client_id = client_id
        self.cancel = CancelToken()

    @Slot()
    def run(self) -> None:
        try:
            account = self.accounts.login(
                flow=self.flow, client_id=self.client_id, cancel=self.cancel,
                on_status=self.status.emit,
                on_device_code=lambda info: self.device_code.emit(
                    info.user_code, info.verification_uri, info.expires_in),
            )
            self.success.emit(account.name, account.uuid)
        except AuthError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("登录失败")
            self.failed.emit(f"未预期的错误：{exc}")
        finally:
            self.finished.emit()


class TokenWorker(QObject):
    """后台刷新令牌（启动前预热）。"""

    status = Signal(str)
    success = Signal(str, str, int)     # name, uuid, expires_in
    failed = Signal(str)
    finished = Signal()

    def __init__(self, accounts: AccountManager) -> None:
        super().__init__()
        self.accounts = accounts

    @Slot()
    def run(self) -> None:
        if self.accounts.selected is None:
            self.finished.emit()
            return
        try:
            session = self.accounts.acquire_session(on_status=self.status.emit)
            self.success.emit(session.name, session.uuid,
                              max(0, int(session.expires_at - time.time())))
        except AuthError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"未预期的错误：{exc}")
        finally:
            self.finished.emit()
