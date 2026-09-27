"""把耗时的认证 / 启动 / 清理放到工作线程，通过 signal 回主线程更新界面。"""
from __future__ import annotations

import logging
import threading
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

# 同理，**工作在子线程里的 QObject（worker）也必须有人持有 Python 引用**：
# 纯 Python 创建的 QObject 归 Python 所有，一旦最后一个 Python 引用消失，
# PySide6 会立刻删除 C++ 对象 —— 而它可能还停在工作线程里等着
# thread.finished → deleteLater，于是"删两次"变成释放后使用（访问违例 0xC0000005），
# 且没有任何 Python 回溯，只有偶发崩溃。登记到 Qt 真正销毁（destroyed）为止。
_ACTIVE_OBJECTS: list[QObject] = []


def track_thread(thread: QThread) -> QThread:
    _ACTIVE_THREADS.add(thread)
    thread.finished.connect(lambda: _ACTIVE_THREADS.discard(thread))
    return thread


def track_object(obj):
    """把 worker 之类的 QObject 钉住，直到 Qt 自己销毁它。"""
    _ACTIVE_OBJECTS.append(obj)
    obj.destroyed.connect(lambda *_: _drop_object(obj))
    return obj


def _drop_object(obj) -> None:
    for index, existing in enumerate(_ACTIVE_OBJECTS):
        if existing is obj:
            del _ACTIVE_OBJECTS[index]
            return


class UiBridge(QObject):
    """主窗口持有的长生命周期信号桥。

    为什么必须有它：LaunchService 的回调（游戏输出转发、状态更新）会被
    **后台线程**调用，而 worker 对象在 run() 返回后就被 Qt 销毁了。把回调直接
    绑在 worker 的 signal 上，会得到 "RuntimeError: Signal source has been
    deleted" —— 这个异常会打死转发线程，进而把游戏日志管道写满、把游戏卡死。
    桥接对象由主窗口持有、活到程序结束，且它的绑定方法属于主线程 QObject，
    所以从任何线程 emit 都会自动排到主线程执行。
    """

    log_line = Signal(str)
    status = Signal(str)


class LaunchWorker(QObject):
    """准备 + 启动游戏，然后在本线程里守到游戏退出。

    **生命周期**：这个 worker 必须活满整场游戏。它的 run() 一返回，
    界面就会 `thread.finished → worker.deleteLater` 把它删掉；而游戏的退出回调
    （`_on_game_exit`）还攥着它。以前 run() 启动完就 return，结果游戏退出时
    emit 抛 `RuntimeError: Signal source has been deleted` —— 界面收不到
    "游戏已退出"，启动按钮一直灰着、也不弹关闭辅助工具的询问。

    输出转发与状态更新走 UiBridge（长生命周期），不用本对象的 signal。
    """

    status = Signal(str)
    log_line = Signal(str)
    failed = Signal(str)
    service_ready = Signal(object)
    game_exited = Signal(int, float)
    finished = Signal()

    def __init__(self, cfg: AppConfig, accounts: AccountManager, *,
                 on_log=None, on_status=None) -> None:
        super().__init__()
        self.cfg = cfg
        self.accounts = accounts
        self.on_log = on_log or self.log_line.emit
        self.on_status = on_status or self.status.emit
        self.service: LaunchService | None = None
        self.force_refresh = False
        # 退出回调跑完了没：run() 靠它保证 emit 一定发生在自己（和 C++ 对象）
        # 还在的时候，而不是被销毁之后。
        self._game_exit_seen = threading.Event()

    @Slot()
    def run(self) -> None:
        try:
            service = LaunchService(self.cfg, self.accounts,
                                    on_log=self.on_log,
                                    on_status=self.on_status)
            self.service = service
            version, command = service.prepare(force_refresh=self.force_refresh)
            self.on_log(f"实例 {version.id}｜Java 要求 {version.java_major}｜"
                        f"classpath {len(command.classpath)} 项")
            if command.missing:
                self.on_log(f"⚠ 有 {len(command.missing)} 个库文件缺失，"
                            "游戏可能启动失败（详情见日志文件）")
            self.on_status("正在启动辅助工具…")
            service.start_tools()
            game = service.start_game(command, on_game_exit=self._on_game_exit)
            self.on_status("Minecraft 运行中")
            self.service_ready.emit(service)
            # 守到游戏退出为止 —— 见类文档：本对象必须活满整场游戏，
            # 否则退出回调会打到已销毁的 C++ 对象上（Signal source has been deleted）。
            game.proc.wait()
            if not self._game_exit_seen.wait(timeout=15):
                log.warning("等待游戏退出回调超时（界面可能没收到退出事件）")
        except (AuthError, FileNotFoundError, ValueError, RuntimeError, OSError) as exc:
            log.exception("启动失败")
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜底，保证界面不会静默卡住
            log.exception("启动时出现未预期错误")
            self.failed.emit(f"未预期的错误：{exc}")
        finally:
            self.finished.emit()

    def _on_game_exit(self, code: int, duration: float) -> None:
        """在**看门线程**里被调用，不能在此时碰任何界面对象。

        emit 本身是线程安全的（跨线程会排到主线程执行），但对象必须还活着，
        所以先发信号再置事件，run() 见到事件才会返回。
        """
        self.game_exited.emit(code, duration)
        self._game_exit_seen.set()


class GameExitWorker(QObject):
    """游戏退出后的清理（关闭工具可能耗时数秒，别卡界面）。"""

    log_line = Signal(str)
    status = Signal(str)
    reports_ready = Signal(object)     # list[CloseReport]
    finished = Signal()

    def __init__(self, service: LaunchService, ask_handler, *,
                 on_log=None, on_status=None) -> None:
        super().__init__()
        self.service = service
        self.ask_handler = ask_handler    # 在主线程弹窗的回调（阻塞等待结果）
        self.on_log = on_log or self.log_line.emit
        self.on_status = on_status or self.status.emit

    @Slot()
    def run(self) -> None:
        try:
            self.on_status("正在清理辅助工具…")
            reports = self.service.stop_tools(ask=self.ask_handler)
            self.reports_ready.emit(reports)
        except Exception as exc:  # noqa: BLE001
            log.exception("清理失败")
            self.on_log(f"清理过程出错：{exc}")
        finally:
            self.on_status("就绪")
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
