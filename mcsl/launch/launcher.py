"""启动流程编排：认证 → 组装命令行 → 启动辅助工具 → 启动游戏 → 退出清理。

这个模块不依赖 GUI，CLI 和界面都通过回调接收状态与日志。
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .. import APP_NAME, APP_VERSION
from ..auth.manager import AccountManager, MCSession
from ..config import AppConfig
from ..logging_setup import scrub
from ..paths import run_dir
from ..tools.manager import CloseReport, ToolManager
from .command import Command, build_command
from .version import MinecraftVersion, list_instances, load_version

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000

LogCb = Callable[[str], None]
StatusCb = Callable[[str], None]
ExitCb = Callable[[int, float], None]


@dataclass
class GameProcess:
    proc: subprocess.Popen
    command: Command
    log_path: Path
    started_at: float = field(default_factory=time.time)
    log_handle: object | None = None      # 游戏 stdout 直接写这个文件句柄

    @property
    def pid(self) -> int:
        return self.proc.pid


class LaunchService:
    def __init__(self, config: AppConfig, accounts: AccountManager,
                 on_log: LogCb | None = None, on_status: StatusCb | None = None) -> None:
        self.cfg = config
        self.accounts = accounts
        self.on_log = on_log or (lambda msg: None)
        self.on_status = on_status or (lambda msg: None)
        self.tools = ToolManager(config.tools, java_path=config.launch.java_path,
                                 on_event=lambda level, msg: self._safe_log(msg))
        self.game: GameProcess | None = None
        self.session: MCSession | None = None
        self._watch_thread: threading.Thread | None = None

    # ------------------------------------------------------------ 准备
    def prepare(self, *, force_refresh: bool = False,
                on_game_exit: ExitCb | None = None) -> tuple[MinecraftVersion, Command]:
        # 应用实例级覆盖（内存、JVM 参数等可以按实例区分）
        cfg = self.cfg.launch
        mc_dir = Path(cfg.minecraft_dir) if cfg.minecraft_dir else None
        if not mc_dir or not mc_dir.is_dir():
            raise FileNotFoundError(f".minecraft 目录不存在：{cfg.minecraft_dir!r}")
        if not cfg.instance:
            instances = list_instances(mc_dir)
            raise ValueError("没有选择实例" + (f"，可选：{', '.join(instances)}" if instances else "（versions 目录为空）"))

        self._safe_status(f"正在加载实例 {cfg.instance}…")
        version = load_version(mc_dir, cfg.instance)
        log.info("实例 %s：mainClass=%s，库 %d 个，要求 Java %s",
                 version.id, version.main_class, len(version.libraries), version.java_major)

        self._safe_status("正在获取 Minecraft 登录令牌…")
        self.session = self.accounts.acquire_session(force=force_refresh,
                                                     on_status=self.on_status)

        self._safe_status("正在组装启动命令行…")
        command = build_command(version, self.session, cfg)
        log.info("命令行：%s", command.redacted())
        return version, command

    # ------------------------------------------------------------ 启动
    def start_tools(self) -> None:
        self._safe_status("正在启动辅助工具…")
        self.tools.start_all()

    def start_game(self, command: Command, *, on_game_exit: ExitCb | None = None) -> GameProcess:
        run_dir().mkdir(parents=True, exist_ok=True)
        log_path = run_dir() / f"game-{datetime.now():%Y%m%d-%H%M%S}.log"

        creationflags = 0
        if os.name == "nt":
            creationflags = CREATE_NEW_CONSOLE if self.cfg.show_console else CREATE_NO_WINDOW

        # ---------------------------------------------------------------
        # 游戏的 stdout/stderr 直接写文件，**不走管道**。
        #
        # 为什么（踩过的坑）：用 pipe + 读取线程时，读取端一旦出问题（线程异常
        # 退出、引用的对象被删…），Windows 管道默认只有 4KB 缓冲，写满之后
        # JVM 的日志写入会永久阻塞 —— 表现为：进程活着、内存吃满、
        # 永远停在 mod 加载阶段、没有窗口。日志文件本身是安全的，
        # 所以这里让游戏自己写文件，谁来看都不影响它运行。
        # ---------------------------------------------------------------
        handle = open(log_path, "wb")
        self._safe_status("正在启动 Minecraft…")
        try:
            proc = subprocess.Popen(
                command.argv(),
                cwd=str(command.cwd),
                env=command.env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        except OSError as exc:
            handle.close()
            raise RuntimeError(f"启动 Minecraft 失败：{exc}") from exc

        game = GameProcess(proc=proc, command=command, log_path=log_path, log_handle=handle)
        self.game = game
        log.info("Minecraft 已启动（PID %d），输出写入 %s", proc.pid, log_path)

        threading.Thread(target=self._tail_output, args=(game,), daemon=True,
                         name="mcsl-tail").start()
        self._watch_thread = threading.Thread(target=self._watch_exit, args=(game, on_game_exit),
                                             daemon=True, name="mcsl-watch")
        self._watch_thread.start()
        return game

    # ------------------------------------------------------------ 输出转发
    def _safe_log(self, text: str) -> None:
        """转发一行输出到界面。这里出错绝不能影响游戏，最多少显示一行。"""
        if not text.strip():
            return
        try:
            self.on_log(text)
        except Exception:  # noqa: BLE001
            log.debug("转发游戏输出失败（不影响游戏）", exc_info=True)

    def _safe_status(self, text: str) -> None:
        try:
            self.on_status(text)
        except Exception:  # noqa: BLE001
            log.debug("更新状态失败（不影响游戏）", exc_info=True)

    def _tail_output(self, game: GameProcess) -> None:
        """读游戏日志文件的新增内容并转发给界面。

        这只是个"看客"：它退出、报错、被杀都跟游戏无关。
        """
        encoding = game.command.stdout_encoding or "utf-8"
        position = 0
        pending = b""
        while True:
            running = game.proc.poll() is None
            try:
                with open(game.log_path, "rb") as fh:
                    fh.seek(position)
                    chunk = fh.read()
                    position = fh.tell()
            except OSError:
                chunk = b""
            if chunk:
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for raw in lines:
                    self._safe_log(raw.decode(encoding, "replace").rstrip("\r"))
            if not running:
                break
            time.sleep(0.4)
        if pending:
            self._safe_log(pending.decode(encoding, "replace"))
        try:
            if game.log_handle is not None:
                game.log_handle.close()
        except OSError:
            pass

    def _watch_exit(self, game: GameProcess, on_game_exit: ExitCb | None) -> None:
        code = game.proc.wait()
        duration = time.time() - game.started_at
        log.info("Minecraft 已退出（退出码 %s，运行 %.0f 秒），日志：%s",
                 code, duration, game.log_path)
        if self.game is game:
            self.game = None
        if on_game_exit:
            try:
                on_game_exit(code, duration)
            except Exception:  # noqa: BLE001 - 回调异常不该影响清理
                log.exception("游戏退出回调出错")

    # ------------------------------------------------------------ 停止
    @property
    def game_running(self) -> bool:
        return self.game is not None and self.game.proc.poll() is None

    def stop_game(self, timeout: float = 10.0) -> bool:
        game = self.game
        if game is None:
            return True
        if game.proc.poll() is not None:
            return True
        self._safe_status("正在结束 Minecraft…")
        try:
            game.proc.terminate()
        except OSError as exc:
            log.warning("结束 Minecraft 失败：%s", exc)
        try:
            game.proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            log.warning("Minecraft 未在 %.0f 秒内退出，改用 taskkill /T /F", timeout)
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(game.pid)],
                           capture_output=True, check=False)
            return game.proc.poll() is not None

    def stop_tools(self, *, ask: Callable[[str], bool] | None = None) -> list[CloseReport]:
        plan = {name: self.cfg.tools.get(name).close_on_exit if self.cfg.tools.get(name) else "always"
                for name in ("jingle", "ninjabrainbot", "obs")}
        reports = self.tools.stop_all(plan, ask=ask,
                                      allow_elevate=not self.cfg.elevate)
        for report in reports:
            self._safe_log("清理：" + report.summary())
            for line in report.diagnostics:
                self._safe_log("    · " + line)
        self._safe_log(scrub("清理完成：" + "；".join(r.summary() for r in reports)))
        return reports

    def shutdown(self) -> None:
        """程序退出时的兜底清理。

        只关 close_on_exit == always 的工具：标成 "ask" 的（默认是 OBS）留着不动，
        否则用户关个启动器窗口就可能把正在进行的录制截断。想关就手动关。
        """
        for name in ("ninjabrainbot", "jingle", "obs"):
            cfg = self.cfg.tools.get(name)
            if cfg is None:
                continue
            if cfg.close_on_exit != "always":
                log.info("退出清理：跳过 %s（策略=%s）", name, cfg.close_on_exit)
                continue
            report = self.tools.stop(name)
            log.info("退出清理：%s", report.summary())


__all__ = ["LaunchService", "GameProcess", "CloseReport", "APP_NAME", "APP_VERSION"]
