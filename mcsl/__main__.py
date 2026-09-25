"""python -m mcsl —— 启动图形界面。"""
from __future__ import annotations

import sys


def main() -> int:
    from .logging_setup import setup
    from .ui.app import run

    setup()
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
