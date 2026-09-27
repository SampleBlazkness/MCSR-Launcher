"""用提权方式跑一个脚本并等待它结束（本机 UAC 设为"始终允许"，不会弹窗）。

    python run_elevated.py C:\\path\\to\\script.ps1 [args...]

为什么要这个：被提权启动的进程，普通权限的 shell 既读不到它的命令行，也关不掉它
（UIPI）；而 Hermes 的 shell 本身不是提权的。这里用 ShellExecuteEx + runas 拉起。
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path

suffix = Path(sys.argv[1]).suffix.lower()
if suffix == ".ps1":
    argv = ["C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", sys.argv[1], *sys.argv[2:]]
else:
    argv = sys.argv[1:]

# ShellExecuteExW with lpVerb="runas"；用 SW_SHOWNORMAL 以便需要时能看到窗口
class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("fMask", ctypes.c_ulong),
                ("hwnd", ctypes.c_void_p), ("lpVerb", ctypes.c_wchar_p),
                ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
                ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int),
                ("hInstApp", ctypes.c_void_p), ("lpIDList", ctypes.c_void_p),
                ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.c_void_p),
                ("dwHotKey", ctypes.c_ulong), ("hIcon", ctypes.c_void_p),
                ("hProcess", ctypes.c_void_p)]

info = SHELLEXECUTEINFOW()
info.cbSize = ctypes.sizeof(info)
info.fMask = 0x00000040                      # SEE_MASK_NOCLOSEPROCESS
info.lpVerb = "runas"
info.lpFile = argv[0]
info.lpParameters = subprocess.list2cmdline(argv[1:]) if len(argv) > 1 else None
info.nShow = 1
if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
    err = ctypes.get_last_error() or ctypes.windll.kernel32.GetLastError()
    print(f"提权失败：ShellExecuteExW error={err}（1223 = 用户拒绝）")
    raise SystemExit(1)

time.sleep(0.5)
ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, 120_000)
code = ctypes.c_ulong()
ctypes.windll.kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
print(f"提权进程结束，退出码 {code.value}")
