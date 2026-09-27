"""把界面渲染成图片，用来肉眼检查布局（不截屏、尽量不弹窗）。

    python tools/ui_screenshot.py [输出目录]

每个标签页一张 PNG。用的是 QWidget.grab()，也就是 Qt 自己把控件画进图片。
注意：必须用真实的 Windows 平台插件 + WA_DontShowOnScreen，
否则 offscreen 平台**没有字体库**，文字会全渲染成豆腐块（□□□），
看起来像程序坏了其实是渲染假象。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 不设 QT_QPA_PLATFORM：用真实平台插件，字体才正常
os.environ.pop("QT_QPA_PLATFORM", None)
os.environ.setdefault("MCSL_DATA_DIR", str(Path(tempfile.gettempdir()) / "mcsl-shot"))
os.environ["MCSL_NO_ELEVATE"] = "1"   # 测试用：跳过提权自重启

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt                                    # noqa: E402
from PySide6.QtWidgets import QApplication, QTabWidget           # noqa: E402

from mcsl.config import load_config                               # noqa: E402
from mcsl.ui.main_window import MainWindow                        # noqa: E402


def main(argv: list[str]) -> int:
    out_dir = Path(argv[0]) if argv else Path(tempfile.gettempdir()) / "mcsl-shots"
    out_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication(sys.argv[:1])          # noqa: F841
    cfg = load_config()
    window = MainWindow(cfg)
    window.resize(1060, 900)
    # 关键：布局/绘制照常进行，但不真的显示在屏幕上
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    app.processEvents()

    tabs: QTabWidget | None = window.findChild(QTabWidget)
    # 顶层（账号栏 + 若干标签页 + 日志）整窗一张
    whole = out_dir / "window.png"
    window.grab().save(str(whole))
    print(f"SAVED {whole}")

    if tabs is not None:
        for index in range(tabs.count()):
            tabs.setCurrentIndex(index)
            app.processEvents()
            name = "".join(ch if ch.isalnum() else "_" for ch in tabs.tabText(index))
            path = out_dir / f"tab{index}_{name}.png"
            tabs.currentWidget().grab().save(str(path))
            print(f"SAVED {path}")
        tabs.setCurrentIndex(0)

    # 日志区单独一张（检查等宽字体与滚动）
    window.log_view.append("示例日志：正在获取 Minecraft 登录令牌…")
    window.log_view.append("实例 1.16.1｜Java 要求 8｜classpath 42 项")
    window.log_view.append("已启动 OBS（PID 1234，管理员权限）")
    app.processEvents()
    log_path = out_dir / "log.png"
    window.log_view.grab().save(str(log_path))
    print(f"SAVED {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
