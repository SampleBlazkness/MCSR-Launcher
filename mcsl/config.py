"""配置：仓库里的 defaults.json 提供模板，%LOCALAPPDATA% 下的 config.json 是用户实际配置。

读取顺序：defaults.json → 深度合并 config.json。写回只写 config.json，
保证仓库里永远不含用户路径、账号或其他个人信息。
"""
from __future__ import annotations

import copy
import json
import logging
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from .paths import config_file, defaults_file

log = logging.getLogger(__name__)

# 辅助工具退出时的处理策略
CLOSE_ALWAYS = "always"
CLOSE_ASK = "ask"
CLOSE_NEVER = "never"
VALID_CLOSE = (CLOSE_ALWAYS, CLOSE_ASK, CLOSE_NEVER)


@dataclass
class ToolConfig:
    """一个辅助工具（Jingle / NinjabrainBot / OBS）的配置。"""
    enabled: bool = True
    path: str = ""                 # 可执行文件或 jar 的绝对路径
    args: list[str] = field(default_factory=list)   # 附加命令行参数
    elevated: bool = False         # 是否以管理员身份启动（Jingle 需要）
    close_on_exit: str = CLOSE_ALWAYS
    start_delay_ms: int = 0        # 启动后等待多久再启动下一个
    working_dir: str = ""          # 留空 = 用 exe/jar 所在目录

    def normalized(self) -> "ToolConfig":
        if self.close_on_exit not in VALID_CLOSE:
            log.warning("工具 close_on_exit 值非法：%r，回退为 %s", self.close_on_exit, CLOSE_ALWAYS)
            self.close_on_exit = CLOSE_ALWAYS
        return self


@dataclass
class AuthConfig:
    """微软登录配置。

    默认 client_id 是官方启动器的公开 id，配合 Live Connect 端点做设备码登录
    （实测可拿到 device code）。换成你自己注册的 Azure 应用时，把
    endpoint_preset 改成 "v2"，并填写自己的 client_id。
    """
    client_id: str = "00000000402b5328"
    endpoint_preset: str = "live"   # live | v2
    flow: str = "device_code"       # device_code | auth_code
    auto_login: bool = True         # 启动时若已有账号则自动续期


@dataclass
class InstanceOverride:
    """针对单个实例的覆盖项（config.json 里的 launch.instances["<实例名>"]）。

    None = 沿用全局设置。以后要在界面上做"每个实例一套 JVM 参数"时，
    数据层已经就位，不用改配置格式。
    """
    memory_mb: int | None = None
    jvm_preset: str | None = None
    jvm_args_extra: str | None = None
    game_args_extra: str | None = None
    java_path: str | None = None
    width: int | None = None
    height: int | None = None
    version_type: str | None = None


@dataclass
class LaunchConfig:
    """游戏启动配置。"""
    minecraft_dir: str = ""              # .minecraft 目录
    instance: str = ""                   # versions/ 下的实例名，如 1.16.1
    java_path: str = ""                  # java.exe 路径
    memory_mb: int = 4096
    jvm_preset: str = "custom"           # aikar | vanilla | custom
    jvm_args_extra: str = ""             # 追加的 JVM 参数（一行，空格分隔）
    game_args_extra: str = ""            # 追加的游戏参数
    version_type: str = "MCSR-Launcher"  # --versionType
    width: int = 0                       # 0 = 不传，交给游戏自身设置
    height: int = 0
    env_overrides: dict[str, str] = field(default_factory=dict)  # 空 = 自动设 APPDATA
    instances: dict[str, InstanceOverride] = field(default_factory=dict)

    def effective(self, instance: str | None = None) -> "LaunchConfig":
        """返回应用了实例覆盖项之后的副本。"""
        instance = instance or self.instance
        override = self.instances.get(instance)
        if override is None:
            return replace(self, instance=instance, instances={})
        merged = replace(self, instance=instance, instances={})
        for field_name in InstanceOverride.__dataclass_fields__:
            value = getattr(override, field_name)
            if value is not None:
                setattr(merged, field_name, value)
        return merged


@dataclass
class AppConfig:
    elevate: bool = True                 # 启动器自身是否提权运行
    show_console: bool = False           # 是否显示游戏控制台窗口
    close_game_on_launcher_exit: bool = False
    auth: AuthConfig = field(default_factory=AuthConfig)
    launch: LaunchConfig = field(default_factory=LaunchConfig)
    tools: dict[str, ToolConfig] = field(default_factory=lambda: {
        "obs": ToolConfig(path="", close_on_exit=CLOSE_ASK),
        "jingle": ToolConfig(path="", elevated=True, close_on_exit=CLOSE_ALWAYS, start_delay_ms=500),
        "ninjabrainbot": ToolConfig(path="", close_on_exit=CLOSE_ALWAYS, start_delay_ms=500),
    })

    # ---------------- 序列化 ----------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        auth = AuthConfig(**_pick(data.get("auth") or {}, AuthConfig))
        launch_raw = dict(data.get("launch") or {})
        instances_raw = launch_raw.pop("instances", {}) or {}
        launch = LaunchConfig(**_pick(launch_raw, LaunchConfig))
        launch.instances = {
            name: InstanceOverride(**_pick(cfg or {}, InstanceOverride))
            for name, cfg in instances_raw.items()
        }
        tools = {name: ToolConfig(**_pick(cfg or {}, ToolConfig)).normalized()
                 for name, cfg in (data.get("tools") or {}).items()}
        for name, default in (("obs", CLOSE_ASK), ("jingle", CLOSE_ALWAYS), ("ninjabrainbot", CLOSE_ALWAYS)):
            if name not in tools:
                tools[name] = cls().tools[name]
        return cls(
            elevate=bool(data.get("elevate", True)),
            show_console=bool(data.get("show_console", False)),
            close_game_on_launcher_exit=bool(data.get("close_game_on_launcher_exit", False)),
            auth=auth, launch=launch, tools=tools,
        )


def _pick(source: dict[str, Any], dc: type) -> dict[str, Any]:
    """只取 dataclass 里认识的键，防止旧配置里的遗留字段把构造搞崩。"""
    return {k: v for k, v in source.items() if k in dc.__dataclass_fields__}


# ---------------- 读写 ----------------
def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config() -> AppConfig:
    data: dict[str, Any] = {}
    try:
        if defaults_file().exists():
            data = json.loads(defaults_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("读取默认配置失败：%s", exc)
    try:
        cf = config_file()
        if cf.exists():
            data = _deep_merge(data, json.loads(cf.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("读取用户配置失败：%s", exc)
    return AppConfig.from_dict(data)


def save_config(cfg: AppConfig) -> Path:
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path
