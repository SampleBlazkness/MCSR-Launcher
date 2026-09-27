"""Java 选择对话框 + Java 提示行的交互探针。

    python tools/ui_java_picker_probe.py

验证的点（都是这次修的 bug 相关）：
  1. 列表按"匹配优先、版本从高到低"排序，且默认预选的是**匹配实例要求**的那个
     —— 旧逻辑是拿目录名字符串排序取最后一个，永远挑中 zulu-8。
  2. 当前配置的那个 Java 会被标成"（当前使用）"。
  3. "只显示匹配的版本"筛选生效。
  4. 双击/点"使用所选"能选中正确的 Java 并 accept。
  5. 主窗口的提示行：Java 与实例要求不符时红字 + 出现"换成 Java x"按钮，
     点一下真的换成匹配的那个。
输出写文件（pythonw 没有 stdout），有断言失败就退出码 1。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("MCSL_DATA_DIR", str(Path(tempfile.gettempdir()) / "mcsl-javaprobe"))
os.environ["MCSL_NO_ELEVATE"] = "1"

from PySide6.QtCore import Qt                                      # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog                # noqa: E402

from mcsl.config import load_config                                # noqa: E402
from mcsl.launch.java import find_java_installations               # noqa: E402
from mcsl.ui.dialogs import JavaPickerDialog                       # noqa: E402
from mcsl.ui.main_window import MainWindow                         # noqa: E402

FAILED = False
OUT = Path(tempfile.gettempdir()) / "mcsl-java-probe.txt"
SHOTS = Path(tempfile.gettempdir()) / "mcsl-java-shots"


def note(text: str) -> None:
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    note(f"{'[ok]  ' if ok else '[FAIL]'} {label}" + (f"  ← {detail}" if detail else ""))
    if not ok:
        FAILED = True


def main() -> int:
    OUT.unlink(missing_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv[:1])

    installs = find_java_installations()
    note(f"本机扫到 {len(installs)} 个 Java：" + "、".join(
        f"Java {i.major}({i.version})" for i in installs))
    java8 = next((i for i in installs if i.major == 8), None)
    java_high = max(installs, key=lambda i: i.major) if installs else None
    if java8 is None or java_high is None or java8.major == java_high.major:
        note("本机 Java 情况不满足测试前提（需要同时有 Java 8 和一个更高版本），跳过")
        return 0

    # ---------------------------------------------------------------- 对话框
    dialog = JavaPickerDialog(instance="1.16.1", required_major=8,
                              current_path=str(java_high.path))
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    dialog.show()
    app.processEvents()
    dialog.grab().save(str(SHOTS / "java_picker_1.16.1.png"))

    rows = dialog.table.rowCount()
    check("列表不只一行（旧逻辑只会挑一个）", rows >= 2, f"{rows} 行")

    first = dialog.table.item(0, 0).data(Qt.ItemDataRole.UserRole)
    check("第一行是匹配 Java 8 的那个", first is not None and first.major == 8,
          f"第一行 = {first.version_label if first else '空'}")
    check("默认预选的就是第一行", dialog.table.currentRow() == 0,
          f"currentRow={dialog.table.currentRow()}")
    check("匹配列写着 ✓ 匹配", "✓" in dialog.table.item(0, 1).text(),
          dialog.table.item(0, 1).text())

    current_label = [dialog.table.item(r, 0).text() for r in range(rows)
                     if "当前使用" in dialog.table.item(r, 0).text()]
    check("当前配置的那个被标成（当前使用）", len(current_label) == 1,
          str(current_label))

    summary = dialog.summary.text()
    check("概要说清了匹配数量", "匹配 Java 8" in summary, summary[:80])

    dialog.only_match.setChecked(True)
    app.processEvents()
    all_match = all(dialog.table.item(r, 1).text().startswith("✓")
                    for r in range(dialog.table.rowCount()))
    check("勾上\"只显示匹配的版本\"后只剩匹配的", all_match and dialog.table.rowCount() >= 1,
          f"{dialog.table.rowCount()} 行")
    dialog.only_match.setChecked(False)
    app.processEvents()

    dialog.table.selectRow(0)
    dialog.use_button.click()
    check("点\"使用所选\"选中了 Java 8 并 accept",
          dialog.selected is not None and dialog.selected.major == 8
          and dialog.result() == QDialog.DialogCode.Accepted,
          f"selected={dialog.selected.version_label if dialog.selected else 'None'}")
    dialog.deleteLater()

    # ---------------------------------------------------------------- 主窗口提示行
    cfg = load_config()
    cfg.launch.instance = "1.16.1"
    cfg.launch.java_path = str(java_high.path)     # 故意配错：1.16.1 要 Java 8
    window = MainWindow(cfg)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    app.processEvents()

    check("提示行指出当前 Java 与实例不符",
          "需要 Java 8" in window.java_hint.text() and "✗" in window.java_hint.text(),
          window.java_hint.text()[:90])
    check("出现\"换成 Java 8\"按钮", window.java_fix_button.isVisible()
          and "Java 8" in window.java_fix_button.text(),
          window.java_fix_button.text())
    window.grab().save(str(SHOTS / "launch_page_java_hint.png"))

    window.java_fix_button.click()
    app.processEvents()
    picked = window.picker_java.value()
    check("一键替换真的把路径换成了 Java 8",
          java8.path.name in picked or str(java8.path).lower() == picked.lower(),
          f"picker={picked}")
    check("换完之后提示行变绿", "✓" in window.java_hint.text(),
          window.java_hint.text()[:90])

    # 每实例存储：勾上"只对当前实例生效"后，java 应写进实例覆盖而不是全局
    window.per_instance_check.setChecked(True)
    app.processEvents()
    collected = window._collect_from_ui()
    override = collected.launch.instances.get("1.16.1")
    check("每实例模式下 Java 存进实例覆盖",
          override is not None and override.java_path == picked,
          f"override.java_path={getattr(override, 'java_path', None)}")

    window.close()
    window.deleteLater()
    app.processEvents()

    # ------------------------------------------------ 配置本来就匹配时：绿字、没有替换按钮
    cfg2 = load_config()
    cfg2.launch.instance = "1.16.1"
    cfg2.launch.java_path = str(java8.path)
    window2 = MainWindow(cfg2)
    window2.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window2.show()
    app.processEvents()
    check("配置匹配时提示是绿字 ✓",
          "✓" in window2.java_hint.text() and "匹配" in window2.java_hint.text(),
          window2.java_hint.text()[:90])
    check("配置匹配时不出现一键替换按钮",
          not window2.java_fix_button.isVisible(), window2.java_fix_button.text())
    check("配置匹配时不会拦启动（_confirm_java_version 直接放行）",
          window2._confirm_java_version() is True)
    window2.close()
    window2.deleteLater()
    app.processEvents()

    note(f"\n截图：{SHOTS}")
    note(f"结论：{'全部通过' if not FAILED else '有失败项'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
