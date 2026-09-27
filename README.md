# MCSR-Launcher

给 Minecraft 速通（RSG）用的一键启动器：**自动获取 Minecraft access_token**，
一条命令拉起 NinjabrainBot / Jingle / OBS 和游戏，退出后自动清理。

替代原来那个「每次都要去 HMCL 导出脚本、手动粘 access_token」的流程。

---

## 它解决了什么

| 旧流程的痛点 | 现在的做法 |
| --- | --- |
| 每次启动都要用 HMCL 导出脚本拿 access_token，手粘进 ps1 | 启动器自己走微软 OAuth（设备码 / 浏览器授权）→ Xbox Live → XSTS → Minecraft Services，**登录一次，之后自动续期** |
| `access_token` 明文写在脚本里、还躺在桌面上 | refresh token 用 Windows **DPAPI 加密**存在 `%LOCALAPPDATA%\MCSR-Launcher\accounts.dat`，仓库与 OneDrive 里都没有 |
| Jingle 关了不掉（提权进程 + UIPI），错误被 `-ErrorAction SilentlyContinue` 吞掉 | 工具由启动器自己启动、自己持有 PID；启动器默认以管理员身份运行，关闭时逐级降级（WM_CLOSE → 强杀 → 提权 taskkill）并**记录每一步的成功/失败原因** |
| OBS 关不掉（原因未定位） | 退出后弹窗询问；关闭过程写诊断日志，能直接看出卡在哪一步 |
| 4 个路径写死在脚本里 | 界面上四个路径选择器 + `.minecraft`、Java 路径，全部持久化 |

---

## 快速开始

```bash
# 1. 依赖（仓库里已经建好 .venv）
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# 2. 开图形界面（也可以双击 run.cmd 或桌面的 MCSR-Launcher 快捷方式）
.venv\Scripts\python -m mcsl
```

三种启动方式：

| 方式 | 说明 |
| --- | --- |
| 桌面快捷方式 `MCSR-Launcher.lnk` | **推荐**。指向 `wscript.exe` + `MCSR-Launcher.vbs`，全程不出现 cmd 黑框；图标取自 `assets/mcsr-launcher.ico` |
| `run.cmd` | 双击即可，同样不保留黑框；出错时才会停下来显示文字 |
| `run-console.cmd` | **带控制台**，报错和日志直接可见，排查问题时用这个 |

### 图标与任务栏

窗口/任务栏/快捷方式的图标都来自 `assets/mcsr-launcher.ico`（7 种尺寸，含真正的 256×256），
它由 `tools/make_icon.py` 从 Minecraft 客户端 jar 根目录的 `pack.png` 生成。
重新生成：`python tools/make_icon.py`（需要 Pillow，仅开发期）。

**任务栏之前显示的是 Python 图标**，根因不是图标文件，而是进程身份：`pythonw.exe` 起的窗口
默认被 Windows 归到「pythonw.exe」这个应用名下，任务栏按钮就用 pythonw.exe 的图标，
窗口自己设的图标只在标题栏和 Alt+Tab 生效。修法是启动时设一个显式的 AppUserModelID
（`mcsl/ui/app.py` 的 `_prepare_app_identity()`，在创建任何窗口之前调用），
并给桌面快捷方式设**同一个** ID（`tools/make_shortcut.py`），否则固定到任务栏会分成两组。

启动器**始终以管理员身份启动**（不再询问）：关闭 Jingle / OBS 这类以管理员权限运行的
工具必须要有管理员权限，否则会被 Windows 的 UIPI 拒绝。如果提权被拒（例如在 UAC 上
点了「否」），会弹出「启动器需要管理员权限」的警告，可选择重试或仍以普通权限启动。

> **为什么 `.cmd` / `.vbs` 里一个中文都不写**：cmd.exe 用系统 OEM 代码页（中文 Windows 是
> GBK）读取脚本字节，UTF-8 的中文注释会被错误解码，甚至把下一行的换行符一起吃掉，
> 于是 `rem …` 和 `setlocal` 被拼成一条命令，报
> `'…?setlocal' is not recognized as an internal or external command`。
> 所以这两个批处理和 VBS 保持纯 ASCII + CRLF。中文说明都放在本文件和界面里。
>
> 另外用 `pythonw` 启动时没有控制台：万一启动阶段就崩了，程序会用**原生 MessageBox**
> 弹出错误并把回溯写进日志，不会静默消失。

首次运行：

1. 「账号与登录」页确认 `client_id` 与端点预设（默认是 `live` + 官方启动器的公开 id，开箱可用）。
2. 点右上角「登录微软账号」，浏览器打开 `https://www.microsoft.com/link` 输入界面上的代码。
   登录成功后 refresh token 会加密落盘，**以后不用再登**。
3. 「路径与实例」页选好 `.minecraft` 目录、四个工具路径、Java 路径，选实例。
4. 「启动设置」页调内存 / JVM 参数（默认已经是你原来那套调参）。
5. 点「启动游戏」。

默认会**以管理员身份重新启动自己**（不再询问）——必须提权才能正常关闭 Jingle
这类以管理员身份启动的工具。提权被拒时会弹出警告，可选择重试或仍以普通权限启动。

---

## Java：为什么不再"自动挑一个"

旧版本点「自动检测本机 Java」是把找到的候选按**目录名**排序取最后一个 —— 字符串比较下
`zulu-8` 排在 `zulu-25` 之后，所以永远挑中 Java 8。1.16.1 用 Java 8 恰好是对的，
可一旦切到需要 Java 25 的 26.3 实例就必错，而且错得没有提示。

现在：

- `mcsl/launch/java.py` 从注册表、`PATH`、`JAVA_HOME`、常见安装目录（Zulu / Adoptium /
  Microsoft / Corretto / Liberica，以及官方启动器自带的 `runtime`）找出**所有** `java.exe`，
  然后**读 JDK 自带的 `release` 文件**拿到真实版本号（读不到才回退到起一个 `java -version`，
  并按路径+mtime 缓存，不会每次输入都去起进程）；读不出版本的文件直接判为不可用。
- Java 输入框下面有一行状态提示：匹配是绿字，不匹配是红字，**并给出「换成 Java 8（Azul）」
  这样的一键替换按钮**。
- 「选择 Java…（列出本机全部）」打开列表：每行显示 版本 / 是否匹配 / 厂商 / 架构 / 路径 / 来源，
  按「匹配优先、版本从高到低」排序，默认预选最合适的那个；还有「只显示与当前实例匹配的版本」
  筛选、「手动选择 java.exe…」和「重新检测」。列表下方会提示
  「看着像 Java 但里面没有 `bin\java.exe`」的坏目录（实测遇到过一个装到一半的 `zulu-21`）。
- 点「启动游戏」时会核对版本：不匹配弹一次确认（可以「仍然启动」，也可以直接「去换 Java…」），
  同一次运行里同一个实例只问一次。

实例需要哪个版本，直接读版本 JSON 的 `javaVersion.majorVersion`（本机：1.16.1 → 8，
26.3-Fabric → 25）。**Java 是按实例记住的**：勾上「上面的内存/JVM 参数只对当前实例生效」时，
选中的 Java 写进该实例的覆盖项（`launch.instances["<实例>"].java_path`），没勾就写全局 ——
所以两个实例可以各自记住自己的 Java，切实例时提示行会立刻告诉你对不对。

---

## 目录结构

```
mcsl/
  auth/        微软 OAuth（设备码 + 浏览器授权码 PKCE）/ XBL / XSTS / Minecraft Services / DPAPI 凭据存储
  launch/      版本 JSON 解析（含 inheritsFrom 合并）、rules 求值、natives、命令行组装、进程管理
               java.py：扫描本机所有 Java 并识别版本（注册表 / PATH / JAVA_HOME / 常见安装目录）
  tools/       Jingle / NinjabrainBot / OBS 的启动与关闭（纯 ctypes 的 Windows 进程/窗口操作）
  ui/          PySide6 界面（core 层不依赖它）
  cli.py       无界面入口，便于调试和自动化
config/
  defaults.json   默认配置模板（可提交，含账号之外的一切默认值）
tools/
  check_secrets.py       提交前密钥扫描（pre-commit 钩子调用）
  compare_with_ps1.py    开发期校验：把生成的命令行与 HMCL 导出脚本逐项对比
  ui_java_picker_probe.py Java 选择对话框 / 提示行的交互探针
```

运行期数据**全部在仓库之外**：

```
%LOCALAPPDATA%\MCSR-Launcher\
  accounts.dat    账号凭据（DPAPI 加密）
  config.json     你的实际配置（仓库里的 defaults.json 只提供默认值）
  logs\           日志（token 会被自动打码）
  run\            游戏控制台输出
```

---

## 配置要点

- **按实例覆盖**：`launch.instances["<实例名>"]` 可以只覆盖内存 / JVM 参数 / 分辨率等，
  例如默认配置里 `26.3-Fabric` 单独设了 10240MB、854×480 和它自己那套 GC 参数。
  界面上勾选「上面的内存/JVM 参数只对当前实例生效」即可。
- **JVM 参数预设**：`custom`（默认，逐字沿用你的调参）、`aikar`（Aikar 基线）、
  `vanilla`（只加 log4shell/RMI 加固参数）。
- **工具退出策略**：每个工具可选「自动关闭 / 退出时询问 / 保持运行」。
  默认 NinjabrainBot 和 Jingle 自动关，OBS 询问。
- **提权与游戏捕获**：MC 由提权进程启动时自身也是提权的，OBS 必须同样提权才能
  对它做游戏捕获，所以默认三个工具都按「管理员」启动（和你的旧脚本行为一致）。
  如果你关掉了启动器提权，Jingle / OBS 会改用提权 taskkill 收尾，可能弹一次 UAC。
- **命令行生成**：完全由版本 JSON 驱动（`arguments.jvm` / `arguments.game` + 占位符 +
  rules 求值），和 HMCL / 官方启动器同一套逻辑，装新实例不用改代码。
  已经与 HMCL 导出的脚本做过逐项对比，classpath 完全一致
  （1.16.1：42/42 项；26.3-Fabric：83/83 项）。

---

## 安全说明

- refresh token / access token **从不写入仓库**：`.gitignore` 排除了 `data/`、`accounts.dat`、
  `*.token`、`config/local.json` 等；另有 `.githooks/pre-commit` 做内容级扫描
  （JWT 特征、`refresh_token=` 之类的赋值），命中就阻止提交。
  启用钩子：`git config core.hooksPath .githooks`
- 凭据用 DPAPI 加密，绑定当前 Windows 用户 + 机器；文件拷走也解不开。
- 日志会把疑似 token 替换成 `<redacted>`。
- 如实说明：java 命令行里必然带 access_token，同一用户的其他进程能看到它。
  这是启动 Minecraft 的固有性质（HMCL、官方启动器同理），不是本程序引入的。
- 借用了官方启动器的公开 `client_id` 作为默认值。想要完全自主，就注册一个自己的
  Azure 应用（免费）：应用注册 → 支持的账户类型选「仅个人 Microsoft 账户」→
  重定向 URI 填 `http://localhost:29111` → 「允许公共客户端流」设为是 →
  把 Application (client) ID 填进界面，端点预设切到 `v2` 即可。

---

## 命令行工具

```bash
.venv\Scripts\python -m mcsl.cli instances                  # 列出实例
.venv\Scripts\python -m mcsl.cli dump-command --instance 1.16.1 --json   # 只组装命令行，不启动
.venv\Scripts\python -m mcsl.cli auth-status                # 账号/令牌状态
.venv\Scripts\python -m mcsl.cli login                      # 登录（终端里显示设备码）
.venv\Scripts\python -m mcsl.cli auth-token                 # 刷新令牌并显示抬头信息
.venv\Scripts\python -m mcsl.cli tools-status               # 工具路径与进程状态
.venv\Scripts\python -m mcsl.cli launch --no-tools          # 只启动游戏
```

---

## 已知限制 / 下一步

- 「原生模式」已实现，但**不会自动下载缺失的库**：库文件缺失时只在日志里警告
  （你的两个实例都由 HMCL 下载好了，不缺）。
- 辅助工具里只有 `.exe` 类型支持「已在运行就复用」；jar 工具无法可靠识别已有进程。
- 界面上的「按实例覆盖」只有内存/JVM 参数/分辨率；`config.json` 支持更多字段（java 路径、
  窗口标题等）。
- 计划中：Modrinth/Fabric 实例安装、每个实例独立的 4 条工具路径、录制状态与 OBS 联动、
  令牌失效时的无感重登、打包成单文件 exe。

## 许可

PySide6 是 LGPL v3：本程序以动态链接方式使用它，若要分发打包产物，请附上
Qt/PySide6 的许可声明。
