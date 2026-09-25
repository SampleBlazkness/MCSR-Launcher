#!/usr/bin/env python3
"""提交前密钥扫描。

为什么需要：access_token / refresh_token 是本项目最容易手滑泄露的东西
（比如把调试用的配置、日志、渲染出的启动脚本一起 git add）。.gitignore
只能挡住"已知路径"，挡不住"有人把 token 直接粘进 README 或测试文件"。
所以再加一道内容级扫描。

设计原则：**只抓"字面量密钥"，不抓代码**。
    * `refresh_token=tokens.refresh_token`  → 变量引用，放过
    * `"access_token = 'eyJhbGci....'"`     → 真令牌，拦住
为此每条规则都配一个"像不像密钥"的判定，并且支持行内豁免标记
（在该行写 `secretscan: ignore`），避免为了绕过扫描而降低规则强度。

用法：
    python tools/check_secrets.py <文件...>      # 扫指定文件
    python tools/check_secrets.py --staged       # 扫 git 暂存区（pre-commit 用）
退出码：0 = 干净，1 = 发现问题。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

JWT_RE = re.compile(rb"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")
ASSIGN_RE = re.compile(
    rb"(?i)[\"']?(refresh[_-]?token|access[_-]?token|xsts[_-]?token|client[_-]?secret|"
    rb"password|microsoft[_-]?token)[\"']?\s*[:=]\s*[\"']([^\"'\s]{16,})[\"']")

# 这些词出现在值里就认为不是真密钥（示例/占位符/假数据）
PLACEHOLDER_MARKERS = (b"example", b"dummy", b"placeholder", b"redacted", b"sample",
                       b"your_", b"xxxx", b"fake", b"test-token", b"changeme")
# 纯标识符风格（全大写+下划线，或全字母）不算密钥
IDENTIFIER_RE = re.compile(rb"^[A-Za-z_][A-Za-z0-9_]*$")
IGNORE_MARKER = "secretscan: ignore"

RULES: list[tuple[re.Pattern[bytes], str]] = [
    (JWT_RE, "疑似 JWT（很可能是 access_token / id_token）"),
    (ASSIGN_RE, "疑似 token / 口令字面量"),
]

# 允许出现的白名单路径（正则匹配仓库相对路径）
ALLOWLIST = [
    re.compile(r"tools/(check_secrets\.py)$"),      # 本文件自身含模式字面量
    re.compile(r"\.githooks/"),
    re.compile(r"\.gitignore$"),
]


def _looks_like_secret(value: bytes) -> bool:
    if len(value) < 16:
        return False
    lowered = value.lower()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        return False
    if IDENTIFIER_RE.match(value):
        return False
    # 真令牌一般同时含字母和数字（或特别的符号密度）
    has_digit = any(48 <= ch <= 57 for ch in value)
    has_alpha = any((65 <= ch <= 90) or (97 <= ch <= 122) for ch in value)
    return has_digit and has_alpha


def _staged_files() -> list[Path]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [Path(p) for p in out.splitlines() if p.strip()]


def _find_problems(blob: bytes, rel: str) -> list[str]:
    problems: list[str] = []
    for rx, why in RULES:
        for match in rx.finditer(blob):
            line_start = blob.rfind(b"\n", 0, match.start()) + 1
            line_end = blob.find(b"\n", match.start())
            line = blob[line_start: line_end if line_end != -1 else len(blob)]
            if IGNORE_MARKER.encode() in line:
                continue
            value = match.group(2) if (match.lastindex or 0) >= 2 else match.group(0)
            if rx is ASSIGN_RE and not _looks_like_secret(value):
                continue
            line_no = blob[: match.start()].count(b"\n") + 1
            shown = match.group(0)[:28].decode("utf-8", "replace")
            problems.append(f"{rel}:{line_no}: {why} —— 命中片段: {shown}…")
    return problems


def scan(path: Path) -> list[str]:
    rel = path.as_posix()
    if any(rx.search(rel) for rx in ALLOWLIST):
        return []
    if not path.is_file():
        return []
    try:
        blob = path.read_bytes()
    except OSError as exc:
        return [f"{rel}: 无法读取（{exc}）"]
    problems = _find_problems(blob, rel)
    # 凭据存储文件名被提交（文件名本身就是问题）
    if re.match(r"(?i)^(accounts\.dat|tokens?\.json|local\.json)$", path.name):
        problems.append(f"{rel}: 凭据/本地配置文件被提交")
    return problems


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    files = _staged_files() if argv[0] == "--staged" else [Path(a) for a in argv]
    problems: list[str] = []
    for f in files:
        problems.extend(scan(f))
    if problems:
        print("\n[✘] 提交被阻止：检测到疑似凭据内容")
        for p in problems:
            print("   " + p)
        print("\n如果这是误报，可在该行加 `secretscan: ignore`（仅限示例/文档），"
              "或改写该内容。")
        print("确实要强行提交：git commit --no-verify（并确认仓库是私有的）。")
        return 1
    print(f"[✓] 密钥扫描通过（{len(files)} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
