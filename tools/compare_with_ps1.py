"""开发期校验：把本程序组装出的命令行与 HMCL 导出的 ps1 做逐项对比。

用法（在项目根目录）：
    python tools/compare_with_ps1.py "C:\\Users\\Sampl\\OneDrive\\Desktop\\Minecraft Speedrun.ps1" 1.16.1
只会报告差异，不做修改；token 等敏感值已排除在比较之外。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcsl.config import load_config                     # noqa: E402
from mcsl.launch.command import build_command           # noqa: E402
from mcsl.launch.version import load_version            # noqa: E402

# 这些差异是设计上的，不算问题
IGNORE_PREFIX = ("-Dminecraft.client.jar", "-Dlog4j.configurationFile")
REPLACED = {"--accessToken", "--username", "--uuid", "--clientId", "--xuid", "--versionType"}


def parse_ps1(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    # 取 java 调用的那一行（以 & ' 开头）
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("&") and "java" in stripped:
            tokens = re.findall(r"'([^']*)'|\"([^\"]*)\"|(\S+)", stripped)
            return [t[0] or t[1] or t[2] for t in tokens][1:]  # 去掉 java 本身
    return []


def normalize(args: list[str]) -> list[str]:
    out = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in REPLACED:
            skip = True
            continue
        if any(arg.startswith(p) for p in IGNORE_PREFIX):
            continue
        if arg.startswith("--versionType"):
            continue
        out.append(arg)
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    ps1_path, instance = Path(sys.argv[1]), sys.argv[2]

    cfg = load_config()
    launch_cfg = cfg.launch.effective(instance)

    class Fake:
        name, uuid = "Blazkness", "f86a3ab6163c420197e5dfc196f6fd91"
        access_token, token_type = "DUMMY", "Bearer"
        client_id, xuid = cfg.auth.client_id, "2535405000000000"

    version = load_version(Path(launch_cfg.minecraft_dir), instance)
    ours = normalize(build_command(version, Fake(), launch_cfg).args)
    theirs = normalize(parse_ps1(ps1_path))

    print(f"比较实例 {instance}：本程序 {len(ours)} 个参数，ps1 {len(theirs)} 个参数")
    only_ours = [a for a in ours if a not in theirs]
    only_theirs = [a for a in theirs if a not in ours]
    print(f"\n只在我们的命令行里（{len(only_ours)}）：")
    for a in only_ours:
        print("  + " + (a[:150] + "…" if len(a) > 150 else a))
    print(f"\n只在 ps1 里（{len(only_theirs)}）：")
    for a in only_theirs:
        print("  - " + (a[:150] + "…" if len(a) > 150 else a))

    our_cp = next((a for a in ours if ";" in a and ".jar" in a), "")
    their_cp = next((a for a in theirs if ";" in a and ".jar" in a), "")
    if our_cp and their_cp:
        ours_set = {p.lower() for p in our_cp.split(";") if p}
        theirs_set = {p.lower() for p in their_cp.split(";") if p}
        print(f"\nclasspath：我们 {len(ours_set)} 项，ps1 {len(theirs_set)} 项，"
              f"交集 {len(ours_set & theirs_set)} 项")
        for path in sorted(ours_set - theirs_set):
            print("  + " + path)
        for path in sorted(theirs_set - ours_set):
            print("  - " + path)
    else:
        print("\nclasspath：没能从两边都取出 classpath 参数，跳过比较")
    return 0


if __name__ == "__main__":
    sys.exit(main())
