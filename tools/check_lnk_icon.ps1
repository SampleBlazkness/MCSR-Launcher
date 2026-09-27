param(
    [Parameter(Mandatory = $true)][string]$Item,   # .lnk 路径
    [Parameter(Mandatory = $true)][string]$Out,    # 导出的 png
    [int]$Size = 256
)

# 把「shell 现在解析这个快捷方式会画出来的图标」导出成 png。
# 和直接看 assets\*.ico 不同：这里走的是缓存，所以能区分"图标文件不对"和"缓存是旧的"。
# 用法： check_lnk_icon.ps1 -Item <lnk> -Out <png> [-Size 256]
$ErrorActionPreference = 'Stop'

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Drawing;

public static class ShellIconExport {
    [ComImport, Guid("bcc18b79-ba16-442f-80c4-8a59c30c463b"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IShellItemImageFactory {
        void GetImage(SIZE size, int flags, out IntPtr phbm);
    }

    [StructLayout(LayoutKind.Sequential)]
    struct SIZE { public int cx; public int cy; }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    static extern void SHCreateItemFromParsingName(string path, IntPtr pbc, ref Guid riid,
                                                  [MarshalAs(UnmanagedType.Interface)] out object ppv);

    const int SIIGBF_ICONONLY = 0x4;

    public static string Export(string path, int size, string outFile) {
        Guid iid = new Guid("bcc18b79-ba16-442f-80c4-8a59c30c463b");
        object factory;
        SHCreateItemFromParsingName(path, IntPtr.Zero, ref iid, out factory);
        IShellItemImageFactory f = (IShellItemImageFactory)factory;
        SIZE s = new SIZE(); s.cx = size; s.cy = size;
        IntPtr hbm;
        f.GetImage(s, SIIGBF_ICONONLY, out hbm);
        if (hbm == IntPtr.Zero) return "NO_IMAGE";
        using (Bitmap bmp = Bitmap.FromHbitmap(hbm)) {
            bmp.Save(outFile, System.Drawing.Imaging.ImageFormat.Png);
        }
        return "OK " + size + "px -> " + outFile;
    }
}
'@ -ReferencedAssemblies System.Drawing

Write-Output ([ShellIconExport]::Export($Item, $Size, $Out))
