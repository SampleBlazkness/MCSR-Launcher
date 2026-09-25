"""根据版本 JSON + 会话信息组装完整的 java 命令行。

设计要点：
  * 完全由版本 JSON 驱动（arguments.jvm / arguments.game + 占位符），
    和 HMCL / 官方启动器同一套逻辑，因此以后装新实例不用改代码。
  * ${classpath} 由 libraries 解析结果拼出，客户端 jar 放最后（与 HMCL 一致）。
  * 追加参数（内存、GC、-Dfile.encoding 等）来自配置，用户可以逐字保留自己
    调好的那套（见 config/defaults.json 里的 custom 预设）。
"""
from __future__ import annotations

import logging
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import APP_NAME, APP_VERSION
from ..config import LaunchConfig
from . import natives as natives_mod
from .rules import evaluate_rules
from .version import MinecraftVersion, resolve_libraries

log = logging.getLogger(__name__)

# Aikar 的 GC 参数（MC 1.16+ 社区公认基线）
AIKAR_FLAGS = (
    "-XX:+UseG1GC -XX:+ParallelRefProcEnabled -XX:MaxGCPauseMillis=200 "
    "-XX:+UnlockExperimentalVMOptions -XX:+DisableExplicitGC -XX:+AlwaysPreTouch "
    "-XX:G1NewSizePercent=30 -XX:G1MaxNewSizePercent=40 -XX:G1HeapRegionSize=8M "
    "-XX:G1ReservePercent=20 -XX:G1HeapWastePercent=5 -XX:G1MixedGCCountTarget=4 "
    "-XX:InitiatingHeapOccupancyPercent=15 -XX:G1MixedGCLiveThresholdPercent=90 "
    "-XX:G1RSetUpdatingPauseTimePercent=5 -XX:SurvivorRatio=32 "
    "-XX:+PerfDisableSharedMem -XX:MaxTenuringThreshold=1 "
    "-Dusing.aikars.flags=https://mcflags.emc.gs -Daikars.new.flags=true"
)

# HMCL / 官方启动器都会加的一批加固参数（log4shell、RMI 反序列化等），保留它们
HARDENING_FLAGS = (
    "-Djava.rmi.server.useCodebaseOnly=true "
    "-Dcom.sun.jndi.rmi.object.trustURLCodebase=false "
    "-Dcom.sun.jndi.cosnaming.object.trustURLCodebase=false "
    "-Dlog4j2.formatMsgNoLookups=true"
)

DEFAULT_FEATURES = {
    "is_demo_user": False,
    "has_custom_resolution": False,
    "has_quick_plays_support": False,
    "is_quick_play_singleplayer": False,
    "is_quick_play_multiplayer": False,
    "is_quick_play_realms": False,
}


@dataclass
class Command:
    java: Path
    args: list[str]                    # 不含 java 本身
    cwd: Path
    env: dict[str, str]
    natives_dir: Path
    classpath: list[Path] = field(default_factory=list)
    missing: list[Path] = field(default_factory=list)
    stdout_encoding: str = "utf-8"

    def redacted(self) -> str:
        """用于日志/界面展示的命令行（access token 已打码）。"""
        from ..logging_setup import scrub

        return scrub(" ".join([str(self.java)] + [str(a) for a in self.args]))

    def argv(self) -> list[str]:
        return [str(self.java)] + [str(a) for a in self.args]


def build_command(version: MinecraftVersion, session, cfg: LaunchConfig, *,
                  java_path: str | Path | None = None,
                  launcher_name: str = APP_NAME, launcher_version: str = APP_VERSION,
                  features: dict[str, bool] | None = None) -> Command:
    """把一切拼成可执行的命令行。session 需提供 uuid/name/access_token/token_type/client_id/xuid。"""
    mc_dir = Path(cfg.minecraft_dir)
    java = Path(java_path or cfg.java_path)
    if not java.is_file():
        raise FileNotFoundError(f"java 可执行文件不存在：{java}")

    resolved = resolve_libraries(version, features=features)
    if resolved.missing:
        for path in resolved.missing[:8]:
            log.warning("缺少库文件：%s", path)
        log.warning("共缺少 %d 个库文件，游戏大概率启动失败", len(resolved.missing))

    natives_path = natives_mod.prepare(version)
    classpath = os.pathsep.join(str(p) for p in resolved.classpath)

    resolved_features = dict(DEFAULT_FEATURES)
    resolved_features["has_custom_resolution"] = bool(cfg.width and cfg.height)
    if features:
        resolved_features.update(features)

    substitutions = _substitutions(version, session, cfg, mc_dir, natives_path,
                                   classpath, launcher_name, launcher_version)

    jvm_args: list[str] = []
    memory = max(512, int(cfg.memory_mb or 4096))
    jvm_args += [f"-Xms{memory}M", f"-Xmx{memory}M"]
    jvm_args += _preset_flags(cfg.jvm_preset)
    jvm_args += _split(cfg.jvm_args_extra)
    jvm_args += _log4j_flag(version)
    # HMCL 会显式告诉游戏客户端 jar 在哪（部分 mod / 工具会读这个属性），保持一致
    if version.jar_path.is_file():
        jvm_args.append(f"-Dminecraft.client.jar={version.jar_path}")
    jvm_args += _evaluate_arg_list(version.jvm_args, substitutions, resolved_features)

    game_args = _evaluate_arg_list(version.game_args, substitutions, resolved_features)
    game_args += _split(cfg.game_args_extra)

    env = dict(os.environ)
    if cfg.env_overrides:
        env.update(cfg.env_overrides)
    else:
        # HMCL 也会这么干：把 APPDATA 指到 .minecraft 的上一级，
        # 避免某些库悄悄写到 C:\Users\<user>\AppData\Roaming\.minecraft
        env["APPDATA"] = str(mc_dir.parent)

    stdout_encoding = _find_encoding(jvm_args)

    return Command(
        java=java,
        args=jvm_args + [version.main_class] + game_args,
        cwd=version.game_dir,
        env=env,
        natives_dir=natives_path,
        classpath=resolved.classpath,
        missing=resolved.missing,
        stdout_encoding=stdout_encoding,
    )


def _substitutions(version: MinecraftVersion, session, cfg: LaunchConfig, mc_dir: Path,
                   natives_path: Path, classpath: str, launcher_name: str,
                   launcher_version: str) -> dict[str, str]:
    return {
        "auth_player_name": session.name,
        "version_name": version.id,
        "game_directory": str(version.game_dir),
        "assets_root": str(version.assets_dir),
        "game_assets": str(version.assets_dir),
        "assets_index_name": version.asset_index,
        "auth_uuid": session.uuid,
        "auth_access_token": session.access_token,
        "auth_session": session.access_token,
        "user_type": "msa",
        "version_type": cfg.version_type or version.type,
        "clientid": getattr(session, "client_id", "") or "",
        "auth_xuid": getattr(session, "xuid", "") or "",
        "natives_directory": str(natives_path),
        "launcher_name": launcher_name,
        "launcher_version": launcher_version,
        "classpath": classpath,
        "resolution_width": str(cfg.width or 854),
        "resolution_height": str(cfg.height or 480),
        "quickPlayPath": "",
        "quickPlaySingleplayer": "",
        "quickPlayMultiplayer": "",
        "quickPlayRealms": "",
    }


def _preset_flags(preset: str) -> list[str]:
    preset = (preset or "vanilla").lower()
    if preset == "aikar":
        return _split(AIKAR_FLAGS) + _split(HARDENING_FLAGS)
    if preset == "vanilla":
        return _split(HARDENING_FLAGS)
    # custom：完全按用户给的来（defaults.json 里的 custom 默认值已经含加固参数）
    return []


def _log4j_flag(version: MinecraftVersion) -> list[str]:
    """版本目录里有 HMCL 生成的 log4j2.xml 就用它（它已打上 log4shell 补丁）。"""
    candidate = version.game_dir / "log4j2.xml"
    if candidate.is_file():
        return [f"-Dlog4j.configurationFile={candidate}"]
    return []


def _evaluate_arg_list(args: list[Any], substitutions: dict[str, str],
                       features: dict[str, bool]) -> list[str]:
    out: list[str] = []
    for item in args:
        if isinstance(item, str):
            out.append(_substitute(item, substitutions))
            continue
        if not isinstance(item, dict):
            continue
        if not evaluate_rules(item.get("rules"), features=features):
            continue
        value = item.get("value")
        if isinstance(value, str):
            out.append(_substitute(value, substitutions))
        elif isinstance(value, list):
            out.extend(_substitute(str(v), substitutions) for v in value)
    return out


def _substitute(text: str, substitutions: dict[str, str]) -> str:
    if "${" not in text:
        return text
    for key, value in substitutions.items():
        text = text.replace("${" + key + "}", value)
    if "${" in text:
        # 未知占位符：留着会让游戏报参数错误，这里明确告警并清空
        unresolved = text[text.index("${"):].split("}", 1)[0] + "}"
        log.warning("版本 JSON 里的占位符 %s 没有对应值，已置空", unresolved)
        text = text.replace(unresolved, "")
    return text


def _split(text: str | None) -> list[str]:
    """按 shell 规则切分参数。

    用 posix=False 是为了不破坏 Windows 路径里的反斜杠（posix=True 会把
    C:\\Program Files 变成 C:Program Files），代价是引号会留在 token 里，
    所以这里再把成对的引号剥掉。
    """
    if not text:
        return []
    try:
        tokens = shlex.split(text, posix=False)
    except ValueError as exc:
        log.warning("参数解析失败（%s），按空格切分：%s", exc, text)
        tokens = text.split()
    out = []
    for token in tokens:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        if token:
            out.append(token)
    return out


def _find_encoding(jvm_args: list[str]) -> str:
    for arg in jvm_args:
        if arg.startswith("-Dstdout.encoding=") or arg.startswith("-Dsun.stdout.encoding="):
            return arg.split("=", 1)[1] or "utf-8"
        if arg.startswith("-Dfile.encoding="):
            enc = arg.split("=", 1)[1]
            if enc:
                return enc
    return "utf-8"
