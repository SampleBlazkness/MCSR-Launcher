"""命令行入口（不依赖 PySide6，方便调试与自动化）。

    python -m mcsl.cli instances                  列出 .minecraft 下的实例
    python -m mcsl.cli dump-command [--instance 1.16.1] [--json]
                                                  只组装命令行并打印（不启动游戏）
    python -m mcsl.cli auth-status                当前账号状态
    python -m mcsl.cli login [--flow device_code|auth_code]
    python -m mcsl.cli auth-token                 打印当前 Minecraft 令牌的抬头信息（不打印令牌本体）
    python -m mcsl.cli launch [--no-tools]        真正启动游戏（Ctrl+C 结束）
    python -m mcsl.cli tools-status               检查工具路径与进程状态
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import APP_NAME, APP_VERSION
from .auth.errors import AuthError
from .auth.manager import AccountManager
from .auth.store import dpapi_available
from .config import load_config
from .launch.command import build_command
from .launch.version import list_instances, load_version
from .logging_setup import setup as setup_logging
from .paths import accounts_file, data_root, logs_dir


class FakeSession:
    """dump-command 用：不联网也能验证命令行组装结果。"""

    def __init__(self, client_id: str = "00000000402b5328") -> None:
        self.name = "YourName"
        self.uuid = "00000000000000000000000000000000"
        self.access_token = "DUMMY_TOKEN_FOR_DRY_RUN"
        self.token_type = "Bearer"
        self.client_id = client_id
        self.xuid = "2535405000000000"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcsl", description=f"{APP_NAME} {APP_VERSION} 命令行")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("instances", help="列出 .minecraft/versions 下的实例")

    p_dump = sub.add_parser("dump-command", help="组装命令行并打印（不启动）")
    p_dump.add_argument("--instance", default=None)
    p_dump.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_dump.add_argument("--show-token", action="store_true", help="用假令牌并显示完整参数")

    sub.add_parser("auth-status", help="显示账号与令牌状态")

    p_login = sub.add_parser("login", help="登录微软账号")
    p_login.add_argument("--flow", default=None, choices=["device_code", "auth_code"])
    p_login.add_argument("--client-id", default=None)

    sub.add_parser("auth-token", help="刷新并显示令牌抬头（不打印令牌本体）")

    p_launch = sub.add_parser("launch", help="启动游戏")
    p_launch.add_argument("--no-tools", action="store_true", help="不启动辅助工具")
    p_launch.add_argument("--instance", default=None)

    sub.add_parser("tools-status", help="检查辅助工具配置与运行状态")

    args = parser.parse_args(argv)
    setup_logging(level=10 if args.verbose else 20)
    cfg = load_config()

    try:
        if args.command == "instances":
            return cmd_instances(cfg)
        if args.command == "dump-command":
            return cmd_dump(cfg, args)
        if args.command == "auth-status":
            return cmd_auth_status(cfg)
        if args.command == "login":
            return cmd_login(cfg, args)
        if args.command == "auth-token":
            return cmd_auth_token(cfg)
        if args.command == "launch":
            return cmd_launch(cfg, args)
        if args.command == "tools-status":
            return cmd_tools_status(cfg)
    except AuthError as exc:
        print(f"[认证错误] {exc}")
        return 2
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"[错误] {exc}")
        return 1
    return 0


def cmd_instances(cfg) -> int:
    mc_dir = Path(cfg.launch.minecraft_dir)
    if not mc_dir.is_dir():
        print(f"[错误] .minecraft 目录不存在：{mc_dir}")
        return 1
    instances = list_instances(mc_dir)
    print(f"{mc_dir} 下的实例（共 {len(instances)} 个，* = 当前选择）：")
    for name in instances:
        print(f"  {'*' if name == cfg.launch.instance else ' '} {name}")
    return 0


def cmd_dump(cfg, args) -> int:
    launch_cfg = cfg.launch
    mc_dir = Path(launch_cfg.minecraft_dir)
    version = load_version(mc_dir, launch_cfg.instance)
    session = FakeSession(cfg.auth.client_id)
    command = build_command(version, session, launch_cfg)
    if args.json:
        print(json.dumps({
            "java": str(command.java),
            "args": command.args,
            "cwd": str(command.cwd),
            "natives": str(command.natives_dir),
            "classpath_count": len(command.classpath),
            "missing": [str(p) for p in command.missing],
            "stdout_encoding": command.stdout_encoding,
        }, ensure_ascii=False, indent=2))
        return 0
    print(f"java      : {command.java}")
    print(f"cwd       : {command.cwd}")
    print(f"natives   : {command.natives_dir}")
    print(f"classpath : {len(command.classpath)} 项，缺失 {len(command.missing)} 项")
    for path in command.missing[:10]:
        print(f"            [缺失] {path}")
    print("参数：")
    for arg in command.args:
        print("   " + arg)
    print("\n（access token 为假令牌，仅用于验证组装结果）")
    return 0


def cmd_auth_status(cfg) -> int:
    mgr = AccountManager(cfg.auth)
    data = mgr.data
    print(f"账号文件      : {accounts_file()}（DPAPI 加密：{'可用' if dpapi_available() else '不可用'}）")
    print(f"配置文件      : {data_root() / 'config.json'}")
    print(f"日志目录      : {logs_dir()}")
    print(f"client_id     : {cfg.auth.client_id}（端点预设 {cfg.auth.endpoint_preset}，流程 {cfg.auth.flow}）")
    if not data.accounts:
        print("已保存账号    : 无（需要先登录）")
        return 0
    print(f"已保存账号    : {len(data.accounts)} 个")
    for acc in data.accounts:
        cached = acc.session or {}
        remaining = 0
        if cached.get("expires_at"):
            import time
            remaining = max(0, int(cached["expires_at"] - time.time()))
        mark = " ← 当前" if acc.uuid == data.selected else ""
        print(f"  - {acc.name}  uuid={acc.uuid}  缓存令牌剩余 {remaining // 3600}h{mark}")
    return 0


def cmd_login(cfg, args) -> int:
    mgr = AccountManager(cfg.auth)
    account = mgr.login(
        flow=args.flow, client_id=args.client_id,
        on_status=lambda msg: print("  " + msg),
        on_device_code=lambda info: print(
            f"\n>>> 请在浏览器打开 {info.verification_uri} 并输入代码： {info.user_code}\n"
            f">>> 代码有效期 {info.expires_in // 60} 分钟\n"),
    )
    print(f"登录成功：{account.name}（uuid={account.uuid}）")
    return 0


def cmd_auth_token(cfg) -> int:
    mgr = AccountManager(cfg.auth)
    session = mgr.acquire_session(on_status=lambda msg: print("  " + msg))
    print(f"玩家       : {session.name}")
    print(f"uuid       : {session.uuid}")
    print(f"xuid       : {session.xuid or '(未知)'}")
    print(f"令牌类型   : {session.token_type}")
    print(f"令牌长度   : {len(session.access_token)} 字符（刻意不打印内容）")
    print(f"剩余有效期 : {max(0, int(session.expires_at - __import__('time').time())) // 60} 分钟")
    return 0


def cmd_launch(cfg, args) -> int:
    import time

    from .launch.launcher import LaunchService

    launch_cfg = cfg.launch
    cfg.launch = launch_cfg
    mgr = AccountManager(cfg.auth)
    service = LaunchService(cfg, mgr, on_log=lambda m: print("  " + m),
                            on_status=lambda m: print(f"[*] {m}"))
    version, command = service.prepare()
    print(f"[*] 实例 {version.id}，Java {version.java_major}")
    if not args.no_tools:
        service.start_tools()
    done = {"flag": False}

    def on_exit(code: int, duration: float) -> None:
        print(f"[*] Minecraft 退出，退出码 {code}，运行 {duration:.0f} 秒")
        service.stop_tools()
        done["flag"] = True

    service.start_game(command, on_game_exit=on_exit)
    print("[*] 游戏运行中，Ctrl+C 结束（会顺手清理辅助工具）")
    try:
        while not done["flag"]:
            time.sleep(0.5)
    except KeyboardInterrupt:
        service.stop_game()
        service.stop_tools()
    return 0


def cmd_tools_status(cfg) -> int:
    from .tools import winproc
    from .tools.manager import TOOL_LABELS

    print(f"本程序是否提权：{'是' if winproc.is_self_elevated() else '否'}")
    for name, tool in cfg.tools.items():
        exists = Path(tool.path).is_file() if tool.path else False
        print(f"\n{TOOL_LABELS.get(name, name)}：")
        print(f"  路径      : {tool.path or '(未配置)'} {'✓' if exists else '✗'}")
        print(f"  启用/提权 : {'启用' if tool.enabled else '禁用'} / {'管理员' if tool.elevated else '普通'}")
        print(f"  退出策略  : {tool.close_on_exit}")
        if exists and Path(tool.path).suffix.lower() != ".jar":
            running = winproc.find_processes_by_path(tool.path)
            for info in running:
                elevated = winproc.is_process_elevated(info.pid)
                print(f"  运行中    : PID {info.pid}（提权={'是' if elevated else '否' if elevated is not None else '未知'}，"
                      f"顶层窗口 {len(winproc.windows_of_pid(info.pid))} 个）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
