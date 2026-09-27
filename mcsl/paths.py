"""路径约定。

设计决定：**仓库目录里不放任何运行期数据**。
  * 配置默认值   → 仓库 config/defaults.json（可提交）
  * 用户配置     → %LOCALAPPDATA%\\MCSR-Launcher\\config.json（不提交，也不在 OneDrive 里）
  * 账号凭据     → %LOCALAPPDATA%\\MCSR-Launcher\\accounts.dat（DPAPI 加密，不提交）
  * 日志 / 运行期 → %LOCALAPPDATA%\\MCSR-Launcher\\{logs,run}

这样即使项目放在 OneDrive 同步盘（桌面就是），token 也不会被同步到云端。
"""
from __future__ import annotations

import os
from pathlib import Path

from . import APP_NAME


def project_root() -> Path:
    """仓库根目录（mcsl 包的上一级）。"""
    return Path(__file__).resolve().parent.parent


def data_root() -> Path:
    """运行期数据根目录：%LOCALAPPDATA%\\MCSR-Launcher（非 Windows 退化为 ~/.local/share）。"""
    override = os.environ.get("MCSL_DATA_DIR")
    if override:
        return Path(override).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def icon_path() -> Path:
    """仓库里的图标（tools/make_icon.py 从 Minecraft 客户端 jar 提取生成）。"""
    return project_root() / "assets" / "mcsr-launcher.ico"


def config_file() -> Path:
    """用户配置文件（覆盖 defaults.json，不进 git）。"""
    return data_root() / "config.json"


def defaults_file() -> Path:
    """仓库内的默认配置模板。"""
    return project_root() / "config" / "defaults.json"


def accounts_file() -> Path:
    """账号凭据文件（DPAPI 加密）。"""
    return data_root() / "accounts.dat"


def logs_dir() -> Path:
    return data_root() / "logs"


def run_dir() -> Path:
    """启动脚本 / 进程记录等运行期临时文件。"""
    return data_root() / "run"


def ensure_dirs() -> None:
    for d in (data_root(), logs_dir(), run_dir(), data_root() / "cache"):
        d.mkdir(parents=True, exist_ok=True)
