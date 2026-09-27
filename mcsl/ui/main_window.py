"""主窗口。"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Slot
from PySide6.QtGui import QCursor, QFont, QGuiApplication
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPushButton, QSpinBox, QStatusBar, QTabWidget,
                               QVBoxLayout, QWidget)

from .. import APP_NAME, APP_VERSION
from ..auth.manager import AccountManager
from ..auth.oauth import DEFAULT_CLIENT_ID
from ..auth.store import dpapi_available
from ..config import CLOSE_ALWAYS, CLOSE_ASK, CLOSE_NEVER, AppConfig, load_config, save_config
from ..launch.launcher import LaunchService
from ..launch.java import (find_java_installations, pick_best, read_install_cached,
                           version_state)
from ..launch.version import list_instances, load_version
from ..paths import data_root, logs_dir
from ..tools import winproc
from ..tools.manager import TOOL_LABELS
from .dialogs import LoginDialog
from .widgets import LogView, PathPicker
from .workers import (GameExitWorker, LaunchWorker, TokenWorker, UiBridge, track_object,
                      track_thread)

log = logging.getLogger(__name__)

CLOSE_CHOICES = [("退出游戏后自动关闭", CLOSE_ALWAYS),
                 ("退出时询问我", CLOSE_ASK),
                 ("保持运行（不关闭）", CLOSE_NEVER)]


class MainWindow(QMainWindow):
    def __init__(self, cfg: AppConfig | None = None) -> None:
        super().__init__()
        self.cfg = cfg or load_config()
        self.accounts = AccountManager(self.cfg.auth)
        self.service: LaunchService | None = None
        self._threads: list[QThread] = []
        self._login_dialog: QDialog | None = None
        self._busy = False
        self._saved_snapshot: dict = {}
        self._centered = False
        # Java 相关缓存：实例声明的 Java 版本、本机扫到的 Java 清单
        self._required_java_cache: dict[tuple[str, str], int | None] = {}
        self._java_installs: list = []
        self._java_scanned = False
        self._java_quickfix_target = None
        # 同一次运行里，用户对某个实例的"版本不匹配也继续"只问一次
        self._java_mismatch_ok: set[str] = set()

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(980, 720)
        # 长生命周期的信号桥：LaunchService 的后台线程会一直用它转发游戏输出，
        # 而 worker 对象早就被销毁了（用 worker 的 signal 会得到
        # "Signal source has been deleted"，进而打死转发线程）。
        self.bridge = UiBridge()
        # 加载守卫：_load_into_ui 会给控件赋值，控件信号不能在这期间回写配置
        self._loading = True
        self._build_ui()
        self.bridge.log_line.connect(self._append_log)
        self.bridge.status.connect(self._on_launch_status)
        self._load_into_ui()
        self._refresh_instances(reload_only=True)
        self._loading = False
        self._refresh_java_hint()
        # 保存按钮只在配置真的变了才可点；改回原样也要重新变灰。
        # 用轮询而不是逐个信号连接：控件太多，漏一个就会出现"改了不变灰"。
        self._mark_saved()
        self._dirty_timer = QTimer(self)
        self._dirty_timer.setInterval(400)
        self._dirty_timer.timeout.connect(self._refresh_save_button)
        self._dirty_timer.start()
        QTimer.singleShot(200, self._warm_token)

    # ================================================================= 界面
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 8)
        root.setSpacing(10)

        root.addWidget(self._build_account_bar())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_paths_tab(), "路径与实例")
        self.tabs.addTab(self._build_launch_tab(), "启动设置")
        self.tabs.addTab(self._build_tools_tab(), "辅助工具")
        self.tabs.addTab(self._build_auth_tab(), "账号与登录")
        root.addWidget(self.tabs, 1)

        # 操作按钮
        buttons = QHBoxLayout()
        self.launch_button = QPushButton("启动游戏")
        self.launch_button.setMinimumHeight(38)
        self.launch_button.setMinimumWidth(160)
        font = self.launch_button.font()
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        self.launch_button.setFont(font)
        self.stop_button = QPushButton("结束游戏")
        self.stop_button.setMinimumHeight(38)
        self.stop_button.setEnabled(False)
        self.save_button = QPushButton("保存配置")
        buttons.addWidget(self.launch_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        root.addLayout(buttons)

        log_box = QGroupBox("运行日志")
        log_layout = QVBoxLayout(log_box)
        self.log_view = LogView()
        log_layout.addWidget(self.log_view)
        hint = QLabel(f"完整日志目录：{logs_dir()}")
        hint.setStyleSheet("color: gray;")
        log_layout.addWidget(hint)
        root.addWidget(log_box, 1)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("就绪")

        self.launch_button.clicked.connect(self._on_launch)
        self.stop_button.clicked.connect(self._on_stop_game)
        self.save_button.clicked.connect(self._on_save_clicked)

    def _build_account_bar(self) -> QWidget:
        box = QGroupBox("当前账号")
        layout = QHBoxLayout(box)
        self.account_label = QLabel("未登录")
        f = QFont()
        f.setBold(True)
        self.account_label.setFont(f)
        self.token_label = QLabel("")
        self.token_label.setStyleSheet("color: gray;")
        self.login_button = QPushButton("登录微软账号")
        self.switch_combo = QComboBox()
        self.switch_combo.setMinimumWidth(200)
        self.switch_combo.setVisible(False)
        self.refresh_button = QPushButton("刷新令牌")

        layout.addWidget(self.account_label)
        layout.addWidget(self.token_label)
        layout.addStretch(1)
        layout.addWidget(self.switch_combo)
        layout.addWidget(self.refresh_button)
        layout.addWidget(self.login_button)

        self.login_button.clicked.connect(self._on_login_clicked)
        self.refresh_button.clicked.connect(self._warm_token)
        self.switch_combo.currentIndexChanged.connect(self._on_account_switch)
        return box

    def _build_paths_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        paths = QGroupBox("四条路径（外加 Java）")
        form = QVBoxLayout(paths)
        self.picker_mc = PathPicker(".minecraft", mode="dir",
                                   placeholder="例如 D:\\Minecraft\\.minecraft")
        self.picker_njb = PathPicker("NinjabrainBot", filter_key="jar",
                                     placeholder="Ninjabrain-Bot-x.y.z.jar")
        self.picker_jingle = PathPicker("Jingle", filter_key="jar",
                                        placeholder="Jingle-x.y.z.jar")
        self.picker_obs = PathPicker("OBS", filter_key="exe",
                                     placeholder="obs64.exe")
        self.picker_java = PathPicker("Java", filter_key="exe",
                                      placeholder="例如 …\\Java\\bin\\java.exe")
        for widget in (self.picker_mc, self.picker_njb, self.picker_jingle,
                       self.picker_obs, self.picker_java):
            form.addWidget(widget)
            widget.changed.connect(self._on_path_changed)

        java_hint_row = QHBoxLayout()
        self.java_hint = QLabel()
        self.java_hint.setWordWrap(True)
        self.java_fix_button = QPushButton("")
        self.java_fix_button.setVisible(False)
        self.java_fix_button.clicked.connect(self._on_java_quickfix)
        java_hint_row.addWidget(self.java_hint, 1)
        java_hint_row.addWidget(self.java_fix_button)
        form.addLayout(java_hint_row)

        pickers = QHBoxLayout()
        self.detect_java_button = QPushButton("选择 Java…（列出本机全部）")
        self.detect_java_button.clicked.connect(self._on_detect_java)
        pickers.addStretch(1)
        pickers.addWidget(self.detect_java_button)
        form.addLayout(pickers)
        layout.addWidget(paths)

        instance_box = QGroupBox("实例")
        inst_layout = QHBoxLayout(instance_box)
        self.instance_combo = QComboBox()
        self.instance_combo.setMinimumWidth(260)
        self.refresh_instances_button = QPushButton("重新扫描")
        self.instance_combo.currentTextChanged.connect(self._on_instance_changed)
        self.refresh_instances_button.clicked.connect(self._refresh_instances)
        inst_layout.addWidget(QLabel("versions 下的实例"))
        inst_layout.addWidget(self.instance_combo)
        inst_layout.addWidget(self.refresh_instances_button)
        inst_layout.addStretch(1)
        layout.addWidget(instance_box)
        layout.addStretch(1)
        return page

    def _build_launch_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        base = QGroupBox("JVM")
        form = QFormLayout(base)
        self.memory_spin = QSpinBox()
        self.memory_spin.setRange(512, 65536)
        self.memory_spin.setSingleStep(512)
        self.memory_spin.setSuffix(" MB")
        self.preset_combo = QComboBox()
        for label, value in (("自定义（不套预设）", "custom"),
                             ("Aikar 基线（G1GC 调参 + 加固）", "aikar"),
                             ("仅加固参数（只加 log4shell / RMI 防护）", "vanilla")):
            self.preset_combo.addItem(label, value)
        self.extra_args_edit = QLineEdit()
        self.extra_args_edit.setPlaceholderText("-XX:+UseG1GC -Dfile.encoding=GB18030 …")
        self.width_spin = QSpinBox()
        self.height_spin = QSpinBox()
        for spin in (self.width_spin, self.height_spin):
            spin.setRange(0, 7680)
        self.width_spin.setSpecialValueText("不指定")
        self.height_spin.setSpecialValueText("不指定")
        res = QHBoxLayout()
        res.addWidget(self.width_spin)
        res.addWidget(QLabel("×"))
        res.addWidget(self.height_spin)
        res.addStretch(1)
        res_widget = QWidget()
        res_widget.setLayout(res)

        form.addRow("最大内存", self.memory_spin)
        form.addRow("参数预设", self.preset_combo)
        form.addRow("追加 JVM 参数", self.extra_args_edit)
        self.jvm_hint = QLabel()
        self.jvm_hint.setWordWrap(True)
        self.jvm_hint.setStyleSheet("color:#8a8a8a;")
        form.addRow("", self.jvm_hint)
        form.addRow("窗口分辨率", res_widget)
        layout.addWidget(base)

        behaviour = QGroupBox("行为")
        bform = QFormLayout(behaviour)
        elevated_note = QLabel("始终以管理员身份启动（关闭 Jingle / OBS 这类提权工具需要它；"
                               "若提权被拒会给出警告）")
        elevated_note.setWordWrap(True)
        self.show_console_check = QCheckBox("显示游戏控制台窗口（调试用；游戏输出始终写入日志文件）")
        self.close_game_check = QCheckBox("关闭启动器时一并结束 Minecraft")
        bform.addRow(elevated_note)
        bform.addRow(self.show_console_check)
        bform.addRow(self.close_game_check)
        layout.addWidget(behaviour)
        layout.addStretch(1)

        self.preset_combo.currentIndexChanged.connect(self._refresh_jvm_hint)
        self.preset_combo.currentIndexChanged.connect(self._on_jvm_edited)
        self.extra_args_edit.textChanged.connect(self._refresh_jvm_hint)
        self.memory_spin.valueChanged.connect(self._on_jvm_edited)
        self.extra_args_edit.textEdited.connect(self._on_jvm_edited)
        return page

    def _build_tools_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.tool_widgets: dict[str, dict] = {}
        for name in ("ninjabrainbot", "jingle", "obs"):
            box = QGroupBox(TOOL_LABELS.get(name, name))
            form = QFormLayout(box)
            enable = QCheckBox("启动游戏时一起启动")
            elevate = QCheckBox("以管理员身份启动")
            close_combo = QComboBox()
            for label, value in CLOSE_CHOICES:
                close_combo.addItem(label, value)
            delay = QSpinBox()
            delay.setRange(0, 30000)
            delay.setSingleStep(100)
            delay.setSuffix(" ms")
            form.addRow(enable)
            form.addRow(elevate)
            form.addRow("游戏退出后", close_combo)
            form.addRow("启动延迟", delay)
            self.tool_widgets[name] = {"enable": enable, "elevate": elevate,
                                       "close": close_combo, "delay": delay}
            layout.addWidget(box)
        layout.addStretch(1)
        return page

    def _build_auth_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        box = QGroupBox("微软登录")
        form = QFormLayout(box)

        how = QLabel(
            "登录方式固定为「设备码」，不需要任何配置：\n"
            "1. 点账号栏里的「重新登录」（首次启动时也会提示登录）\n"
            "2. 弹出的窗口里会显示一个 8 位设备码\n"
            "3. 在浏览器打开 https://www.microsoft.com/link 并输入这个码\n"
            "4. 用微软账号确认登录 —— 成功后启动器自动继续\n\n"
            "登录一次之后，access_token 会自动获取、过期自动续期，不用手动喂令牌。\n"
            "凭据用 Windows DPAPI 加密保存在本机（只有当前 Windows 用户能解开），"
            "不进仓库、不同步到云端。")
        how.setWordWrap(True)
        form.addRow("", how)

        layout.addWidget(box)

        info = QGroupBox("凭据存储")
        ilayout = QVBoxLayout(info)
        self.store_label = QLabel()
        self.store_label.setWordWrap(True)
        ilayout.addWidget(self.store_label)
        layout.addWidget(info)

        buttons = QHBoxLayout()
        self.relogin_button = QPushButton("重新登录当前账号")
        self.logout_button = QPushButton("删除当前账号")
        buttons.addWidget(self.relogin_button)
        buttons.addWidget(self.logout_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addStretch(1)

        self.relogin_button.clicked.connect(self._on_relogin_clicked)
        self.logout_button.clicked.connect(self._on_logout)
        return page

    # ================================================================= 窗口
    def showEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().showEvent(event)
        # 第一次显示时居中：默认 Qt 会把窗口扔在左上角，而各人分辨率/缩放不同，
        # 窗口还可能比可用区域还高（那就有一部分落到屏幕外）。
        # 用 0ms 单次定时器推到"窗口已经真的显示出来、装饰尺寸已知"之后再定位。
        if not self._centered:
            self._centered = True
            QTimer.singleShot(0, self._center_on_screen)

    def _center_on_screen(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()

        # 用 Windows 的真实窗口矩形来居中，不用 Qt 的 frameGeometry()：
        # 后者在 Windows 上不含标题栏（实测差 31px），据此算出来的位置会让窗口偏低。
        rect = winproc.get_window_rect(int(self.winId()))
        if rect is not None:
            left, top, right, bottom = rect
            width, height = right - left, bottom - top
        else:
            frame = self.frameGeometry()
            left, top, width, height = frame.x(), frame.y(), frame.width(), frame.height()

        # max(0, ...) 夹住：窗口比可用区域还高时（布局最小尺寸决定）让它贴顶，
        # 标题栏永远不会被顶到屏幕外面去，只有底部溢出。
        target_x = area.x() + max(0, (area.width() - width) // 2)
        target_y = area.y() + max(0, (area.height() - height) // 2)
        if rect is not None:
            winproc.move_window(int(self.winId()), target_x, target_y)
        else:
            self.move(self.x() + (target_x - left), self.y() + (target_y - top))

        log.info("窗口居中：屏幕 %dx%d@(%d,%d)，窗口 %dx%d，目标 (%d,%d)",
                 area.width(), area.height(), area.x(), area.y(),
                 width, height, target_x, target_y)

    # ================================================================= 载入/保存
    def _load_into_ui(self) -> None:
        previous = self._loading
        self._loading = True
        try:
            self._load_into_ui_inner()
        finally:
            self._loading = previous

    def _load_into_ui_inner(self) -> None:
        cfg = self.cfg
        launch = cfg.launch
        self.picker_mc.set_value(cfg.launch.minecraft_dir)
        self.picker_java.set_value(launch.java_path)
        self.picker_njb.set_value(cfg.tools.get("ninjabrainbot").path if cfg.tools.get("ninjabrainbot") else "")
        self.picker_jingle.set_value(cfg.tools.get("jingle").path if cfg.tools.get("jingle") else "")
        self.picker_obs.set_value(cfg.tools.get("obs").path if cfg.tools.get("obs") else "")

        self.memory_spin.setValue(int(launch.memory_mb or 4096))
        self._select_combo(self.preset_combo, launch.jvm_preset)
        self.extra_args_edit.setText(launch.jvm_args_extra or "")
        self.width_spin.setValue(int(launch.width or 0))
        self.height_spin.setValue(int(launch.height or 0))

        self.elevate_check = None      # 已改为始终提权，界面上不再提供开关
        self.show_console_check.setChecked(cfg.show_console)
        self.close_game_check.setChecked(cfg.close_game_on_launcher_exit)

        for name, widgets in self.tool_widgets.items():
            tool = cfg.tools.get(name)
            if tool is None:
                continue
            widgets["enable"].setChecked(tool.enabled)
            widgets["elevate"].setChecked(tool.elevated)
            self._select_combo(widgets["close"], tool.close_on_exit)
            widgets["delay"].setValue(int(tool.start_delay_ms))

        self._pin_auth_settings()
        # 实例/配置一变，Java 那一行的提示就要跟着重算（否则切实例后还显示上一个实例的判断）
        self._refresh_java_hint()
        self._refresh_jvm_hint()

        storage = ("DPAPI 加密（绑定当前 Windows 用户，拷到别的机器解不开）"
                   if dpapi_available() else "⚠ DPAPI 不可用，凭据以明文保存")
        self.store_label.setText(
            f"账号文件：{data_root() / 'accounts.dat'}\n"
            f"加密方式：{storage}\n"
            f"配置文件：{data_root() / 'config.json'}（本仓库 .gitignore 已排除这些路径）")
        self._refresh_account_bar()

    def _pin_auth_settings(self) -> None:
        """登录方式固定为「live 端点 + 设备码」，配置里残留的旧值一律改回来。

        界面不再暴露 client_id / 端点预设 / 登录方式：公开 id 只有这一条路走得通，
        v2 端点需要自己注册的 Azure 应用，浏览器授权需要自己登记的 localhost 回调。
        留着一堆选不动的选项只会让人困惑（用户明确要求"只用做设备码这一种"）。
        """
        cfg = self.cfg
        if cfg.auth.endpoint_preset != "live" or cfg.auth.flow != "device_code":
            log.info("登录设置固定为 live + 设备码（配置里原来是 %s / %s）",
                     cfg.auth.endpoint_preset, cfg.auth.flow)
        cfg.auth.endpoint_preset = "live"
        cfg.auth.flow = "device_code"
        if not cfg.auth.client_id.strip():
            cfg.auth.client_id = DEFAULT_CLIENT_ID

    def _collect_from_ui(self) -> AppConfig:
        cfg = self.cfg
        cfg.launch.minecraft_dir = self.picker_mc.value()
        for name, picker in (("ninjabrainbot", self.picker_njb), ("jingle", self.picker_jingle),
                             ("obs", self.picker_obs)):
            tool = cfg.tools.get(name)
            if tool is not None:
                tool.path = picker.value()

        # 所有实例共用一套设置（"按实例覆盖"那个功能已按用户要求删除）
        for key, value in {
            "memory_mb": self.memory_spin.value(),
            "jvm_preset": self.preset_combo.currentData(),
            "jvm_args_extra": self.extra_args_edit.text(),
            "width": self.width_spin.value(),
            "height": self.height_spin.value(),
            "java_path": self.picker_java.value(),
        }.items():
            setattr(cfg.launch, key, value)

        cfg.elevate = True             # 始终提权（界面不再提供开关）
        cfg.show_console = self.show_console_check.isChecked()
        cfg.close_game_on_launcher_exit = self.close_game_check.isChecked()

        for name, widgets in self.tool_widgets.items():
            tool = cfg.tools.get(name)
            if tool is None:
                continue
            tool.enabled = widgets["enable"].isChecked()
            tool.elevated = widgets["elevate"].isChecked()
            tool.close_on_exit = widgets["close"].currentData()
            tool.start_delay_ms = widgets["delay"].value()

        # 登录方式固定（界面上没有这些选项了），保存时保持规范值
        cfg.auth.endpoint_preset = "live"
        cfg.auth.flow = "device_code"
        if not cfg.auth.client_id.strip():
            cfg.auth.client_id = DEFAULT_CLIENT_ID
        return cfg

    def _on_save_clicked(self) -> None:
        try:
            path = save_config(self._collect_from_ui())
            self._mark_saved()
            self._append_log(f"配置已保存到 {path}")
            self.status.showMessage("配置已保存", 3000)
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))

    # ------------------------------------------------------------ 配置脏检测
    def _mark_saved(self) -> None:
        """把当前控件状态记为"已保存"基准，并刷新保存按钮。"""
        self._saved_snapshot = self._collect_from_ui().to_dict()
        self._refresh_save_button()

    def _is_dirty(self) -> bool:
        try:
            return self._collect_from_ui().to_dict() != self._saved_snapshot
        except Exception:  # noqa: BLE001 - 控件状态异常时不要把按钮卡住
            log.debug("脏检测失败", exc_info=True)
            return True

    def _refresh_save_button(self) -> None:
        dirty = self._is_dirty()
        if self.save_button.isEnabled() != dirty:
            self.save_button.setEnabled(dirty)
        self.save_button.setToolTip("有未保存的修改" if dirty else "配置与已保存的一致")

    # ================================================================= 交互
    def _on_path_changed(self) -> None:
        if self._loading:
            return
        cfg = self._collect_from_ui()
        if Path(cfg.launch.minecraft_dir).is_dir():
            self._refresh_instances(reload_only=True)
        self._refresh_java_hint()

    def _select_combo(self, combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _on_instance_changed(self, name: str) -> None:
        if self._loading or not name:
            return
        self.cfg.launch.instance = name
        self._load_into_ui()
        self.status.showMessage(f"已切换到实例 {name}", 3000)

    def _on_detect_java(self) -> None:
        """打开 Java 选择对话框（不再"自动挑第一个"）。"""
        from .dialogs import JavaPickerDialog

        required = self._required_java_major()
        dialog = JavaPickerDialog(instance=self.cfg.launch.instance, required_major=required,
                                  current_path=self.picker_java.value(), parent=self)
        track_object(dialog)
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected is not None
        finally:
            dialog.deleteLater()
        # 对话框重新扫过一遍，把结果留给提示行复用
        self._java_installs = list(getattr(dialog, "_installs", []) or [])
        self._java_scanned = True
        if not accepted:
            self._refresh_java_hint()
            return
        install = dialog.selected
        self._apply_java_choice(install)
        self._append_log(f"已选择 Java {install.major}（{install.version or '版本未知'}）"
                         f"：{install.path}")
        self.status.showMessage(f"Java 已设为 {install.version_label}", 4000)
        self._refresh_java_hint()

    def _apply_java_choice(self, install) -> None:
        """把选中的 Java 写进界面（Java 路径是全局设置，与 _collect_from_ui 一致）。"""
        self.picker_java.set_value(str(install.path))

    def _on_java_quickfix(self) -> None:
        """提示行里的"换成匹配版本"按钮。"""
        target = getattr(self, "_java_quickfix_target", None)
        if target is None:
            return
        self._apply_java_choice(target)
        self._append_log(f"已改用 Java {target.major}（{target.version or '版本未知'}）"
                         f"：{target.path}")
        self.status.showMessage(f"已换成 {target.version_label}", 4000)
        self._refresh_java_hint()

    def _required_java_major(self) -> int | None:
        """当前实例声明的 Java 大版本；读不到（缺文件/JSON 坏了）返回 None。"""
        cfg = self.cfg
        key = (cfg.launch.minecraft_dir, cfg.launch.instance)
        if key in self._required_java_cache:
            return self._required_java_cache[key]
        major: int | None = None
        try:
            major = load_version(Path(cfg.launch.minecraft_dir), cfg.launch.instance).java_major or None
        except Exception as exc:  # noqa: BLE001 - 版本 JSON 缺失/损坏都不该拦住界面
            log.debug("读实例 %s 的 Java 要求失败：%s", cfg.launch.instance, exc)
        self._required_java_cache[key] = major
        return major

    @Slot()
    def _refresh_java_hint(self) -> None:
        """Java 那一行下面的状态提示。

        规则（踩过坑所以写成这样）：
          - 比声明版本**低** → 红字 + 一键替换（JVM 参数/加载器都可能要求更高的 Java）
          - 比声明版本**高** → 只给一句灰色说明，不报警、不给替换按钮。
            因为这里的"要求"只是游戏版本 JSON 的声明，装了模组、换了加载器或用了
            新 JVM 参数之后，实际可用的版本和声明经常不一致（本机 1.16.1 就是用
            Java 25 跑的，声明写的是 8）。
        """
        required = self._required_java_major()
        current = self.picker_java.value()
        self._java_quickfix_target = None
        self.java_fix_button.setVisible(False)

        def paint(text: str, color: str) -> None:
            self.java_hint.setText(text)
            self.java_hint.setStyleSheet(f"color:{color};")

        if not current:
            if required:
                paint(f"⚠ 还没指定 Java：实例 {self.cfg.launch.instance} 的版本 JSON 声明需要 "
                      f"Java {required}，点下面的按钮挑一个。", "#c62828")
            else:
                paint("还没有指定 Java。", "#8a8a8a")
            return

        install = read_install_cached(current)
        state = version_state(install, required)

        if state == "unknown":
            paint("✗ 这个路径读不出 Java 版本，可能不是 java.exe（或文件已不存在）。", "#c62828")
            return
        if state == "info":
            paint(f"✓ 当前 Java {install.major}（{install.version or '未知版本'}，"
                  f"{install.vendor_label}）——本实例的版本 JSON 没有声明 Java 要求。", "#2e7d32")
            return
        if state == "match":
            paint(f"✓ 当前 Java {install.major}（{install.version or '未知版本'}，"
                  f"{install.vendor_label}）与实例 {self.cfg.launch.instance} 的声明相符。", "#2e7d32")
            return
        if state == "new":
            # 偏高：不报警。这里多半是因为模组/JVM 参数把实际要求抬上去了。
            paint(f"● 实例 {self.cfg.launch.instance} 的版本 JSON 声明需要 Java {required}，"
                  f"当前是 Java {install.major}（{install.version or '未知版本'}）。"
                  f"声明的只是游戏发布时的信息，装了模组/加载器或改了 JVM 参数后实际要求会变，"
                  f"能正常启动就没问题。", "#8a8a8a")
            return

        # state == "old"：真的偏低
        paint(f"✗ 实例 {self.cfg.launch.instance} 声明需要 Java {required}，"
              f"当前只有 Java {install.major}（{install.version or '未知版本'}），启动大概率失败。",
              "#c62828")
        if not self._java_scanned:
            self._scan_java_quietly()
        best = pick_best(self._java_installs, required)
        if best is not None and best.major > install.major:
            self._java_quickfix_target = best
            self.java_fix_button.setText(f"换成 Java {best.major}（{best.vendor_label}）")
            self.java_fix_button.setVisible(True)

    def _scan_java_quietly(self) -> None:
        """静默扫一遍本机 Java（0.2 秒量级），结果缓存给提示行和快速修复用。"""
        try:
            self._java_installs = find_java_installations([self.picker_java.value()])
        except Exception as exc:  # noqa: BLE001
            log.debug("扫描 Java 失败：%s", exc)
            self._java_installs = []
        self._java_scanned = True

    @Slot()
    def _refresh_jvm_hint(self) -> None:
        """把「参数预设」和「追加 JVM 参数」的关系写在界面上。

        实际行为（mcsl/launch/command.py 的 build_command）：预设先贡献它自己的一套参数，
        然后**无论选哪个预设**都会把"追加 JVM 参数"那行拼到后面。所以不能把那一行改名成
        "自定义参数" —— 那样会谎报（非自定义时它也生效）。这里把叠加关系说清楚。
        """
        from ..launch.command import preset_flags, split_args

        preset = self.preset_combo.currentData() or "custom"
        count = len(preset_flags(preset))
        extra_count = len(split_args(self.extra_args_edit.text()))
        if preset == "custom":
            what = "「自定义」不套任何预设参数"
        elif preset == "aikar":
            what = f"「Aikar 基线」会先加上 Aikar 的 G1GC 调参和 log4shell / RMI 加固（{count} 个）"
        elif preset == "vanilla":
            what = (f"「仅加固参数」只会加上 log4shell / RMI 防护（{count} 个），"
                    f"不碰 GC 相关的调参")
        else:
            what = f"预设 {preset} 会加上 {count} 个参数"
        self.jvm_hint.setText(
            f"{what}；下面那行「追加 JVM 参数」无论选哪个预设都会追加到最后"
            f"（当前 {extra_count} 个参数）。"
            + self._jvm_overlap_warning(preset))

    def _jvm_overlap_warning(self, preset: str) -> str:
        """预设和追加参数撞了同一批 JVM 选项时提醒一句。

        两套参数都带 `-XX:G1NewSizePercent=…` 这种同名前缀时，后出现的那个生效 ——
        这是 JVM 的行为，不是 bug，但用户看到"我设了却没效果"会一头雾水。
        """
        from ..launch.command import preset_flags, split_args

        if preset == "custom":
            return ""
        preset_keys = {flag.split("=")[0].lower() for flag in preset_flags(preset)}
        clashes = [flag for flag in split_args(self.extra_args_edit.text())
                   if flag.split("=")[0].lower() in preset_keys]
        if not clashes:
            return ""
        return f" ⚠ 其中 {len(clashes)} 个与预设重复（同一选项后面的会覆盖前面的）：" \
               + " ".join(clashes[:3]) + ("…" if len(clashes) > 3 else "")

    def _on_jvm_edited(self, *_args) -> None:
        if self._loading:
            return
        self.status.showMessage("设置已修改，点“保存配置”生效或直接启动（会自动保存）", 4000)

    def _refresh_instances(self, reload_only: bool = False) -> None:
        cfg = self.cfg if (reload_only or self._loading) else self._collect_from_ui()
        mc_dir = Path(cfg.launch.minecraft_dir) if cfg.launch.minecraft_dir else None
        instances = list_instances(mc_dir) if mc_dir and mc_dir.is_dir() else []
        current = cfg.launch.instance
        self.instance_combo.blockSignals(True)
        self.instance_combo.clear()
        self.instance_combo.addItems(instances)
        if current in instances:
            self.instance_combo.setCurrentText(current)
        elif instances:
            self.instance_combo.setCurrentIndex(0)
        self.instance_combo.blockSignals(False)
        if reload_only and mc_dir and not mc_dir.is_dir():
            self._append_log(f"⚠ .minecraft 目录不存在或还不是目录：{mc_dir}")

    # ================================================================= 账号
    def _refresh_account_bar(self) -> None:
        accounts = self.accounts.accounts
        selected = self.accounts.selected
        if selected is None:
            self.account_label.setText("未登录")
            self.token_label.setText("点右侧按钮登录微软账号（只需登录一次）")
            self.switch_combo.setVisible(False)
            self.login_button.setText("登录微软账号")
            self.refresh_button.setEnabled(False)
            return
        self.account_label.setText(f"{selected.name}（uuid {selected.uuid[:8]}…）")
        self.login_button.setText("重新登录")
        self.refresh_button.setEnabled(True)
        self.switch_combo.blockSignals(True)
        self.switch_combo.clear()
        for acc in accounts:
            self.switch_combo.addItem(f"{acc.name}" + ("" if acc.uuid == selected.uuid else "（切换）"),
                                      acc.uuid)
        index = self.switch_combo.findData(selected.uuid)
        if index >= 0:
            self.switch_combo.setCurrentIndex(index)
        self.switch_combo.blockSignals(False)
        self.switch_combo.setVisible(len(accounts) > 1)
        session = selected.session or {}
        if session.get("expires_at"):
            import time
            remaining = max(0, int(session["expires_at"] - time.time()))
            self.token_label.setText(f"缓存令牌剩余 {remaining // 3600} 小时 {remaining % 3600 // 60} 分"
                                     "（过期会自动续期）")
        else:
            self.token_label.setText("尚未缓存令牌，启动时会自动获取")

    def _on_account_switch(self, _index: int) -> None:
        uuid = self.switch_combo.currentData()
        if uuid:
            self.accounts.select(uuid)
            self._refresh_account_bar()

    def _on_login_clicked(self, force: bool = False) -> None:
        if self._login_dialog is not None:
            return
        self._collect_from_ui()
        try:
            save_config(self.cfg)
        except OSError:
            pass
        self.accounts.cfg = self.cfg.auth

        dialog = LoginDialog(self.cfg, self.accounts, self)
        dialog.succeeded.connect(self._on_login_succeeded)
        self._login_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._login_dialog = None

    @Slot(str, str)
    def _on_login_succeeded(self, name: str, uuid: str) -> None:
        # 逐步写日志：这条路径曾经在真实登录后静默死亡，日志断点就是定位线索
        log.info("界面收到登录成功：%s（uuid %s）", name, uuid[:8])
        try:
            self._append_log(f"登录成功：{name}（uuid {uuid[:8]}…）")
            log.debug("已写入日志窗格")
            self._refresh_account_bar()
            log.info("账号栏刷新完成")
        except Exception:  # noqa: BLE001
            log.exception("登录成功后的界面刷新失败")
        # 刻意**不**在这里预热令牌：登录流程刚刚做完完整认证链，令牌是新的；
        # 立刻在另一个线程里再碰同一个 AccountManager/requests.Session 属于
        # 共享对象的并发使用，没必要冒这个险。

    @Slot()
    def _on_relogin_clicked(self) -> None:
        self._on_login_clicked(force=True)

    def _on_logout(self) -> None:
        selected = self.accounts.selected
        if selected is None:
            return
        if QMessageBox.question(self, "删除账号",
                                f"确定要删除账号 {selected.name} 吗？\n"
                                "本机保存的登录凭据会被抹掉，下次需要重新登录微软账号。") \
                != QMessageBox.StandardButton.Yes:
            return
        self.accounts.logout(selected.uuid)
        self._refresh_account_bar()
        self._append_log(f"已删除账号 {selected.name}")

    def _warm_token(self) -> None:
        if self.accounts.selected is None:
            log.debug("没有账号，跳过令牌预热")
            self._refresh_account_bar()
            return
        log.info("开始预热令牌（账号 %s）", self.accounts.selected.name)
        worker = TokenWorker(self.accounts)
        worker.status.connect(self._on_token_status)
        worker.success.connect(self._on_token_success)
        worker.failed.connect(self._on_token_failed)
        self._start_worker(worker)

    @Slot(str)
    def _on_token_status(self, message: str) -> None:
        log.debug("令牌预热：%s", message)
        self.status.showMessage(message, 4000)

    @Slot(str, str, int)
    def _on_token_success(self, name: str, uuid: str, remaining: int) -> None:
        log.info("令牌预热完成：%s（剩余 %d 秒）", name, remaining)
        try:
            self._refresh_account_bar()
        except Exception:  # noqa: BLE001
            log.exception("刷新账号栏失败")
        self.status.showMessage(f"令牌就绪：{name}（剩余 {remaining // 3600} 小时）", 6000)

    @Slot(str)
    def _on_token_failed(self, message: str) -> None:
        log.warning("令牌预热失败：%s", message)
        self._append_log(f"⚠ 令牌刷新失败：{message}")
        self.status.showMessage("令牌刷新失败", 6000)

    # ================================================================= 启动
    def _on_launch(self) -> None:
        if self._busy:
            return
        self._collect_from_ui()
        try:
            save_config(self.cfg)
        except OSError as exc:
            self._append_log(f"⚠ 配置保存失败（仍会继续）：{exc}")
        self._mark_saved()
        if self.accounts.selected is None:
            if QMessageBox.question(self, "还没有登录",
                                    "还没有登录微软账号，无法获取 access_token。现在去登录吗？") \
                    == QMessageBox.StandardButton.Yes:
                self._on_login_clicked()
            return

        if not self._confirm_java_version():
            return

        self.accounts.cfg = self.cfg.auth
        self._set_busy(True, "正在准备启动…")
        worker = LaunchWorker(self.cfg, self.accounts,
                              on_log=self.bridge.log_line.emit,
                              on_status=self.bridge.status.emit)
        worker.failed.connect(self._on_launch_failed)
        worker.service_ready.connect(self._on_service_ready)
        worker.game_exited.connect(self._on_game_exited)
        worker.finished.connect(self._on_launch_finished)
        self._start_worker(worker)

    def _confirm_java_version(self) -> bool:
        """启动前核对 Java 版本：不匹配就问一次（同一次运行里每个实例只问一次）。

        为什么要拦一下：Java 版本不对时游戏挂得很惨（1.16.1 用 Java 25 会在很早就崩，
        报错信息还看不懂），而这里我们已经知道实例声明需要哪个版本了。
        """
        required = self._required_java_major()
        current = self.picker_java.value()
        if not required or not current:
            return True
        if os.environ.get("MCSL_SKIP_JAVA_CHECK") == "1":
            # 测试/无人值守用逃生口：否则探针会被这个模态框挂住
            log.debug("MCSL_SKIP_JAVA_CHECK=1，跳过 Java 版本核对")
            return True
        install = read_install_cached(current)
        # 只在"确实偏低"时拦。比声明高不拦：声明只是游戏发布时的信息，
        # 装了模组/换了加载器/改了 JVM 参数之后实际要求常常更高
        # （本机 1.16.1 声明 Java 8，实际用 Java 25 跑）。
        if version_state(install, required) != "old":
            return True
        instance = self.cfg.launch.instance
        if instance in self._java_mismatch_ok:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("Java 版本对不上")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"实例 {instance} 的版本 JSON 声明需要 Java {required}，"
                    f"而当前配置的是 Java {install.major}"
                    f"（{install.version or '版本未知'}），比声明版本低。")
        box.setInformativeText(
            f"路径：{install.path}\n\n继续启动很可能失败（报错信息通常也看不出是这个原因）。\n"
            "建议点“取消”，再用“选择 Java…”换一个更新的版本。")
        go = box.addButton("仍然启动", QMessageBox.ButtonRole.DestructiveRole)
        fix = box.addButton("去换 Java…", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(fix)
        box.exec()
        clicked = box.clickedButton()
        if clicked is go:
            self._java_mismatch_ok.add(instance)
            self._append_log(f"⚠ 已按用户选择用 Java {install.major} 启动需要 Java {required} 的实例")
            return True
        if clicked is fix:
            self._on_detect_java()
        return False

    @Slot(str)
    def _on_launch_status(self, message: str) -> None:
        self.status.showMessage(message)

    @Slot()
    def _on_launch_finished(self) -> None:
        self._set_busy(False)

    def _on_service_ready(self, service: LaunchService) -> None:
        self.service = service
        self.stop_button.setEnabled(True)
        self.launch_button.setEnabled(False)

    def _on_launch_failed(self, message: str) -> None:
        self._append_log("✘ 启动失败：" + message)
        QMessageBox.critical(self, "启动失败", message)
        self._set_busy(False)

    def _on_stop_game(self) -> None:
        if self.service is not None:
            self.service.stop_game()
        self.stop_button.setEnabled(False)

    def _on_game_exited(self, code: int, duration: float) -> None:
        self._append_log(f"Minecraft 已退出（退出码 {code}，运行 {duration:.0f} 秒）")
        self.status.showMessage("游戏已退出", 5000)
        self.stop_button.setEnabled(False)
        self.launch_button.setEnabled(True)
        service = self.service
        if service is None:
            return
        answers = self._ask_close_tools()
        if answers is None:      # 用户全部选择保留
            self._append_log("按你的选择保留全部辅助工具")
            self._set_busy(False)
            return
        self._set_busy(True, "正在清理辅助工具…")
        worker = GameExitWorker(service, ask_handler=lambda name: answers.get(name, True),
                               on_log=self.bridge.log_line.emit,
                               on_status=self.bridge.status.emit)
        worker.reports_ready.connect(self._on_reports_ready)
        worker.finished.connect(self._on_launch_finished)
        self._start_worker(worker)

    def _ask_close_tools(self, only_running: bool = True) -> dict[str, bool] | None:
        """在游戏退出后询问（只对配置为"询问"的工具弹窗）。

        only_running=True 时跳过本来就没在跑的工具，避免问一些无意义的问题。
        """
        answers: dict[str, bool] = {}
        for name in ("jingle", "ninjabrainbot", "obs"):
            tool = self.cfg.tools.get(name)
            if tool is None or tool.close_on_exit != CLOSE_ASK:
                continue
            if only_running and self.service is not None \
                    and not self.service.tools.is_tool_running(name):
                log.debug("%s 未在运行，跳过询问", name)
                continue
            label = TOOL_LABELS.get(name, name)
            box = QMessageBox(self)
            box.setWindowTitle("清理确认")
            box.setText(f"游戏已退出。要关闭 {label} 吗？")
            box.setInformativeText(f"（{label} 的退出策略设为“退出时询问”，可在“辅助工具”页改成固定行为）")
            close_btn = box.addButton(f"关闭 {label}", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(f"保留 {label}", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(close_btn)
            box.exec()
            answers[name] = box.clickedButton() is close_btn
        return answers

    def _on_reports_ready(self, reports: list) -> None:
        failed = [r for r in reports if not r.ok]
        if failed:
            details = "\n".join(f"· {r.summary()}" for r in failed)
            self.status.showMessage("部分辅助工具关闭失败", 8000)
            QMessageBox.warning(self, "有工具没关掉",
                                "以下工具关闭失败，日志里有详细诊断：\n" + details)
        else:
            self.status.showMessage("辅助工具已全部处理", 5000)
        self.service = None if (self.service and not self.service.game_running) else self.service

    # ================================================================= 杂项
    def _append_log(self, text: str) -> None:
        from ..logging_setup import scrub

        self.log_view.append(scrub(text))

    def _set_busy(self, busy: bool, message: str | None = None) -> None:
        self._busy = busy
        running = self.service is not None and self.service.game_running
        self.launch_button.setEnabled(not busy and not running)
        self.stop_button.setEnabled(running)
        self.setCursor(Qt.CursorShape.BusyCursor if busy else Qt.CursorShape.ArrowCursor)
        if message:
            self.status.showMessage(message)

    def _start_worker(self, worker) -> QThread:
        # 线程不挂父对象 + 登记在 _ACTIVE_THREADS：避免"线程还在跑，对象先被销毁"
        # worker 也必须钉住：纯 Python 创建的 QObject 归 Python 所有，引用一断
        # PySide6 就会删掉 C++ 对象，而它可能还在工作线程里等着 deleteLater。
        worker = track_object(worker)
        thread = track_thread(QThread())
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        self._threads.append(thread)
        thread.start()
        log.debug("已启动后台线程（%s）", type(worker).__name__)
        return thread

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        try:
            self._collect_from_ui()
            save_config(self.cfg)
        except OSError:
            pass

        service = self.service
        if service is not None:
            if service.game_running:
                # 游戏还在跑：不动任何辅助工具，免得关个启动器窗口就把 Jingle 关掉
                log.info("游戏仍在运行，退出启动器时保留全部辅助工具")
                if self.cfg.close_game_on_launcher_exit:
                    service.stop_game()
            else:
                # 游戏已退出：该问的必须问（以前这里静默跳过 ask 策略，
                # 用户看到的现象就是"OBS 关不掉"）
                answers = self._ask_close_tools(only_running=True)
                reports = self._close_tools_by_plan(service, answers)
                for report in reports:
                    log.info("退出清理：%s", report.summary())

        for thread in list(self._threads):
            thread.quit()
            if not thread.wait(5000):
                # 不能在这里销毁它：_ACTIVE_THREADS 继续持有，等它自己结束
                log.warning("后台线程未在 5 秒内结束，保留引用避免 Qt 销毁运行中的线程")
        super().closeEvent(event)

    def _close_tools_by_plan(self, service: LaunchService, answers: dict[str, bool]) -> list:
        """按配置与实际运行状态决定关谁：always 直接关，ask 看用户回答。"""
        plan: dict[str, str] = {}
        for name in ("ninjabrainbot", "jingle", "obs"):
            tool = self.cfg.tools.get(name)
            if tool is None or not service.tools.is_tool_running(name):
                continue
            if tool.close_on_exit == CLOSE_ALWAYS:
                plan[name] = CLOSE_ALWAYS
            elif tool.close_on_exit == CLOSE_ASK and answers.get(name):
                plan[name] = CLOSE_ALWAYS
        if not plan:
            return []
        # 关工具要等它自己退出，可能好几秒。先说清楚在干什么，别让人以为界面卡死了。
        self.status.showMessage("正在关闭辅助工具，请稍候…")
        QApplication.processEvents()
        return service.tools.stop_all(plan, allow_elevate=not self.cfg.elevate)
