"""探针：启动游戏 → 游戏退出 → 再次启动，全程不关启动器。

盯死的 bug（用户报的）：点一次启动游戏之后，游戏退出了按钮也不恢复，
必须关掉启动器重开才能再启动。根因是游戏退出回调打到了**已被销毁**的
LaunchWorker 上：

    RuntimeError: Signal source has been deleted

（run() 在游戏一启动就 return → 线程结束 → thread.finished → deleteLater
把 worker 删掉；几十秒后游戏退出时 _watch_exit 才调 on_game_exit。）

这里用假游戏进程（一个睡几秒的 python）复现整条链：网络、Java、Minecraft
都不需要。判据有三条：界面收到"游戏已退出"、启动按钮自动恢复可点、
点第二次能真的再启动一次，而且日志里不能再出现 Signal source has been deleted。
"""
from __future__ import annotations

import faulthandler
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

_fault_file = open(Path(tempfile.gettempdir()) / "mcsl-launch-reset-faults.txt", "a",
                   encoding="utf-8", errors="replace")
faulthandler.enable(file=_fault_file, all_threads=True)

DATA_DIR = Path(tempfile.gettempdir()) / "mcsl-launch-reset"
os.environ["MCSL_DATA_DIR"] = str(DATA_DIR)
os.environ["MCSL_NO_ELEVATE"] = "1"        # 测试用：跳过提权自重启
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["MCSL_SKIP_JAVA_CHECK"] = "1"   # 测试用：跳过启动前的 Java 核对弹窗
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer                                     # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox               # noqa: E402

import mcsl.ui.workers as workers_mod                                 # noqa: E402
from mcsl.auth.manager import AccountManager                          # noqa: E402
from mcsl.auth.store import Account                                   # noqa: E402
from mcsl.config import load_config                                   # noqa: E402
from mcsl.launch.launcher import GameProcess                          # noqa: E402
from mcsl.logging_setup import setup                                  # noqa: E402
from mcsl.ui.main_window import MainWindow                            # noqa: E402

LOG_FILE = Path(tempfile.gettempdir()) / "mcsl-launch-reset-probe.txt"
GAME_SECONDS = 3          # 假游戏活多久
log = logging.getLogger("probe.fake")


def note(text: str = "") -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")


# ============================================================ 假服务（复刻真实时序）
class _Stub:
    """冒充 MinecraftVersion / Command。"""

    def __init__(self) -> None:
        self.id = "1.16.1-fake"
        self.java_major = 8
        self.missing: list = []
        self.classpath: list = ["a.jar"]


class _StubTools:
    def is_tool_running(self, name: str) -> bool:
        return False


class FakeService:
    """和 LaunchService 一样的对外形状，但游戏是个睡几秒的 python 进程。

    关键点：退出回调必须由**另一个线程**在游戏结束后调用 —— 真实实现就是这样
    （launcher.py 的 _watch_exit 跑在普通 threading.Thread 里），这正是当初
    worker 被销毁后回调才到达的原因。
    """

    instances: list["FakeService"] = []

    def __init__(self, cfg, accounts, *, on_log=None, on_status=None) -> None:
        self.cfg = cfg
        self.on_log = on_log
        self.on_status = on_status
        self.game: GameProcess | None = None
        self.game_starts = 0
        self.tools = _StubTools()
        FakeService.instances.append(self)

    # ---- 启动流程
    def prepare(self, *, force_refresh: bool = False):
        return _Stub(), _Stub()

    def start_tools(self) -> None:
        pass

    def start_game(self, command, *, on_game_exit=None) -> GameProcess:
        flags = 0x08000000 if os.name == "nt" else 0        # CREATE_NO_WINDOW
        proc = subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep({GAME_SECONDS})"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
        )
        log_path = DATA_DIR / "run" / f"fake-game-{self.game_starts}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("假游戏启动\n", encoding="utf-8")
        game = GameProcess(proc=proc, command=command, log_path=log_path)
        self.game = game
        self.game_starts += 1

        def watch() -> None:
            code = proc.wait()
            duration = time.time() - game.started_at
            if self.game is game:
                self.game = None
            if on_game_exit:
                # 和真实实现一样（launcher._watch_exit）：回调异常要记进日志，
                # 否则探针的"日志里没有 Signal source has been deleted"这条
                # 断言会因为异常只打到 stderr 而假通过。
                try:
                    on_game_exit(code, duration)
                except Exception:  # noqa: BLE001
                    log.exception("游戏退出回调出错")

        threading.Thread(target=watch, daemon=True, name="fake-watch").start()
        return game

    # ---- 状态
    @property
    def game_running(self) -> bool:
        return self.game is not None and self.game.proc.poll() is None

    def stop_game(self, timeout: float = 10.0) -> bool:
        if self.game is None:
            return True
        self.game.proc.terminate()
        return True

    def stop_tools(self, ask=None) -> list:
        return []


# ============================================================ 跑流程
def main() -> int:
    LOG_FILE.write_text("", encoding="utf-8")
    note("=== 启动→退出→再启动 探针 ===")
    setup(level="INFO")
    workers_mod.LaunchService = FakeService          # 只换掉 worker 里用到的那一个名字
    # 临时数据目录里没有真账号：_on_launch 只检查"有没有选中账号"，
    # 给它一个假的就行（不会走到真取 token 的代码，服务已被换掉）。
    AccountManager.selected = property(lambda self: Account(uuid="0" * 32, name="Probe"))

    app = QApplication(sys.argv)
    # 探针里不弹任何框：一律当成"确认"
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)

    window = MainWindow(load_config())
    window.resize(1000, 880)
    window.show()

    results: list[tuple[bool, str]] = []

    def check(ok: bool, text: str, detail: str = "") -> None:
        results.append((bool(ok), text))
        note(f"[{'ok' if ok else 'FAIL'}]  {text}" + (f"    {detail}" if detail else ""))

    state = {"step": 0, "deadline": time.time() + 40, "clicked_at": 0.0}

    first = FakeService.instances[0] if FakeService.instances else None

    def tick() -> None:
        try:
            _tick()
        except Exception:  # noqa: BLE001
            note("tick 崩溃：\n" + traceback.format_exc())
            check(False, "探针流程没崩", traceback.format_exc().splitlines()[-1])
            finish()

    def _tick() -> None:
        nonlocal first
        svc = FakeService.instances[0] if FakeService.instances else None
        if state["step"] == 0:
            # 点第一次启动
            check(window.launch_button.isEnabled(), "启动前按钮可点")
            window._on_launch()
            state["clicked_at"] = time.time()
            state["step"] = 1
            return
        if state["step"] == 1:
            # 游戏应该在跑：按钮变灰、停止按钮亮起
            if time.time() - state["clicked_at"] < 1.0:
                return
            if svc is None or svc.game_starts == 0:
                if time.time() > state["deadline"]:
                    check(False, "启动流程开始了", "等服务创建超时")
                    finish()
                return
            check(not window.launch_button.isEnabled(),
                  "游戏运行中：启动按钮不可点（避免开两个游戏）")
            check(window.stop_button.isEnabled(), "游戏运行中：停止按钮可点")
            note(f"     假游戏已启动（第 {svc.game_starts} 次）")
            state["step"] = 2
            return
        if state["step"] == 2:
            # 等游戏退出 + 清理跑完
            if svc is not None and svc.game_starts >= 1 and svc.game is None \
                    and not window._busy and not window._cleanup_active:
                text = window.log_view.text()
                check("Minecraft 已退出" in text,
                      "界面收到了「游戏已退出」（以前这条永远不出现）",
                      text[-160:].replace("\n", " / "))
                check(window.launch_button.isEnabled(), "游戏退出后启动按钮自动恢复可点")
                check(not window.stop_button.isEnabled(), "停止按钮同时变灰")
                check(not window._busy, "忙碌状态已解除")
                check(not window._cleanup_active, "退出清理标记已复位")
                state["step"] = 3
                return
            if time.time() > state["deadline"]:
                check(False, "游戏退出后状态自动复位",
                      f"game_running={getattr(svc, 'game_running', None)} "
                      f"busy={window._busy} cleanup={window._cleanup_active}")
                finish()
            return
        if state["step"] == 3:
            # 不关启动器，直接点第二次
            note("     第二次点击「启动游戏」…")
            window._on_launch()
            state["step"] = 4
            return
        if state["step"] == 4:
            if len(FakeService.instances) >= 2 and FakeService.instances[-1].game_starts >= 1:
                check(True, "不关启动器也能再次启动游戏",
                      f"第 {len(FakeService.instances)} 个启动会话已开始")
                check(FakeService.instances[0].game_starts == 1,
                      "第二次启动只开了新的一局（没有重复启动同一局）")
                check(not window.launch_button.isEnabled(),
                      "第二局运行期间按钮同样变灰")
                state["step"] = 5
                return
            if time.time() > state["deadline"]:
                check(False, "不关启动器也能再次启动游戏",
                      f"服务实例数={len(FakeService.instances)}，"
                      f"各自 start_game 次数={[s.game_starts for s in FakeService.instances]}")
                finish()
            return
        if state["step"] == 5:
            # 第二局也要能正常复位
            last = FakeService.instances[-1] if FakeService.instances else None
            if last is not None and last.game is None and not window._busy:
                check(window.launch_button.isEnabled(), "第二局退出后同样自动复位")
                finish()
                return
            if time.time() > state["deadline"]:
                check(False, "第二局退出后同样自动复位", "超时")
                finish()
            return

    def finish() -> None:
        timer.stop()
        # 数据目录里的日志文件：这条 Traceback 是当初 bug 的指纹，必须消失
        log_text = ""
        for path in (DATA_DIR / "logs").glob("*.log"):
            log_text += path.read_text(encoding="utf-8", errors="replace")
        check("Signal source has been deleted" not in log_text,
              "日志里没有 Signal source has been deleted",
              "".join(l for l in log_text.splitlines() if "Signal source" in l))
        check("游戏退出回调出错" not in log_text, "日志里没有「游戏退出回调出错」")
        failed = [t for ok, t in results if not ok]
        note()
        note(f"结论：{'全部通过' if not failed else '失败 ' + str(failed)}"
             f"（{len(results) - len(failed)}/{len(results)}）")
        try:
            window._closing = True
            window.close()
        except Exception:  # noqa: BLE001
            note("关窗时出错：" + traceback.format_exc())
        app.quit()
        sys.exit(1 if failed else 0)

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(200)
    QTimer.singleShot(60000, lambda: (check(False, "整体流程没超时"), finish()))
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        note("探针崩溃：\n" + traceback.format_exc())
        sys.exit(2)
