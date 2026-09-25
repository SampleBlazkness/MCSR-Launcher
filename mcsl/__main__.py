"""python -m mcsl —— 启动图形界面。"""
from __future__ import annotations

import logging
import sys


def main() -> int:
    from . import APP_NAME, APP_VERSION
    from .logging_setup import log_path, setup

    setup()
    log = logging.getLogger("mcsl")
    from .paths import config_file, data_root
    from .tools import winproc

    log.info("%s %s 启动", APP_NAME, APP_VERSION)
    log.info("数据目录：%s｜配置文件：%s", data_root(), config_file())
    log.info("日志文件：%s", log_path())
    log.info("是否提权：%s｜Python：%s", winproc.is_self_elevated(), sys.executable)
    try:
        log.info("Python 版本：%s", sys.version.split()[0])
    except Exception:  # noqa: BLE001 - 纯日志，不该影响启动
        pass

    from .ui.app import run

    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
