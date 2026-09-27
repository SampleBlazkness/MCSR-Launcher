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

    print("\n=== 账号栏 ===")
    window._refresh_account_bar()
    check("未登录时显示提示", "未登录" in window.account_label.text())

    print("\n=== 登录方式与 client_id 的冲突检测（实测过的死路）===")
    # 实测：公开 id 没有登记 localhost 回调，浏览器授权必失败（redirect_uri is not valid），
    # 所以在点登录前就该拦下来，而不是让用户在浏览器里看到一句英文报错。
    old_flow, old_client = window.cfg.auth.flow, window.cfg.auth.client_id
    window.cfg.auth.flow = "auth_code"
    window.cfg.auth.client_id = "00000000402b5328"
    problem = window._browser_flow_problem()
    check("公开 id + 浏览器授权 → 拦下来并解释原因",
          bool(problem) and "redirect_uri" in problem, str(problem)[:70])

    window.cfg.auth.client_id = "11112222-3333-4444-5555-666677778888"
    check("自己注册的 Azure id + 浏览器授权 → 不拦",
          window._browser_flow_problem() is None)

    window.cfg.auth.flow = "device_code"
    window.cfg.auth.client_id = "00000000402b5328"
    check("公开 id + 设备码（当前默认组合）→ 不拦",
          window._browser_flow_problem() is None)
    window.cfg.auth.flow, window.cfg.auth.client_id = old_flow, old_client

    print("\n=== 走不通的选项不该出现在下拉框里 ===")
    window.client_id_edit.setText("00000000402b5328")
    window._refresh_auth_options()
    presets = [window.preset_auth_combo.itemData(i) for i in range(window.preset_auth_combo.count())]
    flows = [window.flow_combo.itemData(i) for i in range(window.flow_combo.count())]
    check("公开 id：端点预设只剩 live", presets == ["live"], str(presets))
    check("公开 id：登录方式只剩设备码", flows == ["device_code"], str(flows))

    window.client_id_edit.setText("11112222-3333-4444-5555-666677778888")
    window._refresh_auth_options()
    presets = [window.preset_auth_combo.itemData(i) for i in range(window.preset_auth_combo.count())]
    flows = [window.flow_combo.itemData(i) for i in range(window.flow_combo.count())]
    check("填了自己的 client_id 后 v2 与浏览器授权自动回来",
          presets == ["live", "v2"] and flows == ["device_code", "auth_code"],
          f"{presets} / {flows}")

    # 手改 config.json 塞进死配置时，载入界面应该自动退回可用项
    dead = load_config()
    dead.auth.client_id = "00000000402b5328"
    dead.auth.endpoint_preset = "v2"
    dead.auth.flow = "auth_code"
    window_dead = MainWindow(dead)
    window_dead.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window_dead.show()
    app.processEvents()
    check("手改过的死配置载入后自动退回 live",
          window_dead.preset_auth_combo.currentData() == "live",
          str(window_dead.preset_auth_combo.currentData()))
    check("手改过的死配置载入后自动退回设备码",
          window_dead.flow_combo.currentData() == "device_code",
          str(window_dead.flow_combo.currentData()))
    window_dead.close()
    window_dead.deleteLater()
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
