"""辅助工具（Jingle / NinjabrainBot / OBS）的启动与关闭。

与旧脚本的本质区别：
  1. 工具由本程序启动，PID 是我们自己知道的，不靠命令行字符串猜；
  2. 关闭走"先请求正常退出 → 等宽限期 → 强杀"，每一步都记录成功/失败原因；
  3. 目标进程提权而自己没有提权时，报错明确（而不是像旧脚本那样被
     -ErrorAction SilentlyContinue 吞掉，然后打印"已关闭"骗人）。
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import CLOSE_ALWAYS, CLOSE_ASK, CLOSE_NEVER, ToolConfig
from . import winproc

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000

TOOL_LABELS = {
    "obs": "OBS",
    "jingle": "Jingle",
    "ninjabrainbot": "NinjabrainBot",
}

# 关不掉的常见原因 → 人话提示
_CLOSE_HINTS = {
    5: "访问被拒绝：目标以管理员权限运行，而本程序没有提权（UIPI 限制）",
}


@dataclass
class ToolHandle:
    name: str
    pid: int
    path: str
    elevated: bool = False
    adopted: bool = False            # 复用了本来就开着的进程
    started_at: float = field(default_factory=time.time)

    @property
    def label(self) -> str:
        return TOOL_LABELS.get(self.name, self.name)


@dataclass
class CloseReport:
    name: str
    outcome: str = ""                # closed | killed | already_exited | failed | skipped
    detail: str = ""
    diagnostics: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return TOOL_LABELS.get(self.name, self.name)

    @property
    def ok(self) -> bool:
        return self.outcome in ("closed", "killed", "already_exited", "skipped")

    def summary(self) -> str:
        text = {
            "closed": "已正常退出",
            "killed": "已强制结束",
            "already_exited": "本来就没在运行",
            "failed": "关闭失败",
            "skipped": "按配置跳过",
        }.get(self.outcome, self.outcome)
        return f"{self.label}：{text}" + (f"（{self.detail}）" if self.detail else "")


class ToolManager:
    def __init__(self, configs: dict[str, ToolConfig], java_path: str = "",
                 on_event: Callable[[str, str], None] | None = None) -> None:
        self.configs = configs
        self.java_path = java_path
        self.on_event = on_event
        self.handles: dict[str, ToolHandle] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 查询
    def spec(self, name: str) -> ToolConfig:
        cfg = self.configs.get(name) or ToolConfig()
        cfg.normalized()
        return cfg

    def _emit(self, level: str, message: str) -> None:
        getattr(log, level, log.info)(message)
        if self.on_event:
            self.on_event(level, message)

    def _jar_launcher(self) -> str:
        """jar 工具用 javaw 启动（无控制台窗口）。"""
        java = self.java_path
        if not java:
            return "javaw"
        p = Path(java)
        if p.name.lower() == "java.exe":
            sibling = p.with_name("javaw.exe")
            if sibling.is_file():
                return str(sibling)
        return java

    def build_command(self, name: str) -> tuple[str, list[str]] | None:
        """返回 (可执行文件, 参数列表)。"""
        cfg = self.spec(name)
        if not cfg.enabled:
            return None
        path = Path(cfg.path) if cfg.path else None
        if path is None or not path.is_file():
            return None
        if path.suffix.lower() == ".jar":
            return self._jar_launcher(), ["-jar", str(path), *cfg.args]
        return str(path), list(cfg.args)

    def find_running(self, name: str) -> list[winproc.ProcessInfo]:
        cfg = self.spec(name)
        if not cfg.path:
            return []
        path = Path(cfg.path)
        if path.suffix.lower() == ".jar":
            return []      # jar 进程没有可靠的路径标识，不做复用检测
        return winproc.find_processes_by_path(path)

    # ------------------------------------------------------------ 启动
    def start(self, name: str) -> ToolHandle | None:
        cfg = self.spec(name)
        if not cfg.enabled:
            self._emit("info", f"{TOOL_LABELS.get(name, name)} 已禁用，跳过")
            return None
        command = self.build_command(name)
        if command is None:
            self._emit("warning", f"{TOOL_LABELS.get(name, name)} 路径无效或未配置：{cfg.path!r}")
            return None

        existing = self.find_running(name)
        if existing:
            info = existing[0]
            handle = ToolHandle(name=name, pid=info.pid, path=cfg.path,
                                elevated=bool(winproc.is_process_elevated(info.pid)),
                                adopted=True)
            with self._lock:
                self.handles[name] = handle
            self._emit("info", f"{handle.label} 已在运行（PID {info.pid}），复用它")
            return handle

        exe, args = command
        cwd = cfg.working_dir or str(Path(cfg.path).parent)
        if cfg.elevated:
            pid, err = winproc.shell_execute(exe, args, cwd=cwd, elevate=True)
            if pid == 0:
                self._emit("error", f"{TOOL_LABELS.get(name, name)} 提权启动失败：{err}")
                return None
        else:
            try:
                creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0
                proc = subprocess.Popen([exe, *args], cwd=cwd or None,
                                        creationflags=creationflags,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                pid = proc.pid
            except OSError as exc:
                self._emit("error", f"{TOOL_LABELS.get(name, name)} 启动失败：{exc}")
                return None

        handle = ToolHandle(name=name, pid=pid, path=cfg.path, elevated=cfg.elevated)
        with self._lock:
            self.handles[name] = handle
        self._emit("info", f"已启动 {handle.label}（PID {pid}"
                           + ("，管理员权限" if cfg.elevated else "") + "）")
        if cfg.start_delay_ms:
            time.sleep(cfg.start_delay_ms / 1000)
        return handle

    def start_all(self, names: list[str] | None = None) -> list[ToolHandle]:
        order = names or [n for n in ("obs", "jingle", "ninjabrainbot") if n in self.configs]
        started = []
        for name in order:
            handle = self.start(name)
            if handle:
                started.append(handle)
        return started

    # ------------------------------------------------------------ 关闭
    def stop(self, name: str, *, grace: float | None = None,
             allow_elevate: bool = False) -> CloseReport:
        handle = self.handles.get(name)
        cfg = self.spec(name)
        if cfg.close_on_exit == CLOSE_NEVER:
            return CloseReport(name=name, outcome="skipped", detail="配置为不关闭")
        if handle is None:
            return CloseReport(name=name, outcome="already_exited", detail="本程序没有启动它")
        return self.stop_pid(name, handle.pid, grace=grace, allow_elevate=allow_elevate,
                             elevated_hint=handle.elevated, adopted=handle.adopted)

    def stop_pid(self, name: str, pid: int, *, grace: float | None = None,
                 allow_elevate: bool = False, elevated_hint: bool | None = None,
                 adopted: bool = False) -> CloseReport:
        label = TOOL_LABELS.get(name, name)
        diag: list[str] = []
        if not winproc.is_running(pid):
            return CloseReport(name=name, outcome="already_exited", diagnostics=diag)

        elevated = winproc.is_process_elevated(pid)
        if elevated is None:
            elevated = bool(elevated_hint)
        diag.append(f"PID {pid}，提权={'是' if elevated else '否'}"
                    + ("（管理员启动器继承）" if adopted and elevated else ""))

        windows = winproc.candidate_windows(pid)
        diag.append(f"顶层窗口 {len(windows)} 个"
                    + (f"，最像主窗口的是 {windows[0].class_name}（{windows[0].title[:24] or '无标题'}）"
                       if windows else "（没有窗口，可能已最小化到托盘）"))

        timeout = grace if grace is not None else (8.0 if name == "obs" else 5.0)
        closed, _ = winproc.send_close_ranked(
            pid, per_window_timeout=1.5, total_timeout=timeout,
            on_note=diag.append)
        if closed:
            self._forget(name, pid)
            self._emit("info", f"{label} 已正常退出")
            return CloseReport(name=name, outcome="closed", detail=f"{timeout:.0f}s",
                               diagnostics=diag)

        diag.append(f"{timeout:.0f} 秒内没能让它正常退出，改为强制结束")

        ok, err = winproc.terminate(pid)
        if ok:
            diag.append("TerminateProcess 成功")
        else:
            diag.append(f"TerminateProcess 失败：{err}")
            if elevated and not winproc.is_self_elevated() and allow_elevate:
                self._emit("warning", f"{label} 是提权进程，改用提权 taskkill…")
                killed, kerr = winproc.kill_elevated([pid])
                diag.append(f"提权 taskkill {'成功' if killed else '失败：' + kerr}")
                ok = killed
            elif elevated and not winproc.is_self_elevated():
                diag.append("提示：本程序未提权，无法结束以管理员身份运行的进程")

        closed = winproc.wait_for_exit(pid, 3.0)
        diag.append("最终状态：已退出" if closed else "最终状态：仍在运行")
        if closed:
            self._forget(name, pid)
            self._emit("info", f"{label} 已强制结束")
            return CloseReport(name=name, outcome="killed", diagnostics=diag)
        self._emit("error", f"{label} 关闭失败（" + "；".join(diag) + "）")
        return CloseReport(name=name, outcome="failed",
                           detail=diag[-2] if len(diag) >= 2 else "", diagnostics=diag)

    def stop_all(self, plan: dict[str, str], *,
                 ask: Callable[[str], bool] | None = None,
                 allow_elevate: bool = False) -> list[CloseReport]:
        """按计划关闭。plan: {工具名: close_on_exit}；ask 回调用于询问用户。"""
        reports: list[CloseReport] = []
        for name in ("ninjabrainbot", "jingle", "obs"):
            policy = plan.get(name, CLOSE_ALWAYS)
            if policy == CLOSE_NEVER:
                reports.append(CloseReport(name=name, outcome="skipped", detail="配置为不关闭"))
                continue
            if policy == CLOSE_ASK:
                if ask is not None and not ask(name):
                    reports.append(CloseReport(name=name, outcome="skipped", detail="用户选择保留"))
                    continue
                if ask is None:
                    reports.append(CloseReport(name=name, outcome="skipped", detail="没有可询问的界面"))
                    continue
            reports.append(self.stop(name, allow_elevate=allow_elevate))
        return reports

    def _forget(self, name: str, pid: int) -> None:
        with self._lock:
            handle = self.handles.get(name)
            if handle and handle.pid == pid:
                self.handles.pop(name, None)

    def live_handles(self) -> list[ToolHandle]:
        return [h for h in self.handles.values() if winproc.is_running(h.pid)]
