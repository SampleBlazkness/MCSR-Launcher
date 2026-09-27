"""从 Minecraft 客户端 jar 里提取图标，生成启动器用的多尺寸 .ico。

    python tools/make_icon.py                       # 用配置里的实例
    python tools/make_icon.py --jar "D:\\...\\1.16.1.jar"

来源：jar 根目录的 pack.png（Minecraft 自带的草方块图标，128x128）。
会写出：
    assets/icon-source.png     原始提取出来的图（留档，便于换源）
    assets/mcsr-launcher.ico   多尺寸图标（16/24/32/48/64/128/256）

注意：pack.png 是 Mojang 的美术资源，个人使用没问题；如果要公开发布，
建议换成自己的图标（把 assets/icon-source.png 换掉再重跑本脚本即可）。
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SIZES = (16, 24, 32, 48, 64, 128, 256)
CANDIDATES = ("pack.png", "assets/minecraft/icons/icon_128x128.png",
              "assets/minecraft/icons/icon_64x64.png")


def find_jar() -> Path:
    from mcsl.config import load_config

    cfg = load_config()
    mc_dir = Path(cfg.launch.minecraft_dir)
    instance = cfg.launch.instance
    jar = mc_dir / "versions" / instance / f"{instance}.jar"
    if jar.is_file():
        return jar
    raise SystemExit(f"找不到客户端 jar：{jar}")


def extract(jar: Path) -> tuple[bytes, str]:
    with zipfile.ZipFile(jar) as zf:
        for name in CANDIDATES:
            try:
                return zf.read(name), name
            except KeyError:
                continue
    raise SystemExit(f"{jar} 里没有找到可用的图标（试过 {', '.join(CANDIDATES)}）")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jar", default=None, help="客户端 jar 路径（默认用配置里的实例）")
    args = parser.parse_args()

    try:
        from PIL import Image
    except ImportError:
        raise SystemExit("需要 Pillow 来生成多尺寸 ico：pip install Pillow")

    jar = Path(args.jar) if args.jar else find_jar()
    data, entry = extract(jar)
    ASSETS.mkdir(parents=True, exist_ok=True)

    source = ASSETS / "icon-source.png"
    source.write_bytes(data)
    image = Image.open(source).convert("RGBA")
    print(f"从 {jar.name} 提取 {entry}（{image.width}x{image.height}）→ {source}")

    ico = ASSETS / "mcsr-launcher.ico"
    image.save(ico, format="ICO",
               sizes=[(s, s) for s in SIZES])
    print(f"已生成多尺寸图标 {ico}（{', '.join(f'{s}px' for s in SIZES)}）")

    # 顺便导出一份 png，方便以后做网页/文档用
    png = ASSETS / "mcsr-launcher-128.png"
    image.resize((128, 128), Image.LANCZOS).save(png)
    print(f"已导出 {png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
