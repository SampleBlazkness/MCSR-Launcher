"""自检：把所有不需要联网、不需要启动游戏的关键路径跑一遍。

    python tools/selftest.py

覆盖：配置读写、规则求值、版本解析、命令行组装、natives 目录准备、
DPAPI 凭据往返、密钥扫描。任何一项失败都会打印 [FAIL] 并以非零码退出。
"""
from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcsl.auth.store import Account, AccountStore, dpapi_available, protect, unprotect  # noqa: E402
from mcsl.config import AppConfig, load_config                                            # noqa: E402
from mcsl.launch import natives as natives_mod                                            # noqa: E402
from mcsl.launch.command import build_command                                             # noqa: E402
from mcsl.launch.rules import evaluate_rules                                              # noqa: E402
from mcsl.launch.version import list_instances, load_version, resolve_libraries           # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[ok]   {name}")
    else:
        print(f"[FAIL] {name}" + (f" —— {detail}" if detail else ""))
        FAILURES.append(name)


@dataclass
class FakeSession:
    name: str = "SelfTest"
    uuid: str = "0" * 32
    access_token: str = "DUMMY_TOKEN"
    token_type: str = "Bearer"
    client_id: str = "00000000402b5328"
    xuid: str = "2535405000000000"


def test_rules() -> None:
    check("无 rules → 允许", evaluate_rules(None) is True)
    check("windows 规则命中", evaluate_rules(
        [{"action": "allow", "os": {"name": "windows"}}]) is True)
    check("linux 规则不命中", evaluate_rules(
        [{"action": "allow", "os": {"name": "linux"}}]) is False)
    check("os.version 正则命中",
          evaluate_rules([{"action": "allow", "os": {"name": "windows", "version": "^10\\."}}],
                         os_version="10.0.22631") is True)
    check("features 未开启 → 不命中",
          evaluate_rules([{"action": "allow", "features": {"is_demo_user": True}}]) is False)
    check("allow 后跟 disallow 取第一条",
          evaluate_rules([{"action": "allow"}, {"action": "disallow"}]) is True)


def test_store() -> None:
    check("DPAPI 可用（Windows 上应为 True）", dpapi_available())
    if dpapi_available():
        blob = protect(b"secret-payload")
        check("DPAPI 加解密往返", unprotect(blob) == b"secret-payload")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "accounts.dat"
        store = AccountStore(path)
        store.upsert(Account(uuid="b" * 32, name="Tester", refresh_token="RT-XYZ",
                             client_id="00000000402b5328"))
        raw = path.read_bytes()
        check("凭据文件不含明文 refresh_token", b"RT-XYZ" not in raw)
        check("凭据文件不含明文玩家名", "Tester".encode() not in raw)
        data = store.load()
        check("凭据读回一致", data.accounts and data.accounts[0].refresh_token == "RT-XYZ")


def test_config() -> None:
    cfg = AppConfig.from_dict({
        "launch": {"instance": "a", "memory_mb": 2048,
                   "instances": {"b": {"memory_mb": 8192, "width": 1280}}},
        "tools": {"obs": {"close_on_exit": "ask", "path": "x.exe"}},
    })
    check("实例覆盖生效", cfg.launch.effective("b").memory_mb == 8192)
    check("实例覆盖不影响别的实例", cfg.launch.effective("a").memory_mb == 2048)
    check("实例覆盖的其它字段", cfg.launch.effective("b").width == 1280)
    check("工具配置解析", cfg.tools["obs"].close_on_exit == "ask")


def test_instances() -> None:
    cfg = load_config()
    mc_dir = Path(cfg.launch.minecraft_dir)
    if not mc_dir.is_dir():
        print(f"[skip] 找不到 .minecraft（{mc_dir}），跳过实例相关检查")
        return
    instances = list_instances(mc_dir)
    check("能列出实例", bool(instances), f"目录 {mc_dir}")
    for name in instances[:2]:
        version = load_version(mc_dir, name)
        check(f"{name} mainClass 非空", bool(version.main_class))
        resolved = resolve_libraries(version)
        check(f"{name} classpath 非空", len(resolved.classpath) > 0)
        check(f"{name} 无缺失库", not resolved.missing,
              "、".join(str(p) for p in resolved.missing[:3]))
        npath = natives_mod.prepare(version)
        check(f"{name} natives 目录可用", npath.is_dir())
        launch_cfg = cfg.launch.effective(name)
        command = build_command(version, FakeSession(), launch_cfg)
        joined = " ".join(command.args)
        check(f"{name} 命令行含主类", version.main_class in command.args)
        check(f"{name} accessToken 已填入", "--accessToken" in joined)
        check(f"{name} 命令行里没有未替换的占位符", "${" not in joined,
              joined[joined.index("${"):][:60] if "${" in joined else "")
        check(f"{name} cwd 存在", Path(command.cwd).is_dir())


def test_startup_path() -> None:
    """跑真正的入口路径（main 里的启动横幅 + 崩溃兜底），而不是只 import 模块。

    教训：曾经 _banner() 里引用了模块级不存在的 log_path（只在 main 里局部导入），
    所有 import 型测试都通过，一启动却直接弹 NameError。所以这里必须调用真函数。
    """
    import logging

    from mcsl import APP_NAME, APP_VERSION
    from mcsl.__main__ import _banner, _crash_report
    from mcsl.logging_setup import log_path, setup

    setup(level=logging.DEBUG)
    try:
        _banner(APP_NAME, APP_VERSION)
        check("启动横幅能执行（入口路径无 NameError）", True)
    except Exception as exc:  # noqa: BLE001
        check("启动横幅能执行（入口路径无 NameError）", False, f"{type(exc).__name__}: {exc}")

    # 崩溃兜底：把弹窗换成收集器，确认日志与提示内容都在
    collected: dict[str, str] = {}
    import mcsl.__main__ as entry

    original = entry._show_dialog
    entry._show_dialog = lambda message: collected.setdefault("msg", message)
    try:
        try:
            raise RuntimeError("自检：模拟启动失败")
        except RuntimeError as exc:
            _crash_report(exc)
        check("崩溃兜底会生成提示框文本", "启动失败" in collected.get("msg", ""))
        check("崩溃提示里带日志路径", str(log_path()) in collected.get("msg", ""))
    finally:
        entry._show_dialog = original


def test_secret_scan() -> None:
    """密钥扫描器必须能抓出 JWT，同时不误报正常代码。"""
    import subprocess  # noqa: F401 - 只用于说明扫描器可独立运行

    scanner = Path(__file__).parent / "check_secrets.py"
    # 刻意用拼接构造假 JWT：既验证扫描器能抓到三段式 JWT，又不在本文件里留下真形态的串
    fake = ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJ4dWlkIjoiMjUzNTQwNTAwMDAwMDAwMCJ9",
                     "abcdefghijklmnop"])
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "leak.txt"
        bad.write_text(f'token = "{fake}"\n', encoding="utf-8")
        good = Path(tmp) / "ok.py"
        good.write_text('NAME = "MCSR-Launcher"\n', encoding="utf-8")
        sys.path.insert(0, str(scanner.parent))
        import importlib.util

        spec = importlib.util.spec_from_file_location("check_secrets", scanner)
        module = importlib.util.module_from_spec(spec)          # type: ignore[arg-type]
        assert spec and spec.loader
        spec.loader.exec_module(module)
        check("扫描器能抓出 JWT", bool(module.scan(bad)))
        check("扫描器不误报正常文件", not module.scan(good))


def test_java() -> None:
    """Java 检测：版本号解析、release 文件读取、排序/挑选规则。

    这里用合成数据，不依赖本机装了什么 —— 旧的"按目录名排序取最后一个"逻辑
    在这组用例下会直接失败（"zulu-8" > "zulu-25"）。
    """
    from mcsl.launch.java import JavaInstall, parse_major, pick_best, read_install

    for text, expected in (("1.8.0_432", 8), ("25.0.2", 25), ("17", 17),
                           ("21.0.1+12-LTS", 21), ("", 0), ("junk", 0)):
        check(f"parse_major({text!r}) == {expected}", parse_major(text) == expected,
              f"得到 {parse_major(text)}")

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "zulu-8"
        (home / "bin").mkdir(parents=True)
        fake = home / "bin" / "java.exe"
        fake.write_bytes(b"\x00\x00")            # 不是真的可执行文件，只测解析
        (home / "release").write_text(
            'IMPLEMENTOR="Azul Systems, Inc."\nJAVA_VERSION="1.8.0_432"\nOS_ARCH="amd64"\n',
            encoding="utf-8")
        install = read_install(fake, "测试")
        check("read_install 读 release 文件得到大版本 8",
              install is not None and install.major == 8,
              f"得到 {install.major if install else None}")
        check("厂商/架构解析正确",
              install is not None and "Azul" in install.vendor_label and install.arch == "x64",
              f"{install.vendor_label if install else '-'}/{install.arch if install else '-'}")

        # 没有 release 且不是可执行文件 → 应当被判为不可用，而不是猜一个版本
        broken = Path(tmp) / "jdk-21" / "bin" / "java.exe"
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b"\x00\x00")
        check("读不出版本的文件被判为不可用",
              read_install(broken, "测试") is None)

    def make(major: int, path: str) -> JavaInstall:
        return JavaInstall(path=Path(path), home=Path(path).parent.parent, major=major,
                           version=str(major), vendor="Test", arch="x64", source="测试")

    installs = [make(8, r"C:\zulu-8\bin\java.exe"), make(21, r"C:\zulu-21\bin\java.exe"),
                make(25, r"C:\zulu-25\bin\java.exe")]
    check("需要 Java 8 时挑 Java 8",
          pick_best(installs, 8).major == 8, f"得到 {pick_best(installs, 8).major}")
    check("需要 Java 25 时挑 Java 25",
          pick_best(installs, 25).major == 25, f"得到 {pick_best(installs, 25).major}")
    check("实例没声明要求时挑版本最高的",
          pick_best(installs, None).major == 25, f"得到 {pick_best(installs, None).major}")
    check("没有匹配版本时挑一个最接近的（供提示，不等于匹配）",
          pick_best([make(8, r"C:\a\bin\java.exe")], 21).major == 8)
    check("空列表返回 None", pick_best([], 8) is None)


def test_wording() -> None:
    """面向用户的文案里不许出现只对某台机器/某个人成立的措辞。

    这条是用户明确要求过的（"不要针对我定制"），所以进回归清单：
    界面文本必须对任何用户、任何环境都成立。
    """
    import subprocess

    script = Path(__file__).resolve().parent / "check_wording.py"
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    tail = (proc.stdout or "").strip().splitlines()
    check("文案检查通过（tools/check_wording.py）", proc.returncode == 0,
          tail[-1] if tail else "")


def main() -> int:
    print("=" * 60)
    test_rules()
    print("-" * 60)
    test_store()
    print("-" * 60)
    test_config()
    print("-" * 60)
    test_instances()
    print("-" * 60)
    test_java()
    print("-" * 60)
    test_wording()
    print("-" * 60)
    test_startup_path()
    print("-" * 60)
    test_secret_scan()
    print("=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} 项失败：" + "、".join(FAILURES))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
