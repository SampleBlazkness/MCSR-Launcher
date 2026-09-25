"""界面层（PySide6）。core 层不依赖这里，可单独用 CLI 驱动。"""
from __future__ import annotations

__all__ = ["run"]


def run(argv: list[str] | None = None) -> int:
    from .app import run as _run

    return _run(argv)
