param([int64]$Hwnd)

# 查某个窗口在 shell 眼里归属哪个应用身份（决定任务栏按钮的图标/分组）。
# 用 SHGetPropertyStoreForWindow + System.AppUserModel.* 属性。
$ErrorActionPreference = 'Stop'

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

public static class WinIdentity {
    [DllImport("shell32.dll", PreserveSig = false)]
    static extern void SHGetPropertyStoreForWindow(IntPtr hwnd, ref Guid riid, out IPropertyStore ppv);

    static readonly Guid IID_IPropertyStore = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
    // PKEY_AppUserModel_ID / RelaunchIconResource / RelaunchDisplayNameResource / RelaunchCommand
    static readonly Guid FMTID_AppUserModel = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");

    static string Read(IPropertyStore store, uint pid) {
        PROPERTYKEY key = new PROPERTYKEY();
        key.fmtid = FMTID_AppUserModel;
        key.pid = pid;
        PROPVARIANT pv;
        try {
            store.GetValue(ref key, out pv);
        } catch (Exception) {
            return "(读取失败)";
        }
        if (pv.vt == 31) return Marshal.PtrToStringUni(pv.p);   // VT_LPWSTR
        if (pv.vt == 8)  return Marshal.PtrToStringBSTR(pv.p);  // VT_BSTR
        return "(未设置, vt=" + pv.vt + ")";
    }

    public static string Dump(IntPtr hwnd) {
        Guid iid = IID_IPropertyStore;
        IPropertyStore store;
        SHGetPropertyStoreForWindow(hwnd, ref iid, out store);
        return "AUMID=" + Read(store, 5)
             + "|ICON=" + Read(store, 3)
             + "|DISPLAY=" + Read(store, 4)
             + "|COMMAND=" + Read(store, 2);
    }
}
'@

Write-Output ([WinIdentity]::Dump([IntPtr]$Hwnd))
