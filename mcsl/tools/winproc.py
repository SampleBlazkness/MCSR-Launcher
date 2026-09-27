"""Windows 进程/窗口底层操作（纯 ctypes，不依赖 pywin32）。

存在意义：上一个脚本只能靠 `Get-CimInstance ... -like '*Jingle*'` 猜进程，
而且提权进程关不掉时错误被 `-ErrorAction SilentlyContinue` 吞掉，看起来像
"已关闭"其实没关。这里每一步都返回明确的成功/失败和错误码，方便定位。
"""
from __future__ import annotations

import ctypes
import logging
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

WM_CLOSE = 0x0010
WM_QUIT = 0x0012

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
SYNCHRONIZE = 0x00100000
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
TOKEN_QUERY = 0x0008
TokenElevation = 20

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NO_CONSOLE = 0x00008000
SEE_MASK_FLAG_NO_UI = 0x00000400
SW_SHOWNORMAL = 1


@dataclass
class ProcessInfo:
    pid: int
    exe_name: str
    image_path: str = ""


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def _kernel32():
    return ctypes.WinDLL("kernel32.dll", use_last_error=True)


def is_windows() -> bool:
    return os.name == "nt"


# ------------------------------------------------------------------ 权限
def is_self_elevated() -> bool:
    if not is_windows():
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------- 应用身份（任务栏图标）
def set_app_user_model_id(app_id: str) -> bool:
    """给本进程设置显式 AppUserModelID（必须在创建任何窗口之前调用）。

    不设的话，Windows 会把 pythonw.exe 起的窗口归到「pythonw.exe」这个应用名下，
    任务栏按钮取的是 pythonw.exe 的图标（Python 那个），而窗口自己设的图标只在
    标题栏和 Alt+Tab 里生效 —— 这就是"任务栏还是 Python 图标"的根因。
    设了显式 ID，任务栏才把我们的窗口当作一个独立应用、改用窗口图标。
    """
    if not is_windows():
        return False
    try:
        shell32 = ctypes.windll.shell32
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        hr = shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(app_id))
        if hr != 0:
            return False
        return get_app_user_model_id().upper() == app_id.upper()
    except Exception:  # noqa: BLE001
        return False


def get_app_user_model_id() -> str:
    """读回本进程的 AppUserModelID（没显式设过时通常是 exe 路径）。"""
    if not is_windows():
        return ""
    try:
        buf = ctypes.c_wchar_p()
        ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(buf))
        return buf.value or ""
    except Exception:  # noqa: BLE001
        return ""


def is_process_elevated(pid: int) -> bool | None:
    """目标进程是否提权运行。None = 查不到（权限不足/进程已退出）。"""
    if not is_windows():
        return None
    k32 = _kernel32()
    advapi = ctypes.WinDLL("advapi32.dll", use_last_error=True)

    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            elevation = wintypes.DWORD()
            size = wintypes.DWORD()
            ok = advapi.GetTokenInformation(token, TokenElevation,
                                            ctypes.byref(elevation),
                                            ctypes.sizeof(elevation),
                                            ctypes.byref(size))
            if not ok:
                return None
            return bool(elevation.value)
        finally:
            k32.CloseHandle(token)
    finally:
        k32.CloseHandle(h)


# ------------------------------------------------------------------ 枚举
def enumerate_processes() -> list[ProcessInfo]:
    """列出所有进程（pid + exe 名）。"""
    if not is_windows():
        return []
    k32 = _kernel32()
    kernel32 = k32

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []
    out: list[ProcessInfo] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            out.append(ProcessInfo(pid=int(entry.th32ProcessID), exe_name=entry.szExeFile))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return out


def process_image_path(pid: int) -> str:
    if not is_windows():
        return ""
    k32 = _kernel32()
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        k32.CloseHandle(h)


def find_processes_by_path(exe_path: str | Path) -> list[ProcessInfo]:
    """按可执行文件完整路径查进程（比按名字匹配精确）。"""
    want = str(Path(exe_path)).lower()
    out = []
    for info in enumerate_processes():
        if info.exe_name.lower() not in (Path(want).name.lower(),):
            continue
        info.image_path = process_image_path(info.pid)
        if info.image_path.lower() == want:
            out.append(info)
    return out


# ------------------------------------------------------------------ 窗口
_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def windows_of_pid(pid: int, *, visible_only: bool = False) -> list[int]:
    """列出属于该 pid 的顶层窗口句柄（含隐藏窗口——OBS 最小化到托盘时窗口是隐藏的）。"""
    if not is_windows():
        return []
    user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
    found: list[int] = []

    def callback(hwnd, _lparam):
        wnd_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wnd_pid))
        if wnd_pid.value == pid:
            if not visible_only or user32.IsWindowVisible(hwnd):
                found.append(int(hwnd))
        return True

    user32.EnumWindows(_EnumWindowsProc(callback), 0)
    return found


def send_close(pid: int) -> int:
    """向该 pid 的所有顶层窗口发 WM_CLOSE，返回成功投递的数量。

    注意：窗口是隐藏的（最小化到托盘）也能收到 WM_CLOSE —— 只要用
    EnumWindows 枚举，而不是依赖 Get-Process 的 MainWindowHandle
    （后者在窗口隐藏时是 0，PowerShell 的 CloseMainWindow() 会直接假失败）。

    但**不要**用它做正常关闭：见 send_close_ranked 的说明。
    """
    if not is_windows():
        return 0
    user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
    posted = 0
    for hwnd in windows_of_pid(pid):
        if user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
            posted += 1
    return posted


# Java AWT / 输入法等的内部隐藏窗口：给它们发 WM_CLOSE 会让 AWT 事件循环
# 先挂掉，真正的窗口关闭事件反而送不到程序里（进程变僵尸）。
# 实测：NinjabrainBot 的 SunAwtToolkit 窗口被 WM_CLOSE 命中后就再也不会正常退出。
_INTERNAL_WINDOW_CLASSES = {
    "SunAwtToolkit", "SunAwtFrameProxy", "D3DFocusWindow", "IME",
    "MSCTFIME UI", "GDI+ Hook Window Class", "CLIPBRD_EVENT_WINDOW",
}
# 这些类更像是主窗口（加分）
_LIKELY_MAIN_CLASSES = {"SunAwtFrame", "Qt5152QWindowIcon", "Qt5QWindowIcon",
                        "Qt6QWindowIcon", "Chrome_WidgetWin_1", "GLFW30", "LWJGL"}

_WS_CAPTION = 0x00C00000
_GWL_STYLE = -16


@dataclass
class WindowInfo:
    hwnd: int
    class_name: str
    title: str
    visible: bool
    score: int = 0

    def describe(self) -> str:
        return (f"{self.hwnd:#x} 类={self.class_name or '?'} "
                f"标题={(self.title or '(无)')[:28]!r} 可见={'是' if self.visible else '否'} "
                f"得分={self.score}")


def window_info(hwnd: int) -> WindowInfo:
    user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
    cls = ctypes.create_unicode_buffer(256)
    title = ctypes.create_unicode_buffer(512)
    visible = bool(user32.IsWindowVisible(hwnd))
    try:
        user32.GetClassNameW(hwnd, cls, 256)
        user32.GetWindowTextW(hwnd, title, 512)
        user32.GetWindowLongW.restype = ctypes.c_long
        style = user32.GetWindowLongW(hwnd, _GWL_STYLE)
    except OSError:
        style = 0
    info = WindowInfo(hwnd=int(hwnd), class_name=cls.value, title=title.value, visible=visible)
    # 打分：越像"主窗口"越高，内部窗口直接打成负数被排到最后
    if info.class_name in _INTERNAL_WINDOW_CLASSES:
        info.score = -100
    else:
        if visible:
            info.score += 3
        if info.title.strip():
            info.score += 2
        if style & _WS_CAPTION:
            info.score += 1
        if info.class_name in _LIKELY_MAIN_CLASSES:
            info.score += 2
    return info


def candidate_windows(pid: int) -> list[WindowInfo]:
    """按"像主窗口的程度"排序的顶层窗口列表。"""
    infos = [window_info(hwnd) for hwnd in windows_of_pid(pid)]
    return sorted(infos, key=lambda i: -i.score)


def send_close_ranked(pid: int, *, per_window_timeout: float = 1.5,
                      total_timeout: float = 8.0,
                      on_note: Callable[[str], None] | None = None) -> tuple[bool, list[WindowInfo]]:
    """逐个向候选窗口投递 WM_CLOSE，命中主窗口就会正常退出。

    返回 (是否已退出, 尝试过的窗口列表)。每个窗口之间等一小会儿——只打主窗口
    才不会把 AWT/Qt 的内部窗口先打死。
    """
    if not is_windows():
        return True, []
    user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
    candidates = candidate_windows(pid)
    if not candidates:
        return not is_running(pid), []
    if on_note:
        on_note("候选窗口：" + "；".join(c.describe() for c in candidates[:4]))

    deadline = time.time() + total_timeout
    for info in candidates:
        if not is_running(pid):
            return True, candidates
        if time.time() >= deadline:
            break
        user32.PostMessageW(info.hwnd, WM_CLOSE, 0, 0)
        wait_until = min(time.time() + per_window_timeout, deadline)
        while time.time() < wait_until:
            if not is_running(pid):
                if on_note:
                    on_note(f"WM_CLOSE 命中主窗口 {info.class_name}，已正常退出")
                return True, candidates
            time.sleep(0.1)
        if on_note:
            on_note(f"WM_CLOSE 投递给 {info.class_name} 后无反应")
    return not is_running(pid), candidates


# ------------------------------------------------------------------ 等待/结束
def is_running(pid: int) -> bool:
    if not is_windows():
        return False
    k32 = _kernel32()
    h = k32.OpenProcess(SYNCHRONIZE, False, pid)
    if not h:
        return False
    try:
        return k32.WaitForSingleObject(h, 0) == 0x00000102  # WAIT_TIMEOUT = 仍在运行
    finally:
        k32.CloseHandle(h)


def wait_for_exit(pid: int, timeout: float) -> bool:
    """等待进程退出，True = 已退出。"""
    if not is_windows():
        return True
    k32 = _kernel32()
    h = k32.OpenProcess(SYNCHRONIZE, False, pid)
    if not h:
        return True   # 打不开通常意味着已经没了
    try:
        result = k32.WaitForSingleObject(h, int(timeout * 1000))
        return result == 0  # WAIT_OBJECT_0
    finally:
        k32.CloseHandle(h)


def terminate(pid: int) -> tuple[bool, str]:
    """TerminateProcess。返回 (是否成功, 错误说明)。"""
    if not is_windows():
        return False, "非 Windows 环境"
    k32 = _kernel32()
    h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        err = ctypes.get_last_error()
        return False, f"OpenProcess 失败（错误码 {err}: {ctypes.FormatError(err).strip()}）"
    try:
        if k32.TerminateProcess(h, 1):
            return True, ""
        err = ctypes.get_last_error()
        return False, f"TerminateProcess 失败（错误码 {err}: {ctypes.FormatError(err).strip()}）"
    finally:
        k32.CloseHandle(h)


# ------------------------------------------------------------------ 提权启动
def shell_execute(exe: str, args: list[str], *, cwd: str = "", elevate: bool = False,
                  wait: bool = False) -> tuple[int, str]:
    """ShellExecuteEx 启动进程，可指定 runas 提权。

    返回 (pid, 错误说明)。pid = 0 表示失败。
    """
    if not is_windows():
        return 0, "非 Windows 环境"
    shell32 = ctypes.WinDLL("shell32.dll", use_last_error=True)
    k32 = _kernel32()

    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_FLAG_NO_UI
    info.lpVerb = "runas" if elevate else "open"
    info.lpFile = str(exe)
    info.lpParameters = " ".join(_quote(a) for a in args)
    info.lpDirectory = cwd or None
    info.nShow = SW_SHOWNORMAL

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        return 0, f"ShellExecuteExW 失败（错误码 {err}: {ctypes.FormatError(err).strip()}）"
    if not info.hProcess:
        return 0, "启动成功但拿不到进程句柄"
    pid = k32.GetProcessId(info.hProcess)
    if wait:
        k32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
    k32.CloseHandle(info.hProcess)
    return int(pid), ""


def _quote(value: str) -> str:
    text = str(value)
    if not text:
        return '""'
    if any(ch in text for ch in ' \t"'):
        return '"' + text.replace('"', r'\"') + '"'
    return text


def relaunch_self_elevated(argv: list[str]) -> tuple[int, str]:
    """以管理员身份重启自己（Python 解释器 + 参数）。"""
    import sys

    exe = sys.executable
    logger = ""
    if exe.lower().endswith("python.exe") and os.path.exists(exe[:-10] + "w.exe"):
        # 用 pythonw 避免多弹一个控制台
        logger = exe[:-10] + "w.exe"
    return shell_execute(logger or exe, argv, elevate=True)


def kill_elevated(pids: list[int]) -> tuple[bool, str]:
    """请求提权的 taskkill 强杀一组 pid（用于自身没提权但目标提权的情况）。

    参数必须拆开传（"/PID", "1234"）：拼成 "/PID 1234" 一个参数时
    会被引号包成一整块，taskkill 报语法错误而静默失败。
    """
    if not pids:
        return True, ""
    args = ["/F"]
    for pid in pids:
        args += ["/PID", str(pid)]
    pid, err = shell_execute("taskkill.exe", args, elevate=True, wait=True)
    if pid == 0:
        return False, err
    return True, ""


def short_path(path: str | Path) -> str:
    return Path(path).name if path else ""
