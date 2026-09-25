"""启动子包：版本解析 / 命令行组装 / 游戏进程管理。"""
from __future__ import annotations

from .command import AIKAR_FLAGS, Command, build_command
from .launcher import GameProcess, LaunchService
from .version import MinecraftVersion, list_instances, load_version, resolve_libraries

__all__ = [
    "Command", "build_command", "AIKAR_FLAGS",
    "MinecraftVersion", "load_version", "list_instances", "resolve_libraries",
    "LaunchService", "GameProcess",
]
