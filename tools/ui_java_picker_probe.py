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
from PySide6.QtWidgets import QApplication, QDialog, QLabel        # noqa: E402

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
    check("第一行是符合声明的那个（Java 8）", first is not None and first.major == 8,
          f"第一行 = {first.version_label if first else '空'}")
    selected = dialog.table.item(dialog.table.currentRow(), 0).data(Qt.ItemDataRole.UserRole)
    check("默认选中的是【当前配置的】那个，不是声明要求的那个",
          selected is not None and selected.major == java_high.major,
          f"选中 = {selected.version_label if selected else '空'}")
    check("符合声明的那行标着 ✓ 符合", "✓" in dialog.table.item(0, 1).text(),
          dialog.table.item(0, 1).text())

    current_label = [dialog.table.item(r, 0).text() for r in range(rows)
                     if "当前使用" in dialog.table.item(r, 0).text()]
    check("当前配置的那个被标成（当前使用）", len(current_label) == 1,
          str(current_label))

    summary = dialog.summary.text()
    check("概要说清了符合声明的数量", "符合声明的 Java 8" in summary, summary[:80])
    check("对话框里写明了\"声明只是发布时的信息\"这一提醒",
          "发布时" in summary or any("发布时" in w.text() for w in dialog.findChildren(QLabel)),
          "在 caveat 标签里")

    dialog.only_match.setChecked(True)
    app.processEvents()
    all_match = all(dialog.table.item(r, 1).text().startswith("✓")
                    for r in range(dialog.table.rowCount()))
    check("勾上\"只显示符合声明的版本\"后只剩符合的",
          all_match and dialog.table.rowCount() >= 1, f"{dialog.table.rowCount()} 行")
    dialog.only_match.setChecked(False)
    app.processEvents()

    dialog.table.selectRow(0)
    dialog.use_button.click()
    check("选中 Java 8 后点\"使用所选\"能正常 accept",
          dialog.selected is not None and dialog.selected.major == 8
          and dialog.result() == QDialog.DialogCode.Accepted,
          f"selected={dialog.selected.version_label if dialog.selected else 'None'}")
    dialog.deleteLater()

    # ------------------------------------------------ 主窗口：比声明【高】不报警（本机真实情况）
    cfg = load_config()
    cfg.launch.instance = "1.16.1"          # 版本 JSON 声明 Java 8
    cfg.launch.instances.pop("1.16.1", None)   # 前面用例可能已写进实例覆盖
    cfg.launch.java_path = str(java_high.path)   # 实际用 Java 25，这才是对的
    window = MainWindow(cfg)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    app.processEvents()

    hint = window.java_hint.text()
    check("比声明高时不再报红（不出现 ✗）", "✗" not in hint and "●" in hint, hint[:100])
    check("提示里说明了那是\"版本 JSON 的声明\"", "声明" in hint, hint[:100])
    check("比声明高时不出现\"换成 Java 8\"这种反向按钮",
          not window.java_fix_button.isVisible(), window.java_fix_button.text())
    check("比声明高时不拦启动", window._confirm_java_version() is True)
    window.grab().save(str(SHOTS / "launch_page_java_newer.png"))

    # 每实例存储：勾上"只对当前实例生效"后，java 应写进实例覆盖而不是全局
    window.per_instance_check.setChecked(True)
    app.processEvents()
    collected = window._collect_from_ui()
    override = collected.launch.instances.get("1.16.1")
    check("每实例模式下 Java 存进实例覆盖",
          override is not None and override.java_path == str(java_high.path),
          f"override.java_path={getattr(override, 'java_path', None)}")

    window.close()
    window.deleteLater()
    app.processEvents()

    # ------------------------------------------------ 声明相符时：绿字、没有替换按钮
    cfg2 = load_config()
    cfg2.launch.instance = "1.16.1"
    # 注意：上面那个窗口 close() 时会把配置存盘（closeEvent 里 save_config），
    # 所以这里必须把实例覆盖清掉，否则读到的还是上一个用例写进去的 Java 25
    cfg2.launch.instances.pop("1.16.1", None)
    cfg2.launch.java_path = str(java8.path)
    window2 = MainWindow(cfg2)
    window2.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window2.show()
    app.processEvents()
    check("配置与声明相符时提示是绿字 ✓",
          "✓" in window2.java_hint.text() and "相符" in window2.java_hint.text(),
          window2.java_hint.text()[:90])
    check("相符时不出现一键替换按钮",
          not window2.java_fix_button.isVisible(), window2.java_fix_button.text())
    check("相符时不拦启动", window2._confirm_java_version() is True)
    window2.close()
    window2.deleteLater()
    app.processEvents()

    # ------------------------------------------------ 比声明【低】：这才是真的有问题
    from mcsl.launch.java import read_install_cached, version_state
    cfg3 = load_config()
    cfg3.launch.instance = "26.3-Fabric"      # 版本 JSON 声明 Java 25
    # 26.3-Fabric 在 defaults.json 里有自己的实例覆盖（java 指向 zulu-25），
    # 先去掉，让它落到全局设置，才好构造"配了 Java 8"这种偏低的情况
    cfg3.launch.instances.pop("26.3-Fabric", None)
    cfg3.launch.java_path = str(java8.path)   # 配了 Java 8 → 偏低
    window3 = MainWindow(cfg3)
    window3.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window3.show()
    app.processEvents()
    hint3 = window3.java_hint.text()
    check("比声明低时确实报红 ✗ 并说明可能失败",
          "✗" in hint3 and "大概率失败" in hint3, hint3[:100])
    check("比声明低时出现一键替换，且目标是更高的版本",
          window3.java_fix_button.isVisible() and "Java 25" in window3.java_fix_button.text(),
          window3.java_fix_button.text())
    check("version_state 判定：8 < 25 → old",
          version_state(read_install_cached(str(java8.path)), 25) == "old")
    window3.close()
    window3.deleteLater()
    app.processEvents()

    note(f"\n截图：{SHOTS}")
    note(f"结论：{'全部通过' if not FAILED else '有失败项'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
