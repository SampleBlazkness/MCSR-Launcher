"""Java 运行环境的发现与识别。

这一层不依赖 GUI，界面和命令行都用它。旧的"自动检测"是按目录名字符串排序取最后一个
（"zulu-8" > "zulu-25"，字符串比较），所以永远挑中 zulu-8；正确做法是**读出版本号**，
再按实例需要的 Java 大版本去匹配。

版本来源优先读 JDK 自带的 `release` 文件（瞬时、不用起进程），读不到才回退到
`java -version`。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# 这些键下面挂着 JavaHome / Path，装了 JDK 的发行版一般会写
_REGISTRY_KEYS = (
    r"SOFTWARE\JavaSoft\Java Development Kit",
    r"SOFTWARE\JavaSoft\JDK",
    r"SOFTWARE\JavaSoft\Java Runtime Environment",
    r"SOFTWARE\JavaSoft\JRE",
    r"SOFTWARE\Eclipse Adoptium\JDK",
    r"SOFTWARE\Eclipse Adoptium\JRE",
    r"SOFTWARE\Eclipse Foundation\JDK",
    r"SOFTWARE\Azul Systems\Zulu",
    r"SOFTWARE\Microsoft\JDK",
    r"SOFTWARE\Amazon Corretto",
    r"SOFTWARE\BellSoft\LibericaJDK",
    r"SOFTWARE\IBM\Semeru",
)

# 常见的安装根目录（每个子目录下找 bin\java.exe）
_COMMON_ROOTS = (
    r"C:\Program Files\Zulu",
    r"C:\Program Files\Java",
    r"C:\Program Files\Eclipse Adoptium",
    r"C:\Program Files\Eclipse Foundation",
    r"C:\Program Files\Microsoft",
    r"C:\Program Files\Amazon Corretto",
    r"C:\Program Files\BellSoft",
    r"C:\Program Files\Semeru",
    r"C:\Program Files\IBM",
    r"C:\Program Files\AdoptOpenJDK",
    r"C:\Program Files (x86)\Java",
)

_ARCH_NAMES = {"amd64": "x64", "x86_64": "x64", "x64": "x64",
               "x86": "x86", "i386": "x86", "i586": "x86",
               "aarch64": "arm64", "arm64": "arm64"}


@dataclass(frozen=True)
class JavaInstall:
    """一个可用的 Java 运行时。"""

    path: Path              # java.exe 的完整路径
    home: Path              # JAVA_HOME（java.exe 往上两级）
    major: int              # 8 / 17 / 21 / 25 …
    version: str            # "1.8.0_432" / "25.0.2"，读不出来就是空串
    vendor: str             # "Azul Systems, Inc." / "Eclipse Adoptium" …
    arch: str               # "x64" / "x86" / "arm64"
    source: str             # 从哪儿发现的，给人看

    @property
    def version_label(self) -> str:
        return f"Java {self.major}" + (f"（{self.version}）" if self.version else "")

    @property
    def vendor_label(self) -> str:
        v = self.vendor.strip()
        for known in ("Azul", "Adoptium", "Temurin", "Microsoft", "Corretto", "Liberica",
                      "Semeru", "Oracle", "OpenJDK", "GraalVM"):
            if known.lower() in v.lower():
                return known
        return v or "未知"

    def matches(self, required_major: int | None) -> bool:
        """没有要求（None/0）时一律算匹配。"""
        return not required_major or self.major == required_major

    def sort_key(self, required_major: int | None) -> tuple:
        """排序用：先匹配的，再版本高的，再 x64 的。"""
        return (
            0 if self.matches(required_major) else 1,
            -self.major,
            0 if self.arch == "x64" else 1,
            str(self.path).lower(),
        )


def parse_major(version: str) -> int:
    """'1.8.0_432' → 8；'25.0.2' → 25；认不出来返回 0。

    注意别用 int(float(...)) 那类写法：'1.8.0_432' 直接 float() 会抛异常，
    而 8 和 1.8 是同一个东西的两种写法。
    """
    text = (version or "").strip().strip('"')
    if not text:
        return 0
    m = re.match(r"^1\.(\d+)", text)
    if m:
        return int(m.group(1))
    m = re.match(r"^(\d+)", text)
    return int(m.group(1)) if m else 0


def _read_release_file(home: Path) -> dict[str, str]:
    """解析 JDK 自带的 release 文件（KEY="VALUE" 一行一个）。"""
    data: dict[str, str] = {}
    release = home / "release"
    if not release.is_file():
        return data
    try:
        text = release.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return data
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip().strip('"')
    return data


def _probe_version(exe: Path) -> tuple[str, str]:
    """跑 `java -version` 拿 (版本, 厂商)。只在没有 release 文件时才用。"""
    try:
        proc = subprocess.run([str(exe), "-version"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return "", ""
    out = (proc.stderr or "") + (proc.stdout or "")
    m = re.search(r'version "([^"]+)"', out)
    version = m.group(1) if m else ""
    vendor = ""
    first = next((ln for ln in out.splitlines() if "version" in ln), "")
    if "openjdk" in first.lower():
        m2 = re.search(r"openjdk\s+([\w\s.+-]+?)\s+[0-9]", first, re.IGNORECASE)
        vendor = (m2.group(1).strip() if m2 else "OpenJDK")
    elif first:
        vendor = first.split()[0]
    if "graalvm" in out.lower():
        vendor = "GraalVM"
    return version, vendor


def read_install(exe: Path, source: str = "") -> JavaInstall | None:
    """把一个 java.exe 变成 JavaInstall；版本读不出来就返回 None（多半不是真的 JVM）。"""
    try:
        exe = exe.resolve()
    except OSError:
        return None
    if not exe.is_file():
        return None
    home = exe.parent.parent

    rel = _read_release_file(home)
    version = rel.get("JAVA_VERSION", "")
    vendor = rel.get("IMPLEMENTOR", "") or rel.get("IMPLEMENTOR_NAME", "")
    arch = rel.get("OS_ARCH", "")
    if not version:
        version, probed_vendor = _probe_version(exe)
        vendor = vendor or probed_vendor
    major = parse_major(version)
    if not major:
        return None
    if not arch:
        bits = 64 if sys.maxsize > 2 ** 32 else 32
        arch = f"x{bits}"
    return JavaInstall(path=exe, home=home, major=major, version=version,
                       vendor=vendor, arch=_ARCH_NAMES.get(arch.lower(), arch),
                       source=source or str(home.parent))


def _registry_homes() -> list[tuple[Path, str]]:
    """从注册表捞 JAVA_HOME（HKCU + HKLM，含 WOW6432Node）。"""
    if os.name != "nt":
        return []
    import winreg

    found: list[tuple[Path, str]] = []
    for root, root_name in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"),
                            (winreg.HKEY_CURRENT_USER, "HKCU")):
        for key in _REGISTRY_KEYS:
            for prefix in ("", r"WOW6432Node\\"):
                try:
                    with winreg.OpenKey(root, prefix + key) as handle:
                        count = winreg.QueryInfoKey(handle)[0]
                        for i in range(count):
                            try:
                                sub = winreg.EnumKey(handle, i)
                                with winreg.OpenKey(handle, sub) as sub_handle:
                                    for value_name in ("JavaHome", "Path", "InstallationPath"):
                                        try:
                                            value, _ = winreg.QueryValueEx(sub_handle, value_name)
                                        except OSError:
                                            continue
                                        raw = str(value).strip().rstrip("\\")
                                        # Adoptium/MSI 的 Path 指向 ...\bin\ 或整棵树
                                        candidate = Path(raw)
                                        if candidate.name.lower() == "bin":
                                            candidate = candidate / "java.exe"
                                        elif (candidate / "bin" / "java.exe").is_file():
                                            candidate = candidate / "bin" / "java.exe"
                                        else:
                                            candidate = candidate / "bin" / "java.exe"
                                        found.append((candidate,
                                                      f"注册表 {root_name}\\{key}\\{sub}"))
                                        break
                            except OSError:
                                continue
                except OSError:
                    continue
    return found


def _common_dir_candidates() -> list[tuple[Path, str]]:
    """扫常见安装根目录：<root>\\<版本目录>\\bin\\java.exe，最多再往里一层。"""
    found: list[tuple[Path, str]] = []
    roots = [Path(p) for p in _COMMON_ROOTS]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Programs")
    # 官方启动器自带的运行时（java-runtime-gamma/delta/epsilon…）
    ms_base = Path(os.environ.get("LOCALAPPDATA", "")) / "Packages"
    if ms_base.is_dir():
        try:
            for pkg in ms_base.glob("Microsoft.4297127D64EC6*"):
                runtime = pkg / "LocalCache" / "Local" / "runtime"
                if runtime.is_dir():
                    roots.append(runtime)
        except OSError:
            pass

    for root in roots:
        if not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            direct = child / "bin" / "java.exe"
            if direct.is_file():
                found.append((direct, f"常见目录 {root.name}\\{child.name}"))
                continue
            # 再下一层：runtime\java-runtime-epsilon\windows-x64\java-runtime-epsilon\bin
            try:
                grandchildren = sorted(child.iterdir())[:12]
            except OSError:
                continue
            for grand in grandchildren:
                exe = grand / "bin" / "java.exe"
                if exe.is_file():
                    found.append((exe, f"常见目录 {root.name}\\{child.name}\\{grand.name}"))
                    continue
                for third in (grand / arch for arch in ("windows-x64", "windows-x86", "bin")):
                    exe = third / "bin" / "java.exe" if third.name.startswith("windows") else third / "java.exe"
                    if exe.is_file():
                        found.append((exe, f"常见目录 {root.name}\\{child.name}\\{grand.name}"))
    return found


def find_java_installations(extra_paths: list[str] | None = None) -> list[JavaInstall]:
    """全机扫一遍，返回去重后的 Java 列表（尽量快的顺序：注册表→PATH→常见目录）。"""
    raw: list[tuple[Path, str]] = []

    # 1) 注册表
    raw.extend(_registry_homes())
    # 2) PATH 上的 java（可能有好几个：where java 会全部列出来）
    which = shutil.which("java")
    if which:
        raw.append((Path(which), "PATH"))
    if os.name == "nt":
        try:
            where = subprocess.run(["where", "java"], capture_output=True, text=True,
                                   timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)
            for line in (where.stdout or "").splitlines():
                line = line.strip()
                if line.lower().endswith("java.exe"):
                    raw.append((Path(line), "PATH"))
        except (OSError, subprocess.SubprocessError):
            pass
    # 3) JAVA_HOME
    java_home = os.environ.get("JAVA_HOME", "").strip()
    if java_home:
        raw.append((Path(java_home) / "bin" / "java.exe", "JAVA_HOME"))
    # 4) 常见目录
    raw.extend(_common_dir_candidates())
    # 5) 调用方给的额外路径（例如配置里当前填的那个）
    for extra in (extra_paths or []):
        if extra and extra.strip():
            raw.append((Path(extra.strip()), "当前配置"))

    installs: list[JavaInstall] = []
    seen: set[str] = set()
    for exe, source in raw:
        try:
            key = str(exe.resolve()).lower()
        except OSError:
            key = str(exe).lower()
        if key in seen:
            continue
        install = read_install(exe, source)
        if install is None:
            continue
        if str(install.path).lower() in seen:
            continue
        seen.add(key)
        seen.add(str(install.path).lower())
        installs.append(install)
    return installs


def pick_best(installs: list[JavaInstall], required_major: int | None) -> JavaInstall | None:
    """挑最合适的：优先匹配所需大版本，其次版本号高、x64。"""
    if not installs:
        return None
    return sorted(installs, key=lambda i: i.sort_key(required_major))[0]


# 读 java 版本要么读 release 文件（瞬时），要么起一个 java 进程（几百毫秒）。
# 界面上的提示会在每次输入时刷新，所以这里按 (路径, mtime) 缓存 —— 重装 Java 换了
# mtime 自然会重新读。
_READ_CACHE: dict[tuple[str, float], JavaInstall | None] = {}


def read_install_cached(exe: Path | str) -> JavaInstall | None:
    """read_install 的带缓存版本（界面频繁调用时用这个）。"""
    path = Path(exe)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = (str(path).lower(), mtime)
    if key not in _READ_CACHE:
        if len(_READ_CACHE) > 64:
            _READ_CACHE.clear()
        _READ_CACHE[key] = read_install(path)
    return _READ_CACHE[key]


# 名字看起来像 Java 版本目录（zulu-21 / jdk-17 / temurin-21 / corretto-8 …）
_JAVA_DIR_RE = re.compile(r"^(zulu|jdk|jre|java|temurin|corretto|liberica|semeru|openjdk|graalvm)"
                          r"[-_.\d]", re.IGNORECASE)


def find_broken_java_dirs() -> list[Path]:
    """找出"看着像 Java 但里面没有 bin\\java.exe"的目录。

    实际遇到过的例子：C:\\Program Files\\Zulu\\zulu-21 整个目录只剩下一个空的 lib
    （安装/卸载中断留下的残骸）。这种目录如果直接列进候选，用户会以为"我装了 Java 21"。
    这里不删任何东西，只是把它显示出来提醒。
    """
    broken: list[Path] = []
    roots = [Path(p) for p in _COMMON_ROOTS]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Programs")
    for root in roots:
        if not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or not _JAVA_DIR_RE.match(child.name):
                continue
            if (child / "bin" / "java.exe").is_file():
                continue
            # 连 lib 都没有的纯空目录不必报（可能是别的用途的同名目录）
            try:
                has_content = any(child.iterdir())
            except OSError:
                continue
            if has_content:
                broken.append(child)
    return broken


def describe_requirement(required_major: int | None) -> str:
    return f"Java {required_major}" if required_major else "任意 Java 8+"
