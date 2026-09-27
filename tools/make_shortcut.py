"""创建/修复桌面快捷方式：图标 + AppUserModelID 一起搞对。

    python tools/make_shortcut.py

做三件事：
  1. 建 .lnk（目标 wscript.exe + MCSR-Launcher.vbs，工作目录 = 仓库根，图标 = assets/mcsr-launcher.ico）
  2. 给 .lnk 设 System.AppUserModel.ID，且和进程里设的那个一致 ——
     否则固定的快捷方式跟运行中的窗口会被 Windows 当成两个不同的应用
  3. 读回属性核对，不靠"我觉得设上了"

AppUserModelID 是个 shell 属性，WScript.Shell 那个 COM 接口根本没有这个字段，
只能用 IPropertyStore（这里的 C# 就是干这个的）。注意：C# 片段里不能有中文，
否则 PowerShell 传参时的编码会把字符串搞坏，注释一律用英文。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from mcsl import APP_NAME, APP_VERSION          # noqa: E402
from mcsl.paths import project_root              # noqa: E402

AUMID = f"SampleBlazkness.{APP_NAME}.{APP_VERSION}"

PS_SCRIPT = r"""
param($LnkPath, $VbsPath, $WorkDir, $IconPath, $Aumid)

$ErrorActionPreference = 'Stop'

# --- 1. create the shortcut (what WScript.Shell can do) ---
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($LnkPath)
$lnk.TargetPath       = "$env:SystemRoot\System32\wscript.exe"
$lnk.Arguments        = '"' + $VbsPath + '"'
$lnk.WorkingDirectory = $WorkDir
$lnk.IconLocation     = $IconPath + ',0'
$lnk.Description      = 'MCSR-Launcher'
$lnk.Save()

# --- 2. set System.AppUserModel.ID (needs IPropertyStore) ---
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

[ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IPropertyStore {
    uint GetCount(out uint cProps);
    uint GetAt(uint iProp, out PROPERTYKEY pkey);
    uint GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
    uint SetValue(ref PROPERTYKEY key, ref PROPVARIANT pv);
    uint Commit();
}

[StructLayout(LayoutKind.Sequential, Pack = 4)]
public struct PROPERTYKEY { public Guid fmtid; public uint pid; }

[StructLayout(LayoutKind.Explicit)]
public struct PROPVARIANT {
    [FieldOffset(0)] public ushort vt;
    [FieldOffset(8)] public IntPtr p;
}

public static class AumidSetter {
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    static extern void SHGetPropertyStoreFromParsingName(
        string pszPath, IntPtr pbc, int flags, ref Guid riid, out IPropertyStore ppv);

    static readonly Guid IID_IPropertyStore = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
    static readonly Guid FMTID_AppUserModel = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
    const uint PID_AppUserModel_ID = 5;
    const int GPS_READWRITE = 2;
    const ushort VT_LPWSTR = 31;

    public static string Set(string lnk, string aumid) {
        Guid iid = IID_IPropertyStore;
        IPropertyStore store;
        SHGetPropertyStoreFromParsingName(lnk, IntPtr.Zero, GPS_READWRITE, ref iid, out store);
        PROPERTYKEY key = new PROPERTYKEY();
        key.fmtid = FMTID_AppUserModel;
        key.pid = PID_AppUserModel_ID;

        // PROPVARIANT for a string, built by hand: propsys.dll does not export
        // InitPropVariantFromString on every Windows build, so do not rely on it.
        PROPVARIANT pv = new PROPVARIANT();
        pv.vt = VT_LPWSTR;
        pv.p = Marshal.StringToCoTaskMemUni(aumid);
        store.SetValue(ref key, ref pv);
        store.Commit();
        Marshal.FreeCoTaskMem(pv.p);

        PROPVARIANT got;
        store.GetValue(ref key, out got);
        return Marshal.PtrToStringUni(got.p);
    }
}
'@

$readback = [AumidSetter]::Set($LnkPath, $Aumid)

# --- 3. report the AUMID readback (link fields must be read in a FRESH process:
#        the shell link handler is cached per process, so reading them here would
#        show empty strings and look like the shortcut got wiped)
Write-Output ("AUMID_READBACK=" + $readback)
"""


def main() -> int:
    root = project_root()
    ico = root / "assets" / "mcsr-launcher.ico"
    vbs = root / "MCSR-Launcher.vbs"
    for path in (ico, vbs):
        if not path.is_file():
            print(f"缺少文件：{path}")
            return 1

    desktop = Path.home() / "OneDrive" / "Desktop"
    if not desktop.is_dir():
        desktop = Path.home() / "Desktop"
    lnk = desktop / "MCSR-Launcher.lnk"

    script = PS_SCRIPT
    # 传给 PowerShell 用 -File + 真文件：-Command 后面再跟命名参数不会绑到 param 块上
    # （会静默变成 $null，症状是 CreateShortcut('') 报 "must end with .lnk or .url"）。
    # 文件必须纯 ASCII + CRLF：PowerShell 读 .ps1 用系统代码页，中文会吞换行。
    ps_file = Path.home() / "AppData/Local/Temp/mcsl-make-shortcut.ps1"
    with open(ps_file, "w", encoding="ascii", newline="\r\n") as fh:
        fh.write(script)
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps_file),
         "-LnkPath", str(lnk), "-VbsPath", str(vbs), "-WorkDir", str(root),
         "-IconPath", str(ico), "-Aumid", AUMID],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.stderr.strip():
        print("STDERR:", result.stderr.strip()[:500])

    readback = ""
    for line in (result.stdout or "").splitlines():
        if line.startswith("AUMID_READBACK="):
            readback = line.split("=", 1)[1].strip()

    # 链接字段必须另起一个进程读：同一个进程里 shell 的链接处理器有缓存，
    # 写完属性后再读会得到空字符串，看着像"快捷方式被写坏了"。
    verify_ps = Path.home() / "AppData/Local/Temp/mcsl-verify-shortcut.ps1"
    with open(verify_ps, "w", encoding="ascii", newline="\r\n") as fh:
        fh.write("param($LnkPath)\n"
                 "$ws = New-Object -ComObject WScript.Shell\n"
                 "$l = $ws.CreateShortcut($LnkPath)\n"
                 "Write-Output ('TARGET=' + $l.TargetPath)\n"
                 "Write-Output ('ARGS=' + $l.Arguments)\n"
                 "Write-Output ('ICON=' + $l.IconLocation)\n"
                 "Write-Output ('WORKDIR=' + $l.WorkingDirectory)\n")
    check = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(verify_ps),
         "-LnkPath", str(lnk)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")

    fields: dict[str, str] = {}
    for line in (check.stdout or "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    print("快捷方式实际内容（新进程读回）：")
    for key in ("TARGET", "ARGS", "ICON", "WORKDIR"):
        print(f"  {key:<8}= {fields.get(key, '(空)')}")
    print(f"  AUMID   = {readback or '(空)'}")

    problems = []
    if AUMID.upper() not in readback.upper():
        problems.append("AppUserModelID 没写进去")
    if "wscript.exe" not in fields.get("TARGET", "").lower():
        problems.append("目标是空的或不是 wscript.exe")
    if "mcsr-launcher.vbs" not in fields.get("ARGS", "").lower():
        problems.append("参数里没有 MCSR-Launcher.vbs")
    if "mcsr-launcher.ico" not in fields.get("ICON", "").lower():
        problems.append("图标没指向我们的 ico")
    print(f"\n快捷方式：{lnk}")
    if problems:
        print("有问题：\n  - " + "\n  - ".join(problems))
        return 1
    print("检查通过：目标、参数、图标、AppUserModelID 全部正确 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
