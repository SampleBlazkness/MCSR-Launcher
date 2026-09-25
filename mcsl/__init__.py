"""MCSR-Launcher —— 速通启动器。

分层：
    mcsl.auth    微软账号 / Xbox Live / Minecraft Services 认证
    mcsl.launch  版本解析 + 命令行生成 + 游戏进程
    mcsl.tools   辅助工具（Jingle / NinjabrainBot / OBS）的启动与关闭
    mcsl.ui      PySide6 图形界面

core 层（auth/launch/tools）不依赖 PySide6，可被 CLI 或测试单独调用。
"""
from __future__ import annotations

APP_NAME = "MCSR-Launcher"
APP_VERSION = "0.1.0"
APP_ID = "MCSR-Launcher"

__all__ = ["APP_NAME", "APP_VERSION", "APP_ID"]
