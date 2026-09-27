"""界面冒烟测试（离屏运行，不需要人看屏幕）。

    set MCSL_DATA_DIR=%TEMP%\\mcsl-smoke
    python tools/ui_smoke.py

验证：主窗口能构建、配置能正确载入控件、控件改动能正确写回配置、
按实例覆盖的开关逻辑正确、账号栏能刷新。用离屏平台跑，不会弹窗。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 离屏 + 独立数据目录（不碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MCSL_DATA_DIR", str(Path(tempfile.gettempdir()) / "mcsl-smoke"))
os.environ["MCSL_NO_ELEVATE"] = "1"   # 测试用：跳过提权自重启

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt                                     # noqa: E402
from PySide6.QtGui import QGuiApplication                         # noqa: E402
from PySide6.QtWidgets import QApplication                       # noqa: E402

from mcsl.config import load_config, save_config                 # noqa: E402
from mcsl.ui.main_window import MainWindow                       # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[ok]   {name}")
    else:
        print(f"[FAIL] {name}" + (f" —— {detail}" if detail else ""))
        FAILURES.append(name)


def main() -> int:
    app = QApplication(sys.argv[:1])          # noqa: F841 - 必须存在
    cfg = load_config()
    window = MainWindow(cfg)
    window.show()                             # 触发 showEvent（居中在这里做）
    app.processEvents()

    print("=== 窗口位置 ===")
    area = (window.screen() or QGuiApplication.primaryScreen()).availableGeometry()
    geo = window.frameGeometry()
    check("窗口左上角在可用区域内（标题栏不会跑到屏幕外）",
          area.contains(geo.topLeft()),
          f"窗口 {geo.width()}x{geo.height()} @({geo.x()},{geo.y()}) 屏幕 {area.width()}x{area.height()}")

    # 窗口放得下就居中；放不下（离屏平台的屏幕只有 800x800）就该贴边而不是跑到屏幕外，
    # 所以两种情况分别断言 —— 只按"居中"断言在离屏下必然失败。
    if geo.width() <= area.width():
        dx = abs(geo.center().x() - area.center().x())
        dx_desc = f"水平居中偏差 {dx}px"
    else:
        dx = abs(geo.x() - area.x())
        dx_desc = f"窗口比屏幕宽，应贴左边：实际 x={geo.x()}，屏幕 x={area.x()}"
    if geo.height() <= area.height():
        dy = abs(geo.center().y() - area.center().y())
        dy_desc = f"垂直居中偏差 {dy}px"
    else:
        dy = abs(geo.y() - area.y())
        dy_desc = f"窗口比屏幕高，应贴顶部：实际 y={geo.y()}，屏幕 y={area.y()}"
    check("窗口按可用区域居中或贴边（都不会跑到屏幕外）", dx <= 8 and dy <= 8,
          f"{dx_desc}；{dy_desc}")

    print("=== 控件是否载入了配置 ===")
    check("路径框载入了 .minecraft", window.picker_mc.value() == cfg.launch.minecraft_dir,
          window.picker_mc.value())
    check("路径框载入了 NinjabrainBot",
          window.picker_njb.value() == cfg.tools["ninjabrainbot"].path)
    check("路径框载入了 OBS", window.picker_obs.value() == cfg.tools["obs"].path)
    check("内存框载入了配置", window.memory_spin.value() == cfg.launch.effective().memory_mb)
    check("实例下拉非空", window.instance_combo.count() > 0,
          f"{window.instance_combo.count()} 项")
    check("实例下拉包含配置里的实例",
          cfg.launch.instance in [window.instance_combo.itemText(i)
                                  for i in range(window.instance_combo.count())])
    check("JVM 预设选中 custom", window.preset_combo.currentData() == cfg.launch.effective().jvm_preset)
    check("追加参数框显示了调参", "-XX:+UseG1GC" in window.extra_args_edit.text())
    check("工具关闭策略载入（OBS=询问）",
          next(w["close"].currentData() for n, w in window.tool_widgets.items() if n == "obs") == "ask")

    print("\n=== 保存按钮的脏检测 ===")
    check("刚打开时保存按钮是灰的", not window.save_button.isEnabled(),
          f"enabled={window.save_button.isEnabled()}")
    before = window.memory_spin.value()
    window.memory_spin.setValue(before + 512)
    window._refresh_save_button()
    check("改了配置后按钮变为可点", window.save_button.isEnabled())
    window.memory_spin.setValue(before)
    window._refresh_save_button()
    check("改回原样后按钮又变灰", not window.save_button.isEnabled())
    window.extra_args_edit.setText(window.extra_args_edit.text() + " ")
    window._refresh_save_button()
    check("只加一个空格也算改动", window.save_button.isEnabled())
    window.extra_args_edit.setText(window.extra_args_edit.text().rstrip())
    window._refresh_save_button()
    check("去掉那个空格后再次变灰", not window.save_button.isEnabled())

    print("\n=== 控件改动是否写回配置 ===")
    window.memory_spin.setValue(6144)
    window.extra_args_edit.setText("-XX:+UseG1GC -Dfile.encoding=GB18030")
    window.picker_njb.set_value("D:/tmp/NJB.jar")
    window.tool_widgets["obs"]["delay"].setValue(1500)
    collected = window._collect_from_ui()
    check("内存写回", collected.launch.effective().memory_mb == 6144)
    check("追加参数写回",
          collected.launch.effective().jvm_args_extra == "-XX:+UseG1GC -Dfile.encoding=GB18030")
    check("工具路径写回", collected.tools["ninjabrainbot"].path == "D:/tmp/NJB.jar")
    check("工具延迟写回", collected.tools["obs"].start_delay_ms == 1500)

    print("\n=== 按实例覆盖 ===")
    window.memory_spin.setValue(2048)
    window.per_instance_check.setChecked(True)
    collected = window._collect_from_ui()
    instance = collected.launch.instance
    check("覆盖写进了 instances 表", instance in collected.launch.instances)
    check("覆盖值生效", collected.launch.effective(instance).memory_mb == 2048)
    check("全局值没被改动", collected.launch.memory_mb == 6144)
    window.per_instance_check.setChecked(False)
    collected = window._collect_from_ui()
    check("取消勾选后覆盖被清除", instance not in collected.launch.instances)

    print("\n=== 保存与读取往返 ===")
    path = save_config(collected)
    reloaded = load_config()
    check("配置能存到数据目录（不在仓库里）",
          str(path).startswith(os.environ["MCSL_DATA_DIR"]), str(path))
    check("重读一致", reloaded.launch.effective().memory_mb == 2048
          and reloaded.tools["ninjabrainbot"].path == "D:/tmp/NJB.jar")

    print("\n=== 切换实例后 Java 提示要跟着更新 ===")
    hint_before = window.java_hint.text()
    names = [window.instance_combo.itemText(i) for i in range(window.instance_combo.count())]
    target = next((n for n in names if n != window.instance_combo.currentText()), None)
    if target is None:
        check("至少有两个实例可供切换（否则这条测不了）", False, f"只有 {names}")
    else:
        window.instance_combo.setCurrentIndex(window.instance_combo.findText(target))
        app.processEvents()
        hint_after = window.java_hint.text()
        check(f"切到 {target} 后提示立刻更新（不需要点别的按钮）",
              hint_after != hint_before and target in hint_after,
              f"切换前 {hint_before[:40]} → 切换后 {hint_after[:60]}")

    print("\n=== 账号栏 ===")
    window._refresh_account_bar()
    check("未登录时显示提示", "未登录" in window.account_label.text())

    print("\n=== 登录方式只剩设备码（界面上不该再有这些选项）===")
    from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit
    auth_tab = window.tabs.widget(3)          # 0 路径 1 启动设置 2 辅助工具 3 账号与登录
    combos = auth_tab.findChildren(QComboBox)
    edits = auth_tab.findChildren(QLineEdit)
    check("账号页没有任何下拉框（端点预设/登录方式已移除）", not combos,
          f"{[c.currentText() for c in combos]}")
    check("账号页没有 client_id 输入框", not edits, f"{[e.text() for e in edits]}")
    check("页面上写清了设备码流程",
          any("设备码" in lbl.text() for lbl in auth_tab.findChildren(QLabel)))

    # 老配置里残留的 v2 / auth_code / 空 client_id 要在载入时被改回可用值
    stale = load_config()
    stale.auth.endpoint_preset = "v2"
    stale.auth.flow = "auth_code"
    stale.auth.client_id = ""
    window_stale = MainWindow(stale)
    window_stale.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window_stale.show()
    app.processEvents()
    check("残留的 v2 预设被改回 live",
          window_stale.cfg.auth.endpoint_preset == "live",
          str(window_stale.cfg.auth.endpoint_preset))
    check("残留的 auth_code 被改回 device_code",
          window_stale.cfg.auth.flow == "device_code", str(window_stale.cfg.auth.flow))
    check("空的 client_id 自动填成公开 id",
          window_stale.cfg.auth.client_id == "00000000402b5328",
          window_stale.cfg.auth.client_id)
    stale_collected = window_stale._collect_from_ui()
    check("保存出去的配置也是固定的 live + device_code",
          stale_collected.auth.endpoint_preset == "live"
          and stale_collected.auth.flow == "device_code")
    window_stale.close()
    window_stale.deleteLater()
    app.processEvents()

    window.close()
    print("=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} 项失败：" + "、".join(FAILURES))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
