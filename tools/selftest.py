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
    test_secret_scan()
    print("=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} 项失败：" + "、".join(FAILURES))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
