param([switch]$NoCapture)

# 桌面图标刷新：调用 shell 的 SHChangeNotify(SHCNE_ASSOCCHANGED)，
# 这是 Win32 里让资源管理器重建图标缓存并重画视口的正规做法
# （比 ie4uinit.exe -show 更彻底，也不需要重启 explorer）。
$ErrorActionPreference = 'Stop'

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class ShellNotify {
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    public static extern void SHChangeNotify(int wEventId, uint uFlags, IntPtr dwItem1, IntPtr dwItem2);
}
'@

# SHCNE_ASSOCCHANGED = 0x08000000, SHCNF_IDLIST = 0
[ShellNotify]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)
Write-Output 'SHChangeNotify(SHCNE_ASSOCCHANGED) sent'
