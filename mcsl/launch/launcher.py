"""启动流程编排：认证 → 组装命令行 → 启动辅助工具 → 启动游戏 → 退出清理。

这个模块不依赖 GUI，CLI 和界面都通过回调接收状态与日志。
"""
from __future__ import annotations

import logging
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
                                 on_event=lambda level, msg: self.on_log(msg))
        self.game: GameProcess | None = None
        self.session: MCSession | None = None
        self._watch_thread: threading.Thread | None = None

    # ------------------------------------------------------------ 准备
    def prepare(self, *, force_refresh: bool = False,
                on_game_exit: ExitCb | None = None) -> tuple[MinecraftVersion, Command]:
        # 应用实例级覆盖（内存、JVM 参数等可以按实例区分）
        cfg = self.cfg.launch.effective()
        mc_dir = Path(cfg.minecraft_dir) if cfg.minecraft_dir else None
        if not mc_dir or not mc_dir.is_dir():
            raise FileNotFoundError(f".minecraft 目录不存在：{cfg.minecraft_dir!r}")
        if not cfg.instance:
            instances = list_instances(mc_dir)
            raise ValueError("没有选择实例" + (f"，可选：{', '.join(instances)}" if instances else "（versions 目录为空）"))

        self.on_status(f"正在加载实例 {cfg.instance}…")
        version = load_version(mc_dir, cfg.instance)
        log.info("实例 %s：mainClass=%s，库 %d 个，要求 Java %s",
                 version.id, version.main_class, len(version.libraries), version.java_major)

        self.on_status("正在获取 Minecraft 登录令牌…")
        self.session = self.accounts.acquire_session(force=force_refresh,
                                                     on_status=self.on_status)

        self.on_status("正在组装启动命令行…")
        command = build_command(version, self.session, cfg)
        log.info("命令行：%s", command.redacted())
        return version, command

    # ------------------------------------------------------------ 启动
    def start_tools(self) -> None:
        self.on_status("正在启动辅助工具…")
        self.tools.start_all()

    def start_game(self, command: Command, *, on_game_exit: ExitCb | None = None) -> GameProcess:
        run_dir().mkdir(parents=True, exist_ok=True)
        log_path = run_dir() / f"game-{datetime.now():%Y%m%d-%H%M%S}.log"

        creationflags = 0
        if not self.cfg.show_console and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = CREATE_NO_WINDOW

        self.on_status("正在启动 Minecraft…")
        try:
            proc = subprocess.Popen(
                command.argv(),
                cwd=str(command.cwd),
                env=command.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise RuntimeError(f"启动 Minecraft 失败：{exc}") from exc

        game = GameProcess(proc=proc, command=command, log_path=log_path)
        self.game = game
        log.info("Minecraft 已启动（PID %d），输出写入 %s", proc.pid, log_path)

        threading.Thread(target=self._pump_output, args=(game,), daemon=True,
                         name="mcsl-output").start()
        self._watch_thread = threading.Thread(target=self._watch_exit, args=(game, on_game_exit),
                                             daemon=True, name="mcsl-watch")
        self._watch_thread.start()
        return game

    def _pump_output(self, game: GameProcess) -> None:
        encoding = game.command.stdout_encoding or "utf-8"
        try:
            with open(game.log_path, "w", encoding="utf-8", errors="replace") as fh:
                assert game.proc.stdout is not None
                for raw in game.proc.stdout:
                    text = raw.decode(encoding, errors="replace").rstrip("\r\n")
                    fh.write(text + "\n")
                    if text:
                        self.on_log(text)
        except (OSError, ValueError) as exc:
            log.debug("读取游戏输出的循环结束：%s", exc)

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
        self.on_status("正在结束 Minecraft…")
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
            self.on_log("清理：" + report.summary())
            for line in report.diagnostics:
                self.on_log("    · " + line)
        self.on_log(scrub("清理完成：" + "；".join(r.summary() for r in reports)))
        return reports

    def shutdown(self) -> None:
        """程序退出时的兜底清理（不询问，直接关掉本程序启动的工具）。"""
        for name in ("ninjabrainbot", "jingle", "obs"):
            cfg = self.cfg.tools.get(name)
            if cfg is None or cfg.close_on_exit == "never":
                continue
            report = self.tools.stop(name)
            log.info("退出清理：%s", report.summary())


__all__ = ["LaunchService", "GameProcess", "CloseReport", "APP_NAME", "APP_VERSION"]
