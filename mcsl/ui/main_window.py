"""主窗口。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QPushButton, QScrollArea, QSizePolicy, QSpinBox,
                               QStatusBar, QTabWidget, QVBoxLayout, QWidget)

from .. import APP_NAME, APP_VERSION
from ..auth.manager import AccountManager
from ..auth.store import dpapi_available
from ..config import CLOSE_ALWAYS, CLOSE_ASK, CLOSE_NEVER, AppConfig, load_config, save_config
from ..launch.launcher import LaunchService
from ..launch.version import list_instances
from ..paths import data_root, logs_dir
from ..tools import winproc
from ..tools.manager import TOOL_LABELS
from .widgets import LogView, PathPicker
from .workers import GameExitWorker, LaunchWorker, LoginWorker, TokenWorker

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

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(980, 720)
        self._build_ui()
        self._load_into_ui()
        self._refresh_instances(reload_only=True)
        QTimer.singleShot(200, self._warm_token)

    # ================================================================= 界面
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 8)
        root.setSpacing(10)

        root.addWidget(self._build_account_bar())

        tabs = QTabWidget()
        tabs.addTab(self._build_paths_tab(), "路径与实例")
        tabs.addTab(self._build_launch_tab(), "启动设置")
        tabs.addTab(self._build_tools_tab(), "辅助工具")
        tabs.addTab(self._build_auth_tab(), "账号与登录")
        root.addWidget(tabs, 1)

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
                                      placeholder="…\\zulu-25\\bin\\java.exe")
        for widget in (self.picker_mc, self.picker_njb, self.picker_jingle,
                       self.picker_obs, self.picker_java):
            form.addWidget(widget)
            widget.changed.connect(self._on_path_changed)

        pickers = QHBoxLayout()
        self.detect_java_button = QPushButton("自动检测本机 Java")
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
        for label, value in (("自定义（沿用我的调参）", "custom"),
                             ("Aikar 基线", "aikar"),
                             ("仅加固参数", "vanilla")):
            self.preset_combo.addItem(label, value)
        self.extra_args_edit = QLineEdit()
        self.extra_args_edit.setPlaceholderText("-XX:+UseG1GC -Dfile.encoding=GB18030 …")
        self.per_instance_check = QCheckBox("上面的内存/JVM 参数只对当前实例生效")
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
        form.addRow("", self.per_instance_check)
        form.addRow("窗口分辨率", res_widget)
        layout.addWidget(base)

        behaviour = QGroupBox("行为")
        bform = QFormLayout(behaviour)
        self.elevate_check = QCheckBox("以管理员身份运行启动器（推荐：否则关不掉 Jingle 这类提权工具）")
        self.show_console_check = QCheckBox("显示游戏控制台窗口（调试用）")
        self.close_game_check = QCheckBox("关闭启动器时一并结束 Minecraft")
        bform.addRow(self.elevate_check)
        bform.addRow(self.show_console_check)
        bform.addRow(self.close_game_check)
        layout.addWidget(behaviour)
        layout.addStretch(1)

        self.preset_combo.currentIndexChanged.connect(self._on_jvm_edited)
        self.memory_spin.valueChanged.connect(self._on_jvm_edited)
        self.extra_args_edit.textEdited.connect(self._on_jvm_edited)
        self.per_instance_check.toggled.connect(self._on_per_instance_toggled)
        return page

    def _build_tools_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.tool_widgets: dict[str, dict] = {}
        for name in ("ninjabrainbot", "jingle", "obs"):
            box = QGroupBox(TOOL_LABELS.get(name, name))
            form = QFormLayout(box)
            enable = QCheckBox("启动游戏时一起启动")
            elevate = QCheckBox("以管理员身份启动（Jingle 需要）")
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

        self.client_id_edit = QLineEdit()
        self.client_id_edit.setPlaceholderText("Azure 应用的 Application (client) ID")
        self.preset_auth_combo = QComboBox()
        self.preset_auth_combo.addItem("live（login.live.com，配合官方公开 id）", "live")
        self.preset_auth_combo.addItem("v2（microsoftonline，配合自己注册的应用）", "v2")
        self.flow_combo = QComboBox()
        self.flow_combo.addItem("设备码（显示一个代码，去网页输入）", "device_code")
        self.flow_combo.addItem("浏览器授权（自动打开浏览器回调）", "auth_code")

        form.addRow("client_id", self.client_id_edit)
        form.addRow("端点预设", self.preset_auth_combo)
        form.addRow("登录方式", self.flow_combo)

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

        self.relogin_button.clicked.connect(lambda: self._on_login_clicked(force=True))
        self.logout_button.clicked.connect(self._on_logout)
        self.auth_changed_widgets = [self.client_id_edit, self.preset_auth_combo, self.flow_combo]
        for widget in self.auth_changed_widgets:
            signal = getattr(widget, "textEdited", None) or widget.currentIndexChanged
            signal.connect(self._on_auth_edited)
        return page

    # ================================================================= 载入/保存
    def _load_into_ui(self) -> None:
        cfg = self.cfg
        launch = cfg.launch.effective(cfg.launch.instance)
        self.picker_mc.set_value(cfg.launch.minecraft_dir)
        self.picker_java.set_value(launch.java_path)
        self.picker_njb.set_value(cfg.tools.get("ninjabrainbot").path if cfg.tools.get("ninjabrainbot") else "")
        self.picker_jingle.set_value(cfg.tools.get("jingle").path if cfg.tools.get("jingle") else "")
        self.picker_obs.set_value(cfg.tools.get("obs").path if cfg.tools.get("obs") else "")

        self.memory_spin.setValue(int(launch.memory_mb or 4096))
        self._select_combo(self.preset_combo, launch.jvm_preset)
        self.extra_args_edit.setText(launch.jvm_args_extra or "")
        self.per_instance_check.setChecked(cfg.launch.instance in cfg.launch.instances)
        self.width_spin.setValue(int(launch.width or 0))
        self.height_spin.setValue(int(launch.height or 0))

        self.elevate_check.setChecked(cfg.elevate)
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

        self.client_id_edit.setText(cfg.auth.client_id)
        self._select_combo(self.preset_auth_combo, cfg.auth.endpoint_preset)
        self._select_combo(self.flow_combo, cfg.auth.flow)

        storage = ("DPAPI 加密（绑定当前 Windows 用户，拷到别的机器解不开）"
                   if dpapi_available() else "⚠ DPAPI 不可用，凭据以明文保存")
        self.store_label.setText(
            f"账号文件：{data_root() / 'accounts.dat'}\n"
            f"加密方式：{storage}\n"
            f"配置文件：{data_root() / 'config.json'}（本仓库 .gitignore 已排除这些路径）")
        self._refresh_account_bar()

    def _collect_from_ui(self) -> AppConfig:
        cfg = self.cfg
        cfg.launch.minecraft_dir = self.picker_mc.value()
        cfg.launch.java_path = self.picker_java.value() if not self.per_instance_check.isChecked() \
            else cfg.launch.java_path
        for name, picker in (("ninjabrainbot", self.picker_njb), ("jingle", self.picker_jingle),
                             ("obs", self.picker_obs)):
            tool = cfg.tools.get(name)
            if tool is not None:
                tool.path = picker.value()

        target_launch = cfg.launch.effective(cfg.launch.instance)
        values = {
            "memory_mb": self.memory_spin.value(),
            "jvm_preset": self.preset_combo.currentData(),
            "jvm_args_extra": self.extra_args_edit.text(),
            "width": self.width_spin.value(),
            "height": self.height_spin.value(),
        }
        if self.per_instance_check.isChecked():
            from ..config import InstanceOverride

            override = cfg.launch.instances.get(cfg.launch.instance) or InstanceOverride()
            for key, value in values.items():
                setattr(override, key, value)
            cfg.launch.instances[cfg.launch.instance] = override
        else:
            for key, value in values.items():
                setattr(cfg.launch, key, value)
            cfg.launch.instances.pop(cfg.launch.instance, None)
        if target_launch.java_path and not cfg.launch.java_path:
            cfg.launch.java_path = target_launch.java_path

        cfg.elevate = self.elevate_check.isChecked()
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

        cfg.auth.client_id = self.client_id_edit.text().strip()
        cfg.auth.endpoint_preset = self.preset_auth_combo.currentData()
        cfg.auth.flow = self.flow_combo.currentData()
        return cfg

    def _on_save_clicked(self) -> None:
        try:
            path = save_config(self._collect_from_ui())
            self._append_log(f"配置已保存到 {path}")
            self.status.showMessage("配置已保存", 3000)
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))

    # ================================================================= 交互
    def _on_path_changed(self) -> None:
        cfg = self._collect_from_ui()
        if Path(cfg.launch.minecraft_dir).is_dir():
            self._refresh_instances(reload_only=True)

    def _select_combo(self, combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _on_instance_changed(self, name: str) -> None:
        if not name:
            return
        self.cfg.launch.instance = name
        self._load_into_ui()
        self.status.showMessage(f"已切换到实例 {name}", 3000)

    def _on_detect_java(self) -> None:
        candidates: list[Path] = []
        for root in (Path("C:/Program Files/Zulu"), Path("C:/Program Files/Java"),
                     Path("C:/Program Files/Eclipse Adoptium"),
                     Path("C:/Program Files/Microsoft")):
            if root.is_dir():
                for child in root.iterdir():
                    exe = child / "bin" / "java.exe"
                    if exe.is_file():
                        candidates.append(exe)
        for extra in (Path(self.picker_java.value()), Path(sys.executable).parent / "java.exe"):
            if extra.is_file() and extra not in candidates:
                candidates.append(extra)
        if not candidates:
            QMessageBox.information(self, "没找到 Java", "没有在常见安装目录里找到 java.exe，请手动选择。")
            return
        # 选版本号最大的（目录名大致按版本排序）
        best = sorted(candidates, key=lambda p: p.parent.parent.name)[-1]
        self.picker_java.set_value(str(best))
        self._append_log(f"已检测到 Java：{best}")

    def _on_jvm_edited(self, *_args) -> None:
        if self.per_instance_check.isChecked():
            self.per_instance_check.setToolTip("当前实例已有独立设置")
        self.status.showMessage("设置已修改，点“保存配置”生效或直接启动（会自动保存）", 4000)

    def _on_per_instance_toggled(self, checked: bool) -> None:
        if checked:
            self.status.showMessage("内存/JVM 参数将只对当前实例生效", 4000)
        else:
            self.status.showMessage("内存/JVM 参数改回全局生效（当前实例的独立设置会被清除）", 4000)

    def _on_auth_edited(self, *_args) -> None:
        self.status.showMessage("登录相关设置已修改，点“保存配置”后生效", 4000)

    def _refresh_instances(self, reload_only: bool = False) -> None:
        cfg = self._collect_from_ui() if not reload_only else self.cfg
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

        dialog = QDialog(self)
        dialog.setWindowTitle("登录微软账号")
        dialog.setMinimumWidth(460)
        layout = QVBoxLayout(dialog)
        head = QLabel("正在向微软请求授权…")
        head.setWordWrap(True)
        layout.addWidget(head)
        code_label = QLabel("")
        code_font = QFont("Consolas")
        code_font.setPointSize(22)
        code_font.setBold(True)
        code_label.setFont(code_font)
        code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(code_label)
        detail = QLabel("")
        detail.setWordWrap(True)
        detail.setOpenExternalLinks(True)
        layout.addWidget(detail)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        worker = LoginWorker(self.cfg, self.accounts, self.cfg.auth.flow, self.cfg.auth.client_id)
        worker.status.connect(lambda msg: (head.setText(msg), self._append_log(msg)))
        worker.device_code.connect(
            lambda code, uri, expires: (code_label.setText(code),
                                        detail.setText(f"在浏览器打开 <a href='{uri}'>{uri}</a> 并输入上面的代码"
                                                       f"（{expires // 60} 分钟内有效）")))
        worker.failed.connect(lambda msg: (dialog.reject(),
                                           QMessageBox.critical(self, "登录失败", msg),
                                           self._append_log("登录失败：" + msg)))
        worker.success.connect(lambda name, uuid: (dialog.accept(),
                                                   self._append_log(f"登录成功：{name}"),
                                                   self._refresh_account_bar(),
                                                   self._warm_token()))
        worker.finished.connect(worker.deleteLater)
        dialog.rejected.connect(worker.cancel.cancel)
        self._login_dialog = dialog
        self._start_worker(worker)
        try:
            dialog.exec()
        finally:
            worker.cancel.cancel()
            self._login_dialog = None

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
            self._refresh_account_bar()
            return
        worker = TokenWorker(self.accounts)
        worker.status.connect(lambda msg: self.status.showMessage(msg, 4000))
        worker.success.connect(
            lambda name, uuid, remaining: (self._refresh_account_bar(),
                                           self.status.showMessage(
                                               f"令牌就绪：{name}（剩余 {remaining // 3600} 小时）", 6000)))
        worker.failed.connect(lambda msg: (self._append_log(f"⚠ 令牌刷新失败：{msg}"),
                                           self.status.showMessage("令牌刷新失败", 6000)))
        self._start_worker(worker)

    # ================================================================= 启动
    def _on_launch(self) -> None:
        if self._busy:
            return
        self._collect_from_ui()
        try:
            save_config(self.cfg)
        except OSError as exc:
            self._append_log(f"⚠ 配置保存失败（仍会继续）：{exc}")
        if self.accounts.selected is None:
            if QMessageBox.question(self, "还没有登录",
                                    "还没有登录微软账号，无法获取 access_token。现在去登录吗？") \
                    == QMessageBox.StandardButton.Yes:
                self._on_login_clicked()
            return

        self.accounts.cfg = self.cfg.auth
        self._set_busy(True, "正在准备启动…")
        worker = LaunchWorker(self.cfg, self.accounts)
        worker.status.connect(lambda msg: self.status.showMessage(msg))
        worker.log_line.connect(self._append_log)
        worker.failed.connect(self._on_launch_failed)
        worker.service_ready.connect(self._on_service_ready)
        worker.game_exited.connect(self._on_game_exited)
        worker.finished.connect(lambda: self._set_busy(False))
        self._start_worker(worker)

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
        worker = GameExitWorker(service, ask_handler=lambda name: answers.get(name, True))
        worker.log_line.connect(self._append_log)
        worker.status.connect(lambda msg: self.status.showMessage(msg))
        worker.reports_ready.connect(self._on_reports_ready)
        worker.finished.connect(lambda: self._set_busy(False))
        self._start_worker(worker)

    def _ask_close_tools(self) -> dict[str, bool] | None:
        """在游戏退出后询问（只对配置为"询问"的工具弹窗）。"""
        answers: dict[str, bool] = {}
        for name in ("jingle", "ninjabrainbot", "obs"):
            tool = self.cfg.tools.get(name)
            if tool is None or tool.close_on_exit != CLOSE_ASK:
                continue
            label = TOOL_LABELS.get(name, name)
            box = QMessageBox(self)
            box.setWindowTitle("清理确认")
            box.setText(f"游戏已退出。要关闭 {label} 吗？")
            box.setInformativeText(f"（{label} 的退出策略设为“退出时询问”，可在“辅助工具”页改成固定行为）")
            close_btn = box.addButton(f"关闭 {label}", QMessageBox.ButtonRole.AcceptRole)
            keep_btn = box.addButton(f"保留 {label}", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(close_btn)
            box.exec()
            answers[name] = box.clickedButton() is close_btn
            del keep_btn
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
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._threads.append(thread)
        thread.start()
        return thread

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        try:
            self._collect_from_ui()
            save_config(self.cfg)
        except OSError:
            pass
        if self.service is not None and self.service.game_running and self.cfg.close_game_on_launcher_exit:
            self.service.stop_game()
        if self.service is not None:
            self.service.shutdown()
        for thread in self._threads:
            thread.quit()
            thread.wait(3000)
        super().closeEvent(event)
