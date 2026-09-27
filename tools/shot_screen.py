"""抓屏幕（可只抓任务栏一条），存成 PNG 供人工/视觉核对。

    python tools/shot_screen.py                   # 全屏
    python tools/shot_screen.py --taskbar         # 先把鼠标压到屏幕最底部唤出任务栏，再抓那条
    python tools/shot_screen.py --out "D:/x/y.png"

用 CopyFromScreen（GDI）而不是 PrintWindow：任务栏是 explorer 画的，普通权限进程也能抓到；
被提权进程自己的窗口在截图里可能是黑的，但任务栏按钮正常。

注意：用户的 Windows 任务栏设了自动隐藏，不把鼠标移到屏幕最底部是看不到的。
"""
from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

user32 = ctypes.windll.user32


def primary_size() -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def summon_taskbar() -> None:
    """把鼠标移到屏幕最底部，让自动隐藏的任务栏滑出来。"""
    w, h = primary_size()
    user32.SetCursorPos(w // 2, h - 1)
    time.sleep(0.8)


def capture(rect: tuple[int, int, int, int], out: Path) -> Path:
    left, top, right, bottom = rect
    out.parent.mkdir(parents=True, exist_ok=True)
    script = f"""
Add-Type -AssemblyName System.Drawing
$bmp = New-Object System.Drawing.Bitmap({right - left}, {bottom - top})
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen({left}, {top}, 0, 0, $bmp.Size)
$bmp.Save('{out}', [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out",
                    default=str(Path.home() / "AppData/Local/Temp/mcsl-screen.png"))
    ap.add_argument("--taskbar", action="store_true",
                    help="先唤出任务栏，再只抓主屏最下面 60 像素")
    args = ap.parse_args()

    if args.taskbar:
        summon_taskbar()
    w, h = primary_size()
    rect = (0, h - 60, w, h) if args.taskbar else (0, 0, w, h)
    path = capture(rect, Path(args.out))
    print(f"已保存：{path}（屏幕 {w}x{h}，抓取区域 {rect}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
