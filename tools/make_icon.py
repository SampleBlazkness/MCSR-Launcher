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
# 圆角半径占边长的比例。0.22 接近 Windows 11 应用图标的观感（squircle 那一路）。
DEFAULT_RADIUS = 0.22
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


def rounded_corners(image, radius_ratio: float):
    """把直角方块切成圆角（圆角外变成透明）。

    掩膜按 4 倍超采样画再缩回来：直接在小尺寸上画圆角会得到锯齿边，
    而图标要在 16px 下也不糊。radius_ratio 是圆角半径占边长的比例。
    """
    from PIL import Image, ImageChops, ImageDraw

    if radius_ratio <= 0:
        return image
    size = image.width
    scale = 4
    big = size * scale
    mask = Image.new("L", (big, big), 0)
    radius = max(1, round(big * min(radius_ratio, 0.5)))
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=radius, fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    alpha = ImageChops.multiply(image.getchannel("A"), mask)
    out = image.copy()
    out.putalpha(alpha)
    return out


def build_variant(data: bytes, radius_ratio: float):
    """从 jar 里的原始 png 生成指定圆角的主图（最大尺寸，未加圆角前先放大）。"""
    from PIL import Image
    import io

    image = Image.open(io.BytesIO(data)).convert("RGBA")
    biggest = max(SIZES)
    if image.width < biggest:
        # Pillow 存 ICO 时内部用 thumbnail()，只缩不放：源图 128px 却请求 256px
        # 会写出"目录里是 256、实际内容 128"的条目（Qt 也只读成 128），高 DPI 下模糊。
        # 草方块是像素画，所以用最近邻整数倍放大，才是游戏里的样子。
        resample = Image.NEAREST if biggest % image.width == 0 else Image.LANCZOS
        image = image.resize((biggest, biggest), resample)
    return rounded_corners(image, radius_ratio)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jar", default=None, help="客户端 jar 路径（默认用配置里的实例）")
    parser.add_argument("--radius", type=float, default=DEFAULT_RADIUS,
                        help=f"圆角半径占边长的比例，0 = 直角（默认 {DEFAULT_RADIUS}）")
    parser.add_argument("--sheet", default=None,
                        help="只生成一张对比图（几个圆角并排），不写 ico")
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
    print(f"从 {jar.name} 提取 {entry}（{len(data)} 字节）→ {source}")

    if args.sheet:
        # 对比图：不同圆角并排，放在深色底上，方便直接看
        ratios = (0.0, 0.12, 0.22, 0.40)
        cell, gap, pad = 128, 24, 24
        width = pad * 2 + cell * len(ratios) + gap * (len(ratios) - 1)
        sheet = Image.new("RGBA", (width, cell + pad * 2), (32, 32, 32, 255))
        for index, ratio in enumerate(ratios):
            tile = build_variant(data, ratio).resize((cell, cell), Image.LANCZOS)
            sheet.alpha_composite(tile, (pad + index * (cell + gap), pad))
        out = Path(args.sheet)
        sheet.convert("RGB").save(out)
        print(f"对比图（从左到右 0% 直角 / 12% / 22% / 40%）：{out}")
        return 0

    image = build_variant(data, args.radius)

    ico = ASSETS / "mcsr-launcher.ico"
    image.save(ico, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"已生成多尺寸图标 {ico}（{', '.join(f'{s}px' for s in SIZES)}，"
          f"圆角 {args.radius:.0%}）")

    # 顺便导出一份 png，方便以后做网页/文档用
    png = ASSETS / "mcsr-launcher-128.png"
    image.resize((128, 128), Image.LANCZOS).save(png)
    print(f"已导出 {png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
