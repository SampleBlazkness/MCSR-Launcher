"""日志：同时输出到控制台和 %LOCALAPPDATA%\\MCSR-Launcher\\logs\\。

关键约束：**任何 token 都不允许进日志**。这里提供一个 scrub()，
所有需要记录的命令行 / HTTP 响应都先过一遍。
"""
from __future__ import annotations

import logging
import re
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .paths import logs_dir

# 命中这些模式的内容在写日志前会被替换掉
_SECRET_PATTERNS = [
    re.compile(r"(eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,})"),
    re.compile(r"((?i:access[_-]?token|refresh[_-]?token|xsts|identityToken)[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9_\-\.~]{16,})"),
    re.compile(r"(--accessToken[\"']?\s*[,=]?\s*[\"'])([A-Za-z0-9_\-\.~]{16,})"),
]


def scrub(text: str) -> str:
    """把疑似凭据替换成 <redacted>，用于日志与界面显示。"""
    if not text:
        return text
    out = _SECRET_PATTERNS[0].sub("<redacted-jwt>", text)
    out = _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)}<redacted>", out)
    out = _SECRET_PATTERNS[2].sub(lambda m: f"{m.group(1)}<redacted>", out)
    return out


class _ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - 格式化失败不该吞日志
            return True
        cleaned = scrub(msg)
        if cleaned != msg:
            record.msg = cleaned
            record.args = ()
        return True


_configured = False
_log_path: Path | None = None


def setup(level: int = logging.INFO, *, console: bool = True, filename: str | None = None) -> Path:
    """初始化根 logger，返回日志文件路径。重复调用是幂等的。"""
    global _configured, _log_path
    if _configured and _log_path is not None:
        return _log_path

    logs_dir().mkdir(parents=True, exist_ok=True)
    name = filename or f"mcsl-{datetime.now():%Y%m%d}.log"
    path = logs_dir() / name
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")

    root = logging.getLogger()
    root.setLevel(level)
    scrub_filter = _ScrubFilter()

    fh = RotatingFileHandler(path, maxBytes=4 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(scrub_filter)
    root.addHandler(fh)

    if console and sys.stderr is not None:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        sh.addFilter(scrub_filter)
        root.addHandler(sh)

    for noisy in ("urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    _log_path = path
    return path


def log_path() -> Path | None:
    return _log_path
