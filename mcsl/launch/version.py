"""版本 JSON 解析：加载实例、合并 inheritsFrom、解析库与 classpath。

支持两种形态：
    1. 原生 Mojang / Fabric 版本 JSON（含 inheritsFrom，需要向父版本合并）
    2. HMCL 导出的已合并 JSON（无 inheritsFrom，patches 里带合并痕迹）
你的两个实例（1.16.1、26.3-Fabric）都是第 2 种，但第 1 种也必须能用
（以后从 Fabric 官网装实例就是那种形态）。
"""
from __future__ import annotations

import json
import logging
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .rules import evaluate_rules

log = logging.getLogger(__name__)


def detect_os_name() -> str:
    if os.name == "nt":
        return "windows"
    if platform.system() == "Darwin":
        return "osx"
    return "linux"


def detect_os_version() -> str:
    if os.name == "nt":
        try:
            return platform.version()   # 形如 10.0.22631
        except Exception:  # noqa: BLE001
            return ""
    return platform.release()


def detect_arch() -> str:
    machine = (platform.machine() or "").lower()
    if machine in ("amd64", "x86_64", "x64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("x86", "i386", "i686"):
        return "x86"
    return machine or "x86_64"


@dataclass
class Library:
    name: str
    path: Path | None          # 相对 .minecraft/libraries 的路径
    rules: list[dict[str, Any]] = field(default_factory=list)
    natives: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    from_natives_file: bool = False   # 是否为 natives 专用 jar（旧式）

    @property
    def present(self) -> bool:
        return self.path is not None and self.path.exists()


@dataclass
class MinecraftVersion:
    id: str
    main_class: str
    jar_id: str
    asset_index: str
    assets_name: str
    type: str
    java_major: int
    libraries: list[Library]
    jvm_args: list[Any]
    game_args: list[Any]
    logging: dict[str, Any] | None
    json_path: Path
    mc_dir: Path

    @property
    def jar_path(self) -> Path:
        return self.mc_dir / "versions" / self.jar_id / f"{self.jar_id}.jar"

    @property
    def assets_dir(self) -> Path:
        # asset_index 指向 libraries/…/assets 的情况也要照顾到
        return self.mc_dir / "assets"

    @property
    def game_dir(self) -> Path:
        return self.mc_dir / "versions" / self.id


# ----------------------------------------------------------------- 加载
def list_instances(mc_dir: Path) -> list[str]:
    versions = mc_dir / "versions"
    if not versions.is_dir():
        return []
    out = []
    for child in sorted(versions.iterdir(), key=lambda p: p.name.lower()):
        if child.is_dir() and (child / f"{child.name}.json").is_file():
            out.append(child.name)
    return out


def load_version(mc_dir: Path, version_id: str) -> MinecraftVersion:
    json_path = mc_dir / "versions" / version_id / f"{version_id}.json"
    if not json_path.is_file():
        raise FileNotFoundError(f"找不到版本文件：{json_path}")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    merged = _merge_inherited(mc_dir, data, seen={version_id})

    libs: list[Library] = []
    for entry in merged.get("libraries", []):
        lib = _parse_library(mc_dir, entry)
        if lib is not None:
            libs.append(lib)

    arguments = merged.get("arguments") or {}
    jvm_args = list(arguments.get("jvm") or [])
    game_args = list(arguments.get("game") or [])
    legacy = merged.get("minecraftArguments")
    if legacy and not game_args:
        game_args = legacy.split(" ")

    java_major = int((merged.get("javaVersion") or {}).get("majorVersion") or 8)
    asset_index = str((merged.get("assetIndex") or {}).get("id")
                      or merged.get("assets") or "")

    version = MinecraftVersion(
        id=str(merged.get("id") or version_id),
        main_class=str(merged.get("mainClass") or ""),
        jar_id=str(merged.get("jar") or version_id),
        asset_index=asset_index,
        assets_name=str(merged.get("assets") or asset_index),
        type=str(merged.get("type") or "release"),
        java_major=java_major,
        libraries=libs,
        jvm_args=jvm_args,
        game_args=game_args,
        logging=merged.get("logging"),
        json_path=json_path,
        mc_dir=mc_dir,
    )
    if not version.main_class:
        raise ValueError(f"版本 {version_id} 的 JSON 里没有 mainClass")
    return version


def _merge_inherited(mc_dir: Path, data: dict[str, Any], seen: set[str]) -> dict[str, Any]:
    """把 inheritsFrom 链向上合并：父在前，子覆盖/追加。"""
    parent_id = data.get("inheritsFrom")
    if not parent_id:
        return data
    if parent_id in seen:
        raise ValueError(f"版本继承出现环：{parent_id}")
    seen.add(parent_id)
    parent_path = mc_dir / "versions" / parent_id / f"{parent_id}.json"
    if not parent_path.is_file():
        raise FileNotFoundError(f"父版本 {parent_id} 的 JSON 不存在：{parent_path}")
    parent = _merge_inherited(mc_dir, json.loads(parent_path.read_text(encoding="utf-8")), seen)

    merged = dict(parent)
    for key in ("id", "mainClass", "jar", "assetIndex", "assets", "type", "javaVersion",
                "downloads", "logging", "releaseTime", "time"):
        if key in data and data[key] is not None:
            merged[key] = data[key]

    merged["libraries"] = _dedupe_libraries(
        list(parent.get("libraries") or []) + list(data.get("libraries") or []))

    if "arguments" in data:
        parent_args = parent.get("arguments") or {}
        child_args = data["arguments"] or {}
        merged["arguments"] = {
            "jvm": list(parent_args.get("jvm") or []) + list(child_args.get("jvm") or []),
            "game": list(parent_args.get("game") or []) + list(child_args.get("game") or []),
        }
    for key in ("minecraftArguments", "patches"):
        if key in data:
            merged[key] = data[key]
    return merged


def _dedupe_libraries(libs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同名库只保留最后一个（子版本优先），但保持首次出现的顺序。"""
    out: dict[str, dict[str, Any]] = {}
    for entry in libs:
        name = entry.get("name")
        if not name:
            continue
        out.pop(name, None)
        out[name] = entry
    return list(out.values())


def _parse_library(mc_dir: Path, entry: dict[str, Any]) -> Library | None:
    name = entry.get("name")
    if not name:
        return None
    downloads = entry.get("downloads") or {}
    rel = (downloads.get("artifact") or {}).get("path")
    if not rel:
        rel = maven_path(name)
    path = mc_dir / "libraries" / rel if rel else None
    return Library(
        name=name,
        path=path,
        rules=list(entry.get("rules") or []),
        natives=dict(entry.get("natives") or {}),
        raw=entry,
        from_natives_file="natives" in entry,
    )


def maven_path(name: str) -> str:
    """"group:artifact:version[:classifier]" → libraries 下的相对路径。"""
    parts = name.split(":")
    if len(parts) < 3:
        return ""
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = f"-{parts[3]}" if len(parts) > 3 else ""
    return f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}{classifier}.jar"


# ----------------------------------------------------------------- classpath
@dataclass
class ResolvedLibraries:
    classpath: list[Path]
    missing: list[Path]
    natives_to_extract: list[tuple[Library, str]]   # (库, classifier)


def resolve_libraries(version: MinecraftVersion, *, os_name: str | None = None,
                      os_version: str | None = None, os_arch: str | None = None,
                      features: dict[str, bool] | None = None) -> ResolvedLibraries:
    os_name = os_name or detect_os_name()
    os_version = os_version if os_version is not None else detect_os_version()
    os_arch = os_arch or detect_arch()

    classpath: list[Path] = []
    missing: list[Path] = []
    natives: list[tuple[Library, str]] = []

    for lib in version.libraries:
        if not evaluate_rules(lib.rules, os_name=os_name, os_version=os_version,
                              os_arch=os_arch, features=features):
            continue
        if lib.from_natives_file:
            classifier = _native_classifier(lib, os_name, os_arch)
            if classifier:
                natives.append((lib, classifier))
            continue          # 旧式 natives 库不进 classpath（由 -Djava.library.path 提供）
        if lib.path is None:
            continue
        if lib.path.exists():
            classpath.append(lib.path)
        else:
            missing.append(lib.path)

    if version.jar_path.exists():
        classpath.append(version.jar_path)
    else:
        missing.append(version.jar_path)

    return ResolvedLibraries(classpath=classpath, missing=missing, natives_to_extract=natives)


def _native_classifier(lib: Library, os_name: str, os_arch: str) -> str | None:
    template = lib.natives.get(os_name)
    if not template:
        return None
    arch_bits = "64" if os_arch == "x86_64" else "32"
    return template.replace("${arch}", arch_bits)


__all__ = [
    "Library", "MinecraftVersion", "ResolvedLibraries",
    "list_instances", "load_version", "resolve_libraries",
    "maven_path", "detect_os_name", "detect_os_version", "detect_arch",
]
