"""账号凭据存储：Windows DPAPI 加密后落盘。

为什么用 DPAPI：refresh token 等价于账号的长期凭据（90 天内可无限换新令牌）。
用 DPAPI（CryptProtectData）加密后，文件被拷到别的机器/别的用户下都解不开，
比明文 JSON 强得多；同时不需要用户再记一个主密码。

文件位置：%LOCALAPPDATA%\\MCSR-Launcher\\accounts.dat —— 在仓库之外，
也不可能被 git 或 OneDrive 带走。
"""
from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..paths import accounts_file

log = logging.getLogger(__name__)

# 额外的 entropy：绑定到本应用名，降低别的程序顺手解密的可能性
_ENTROPY = b"MCSR-Launcher/accounts/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01
_STORE_VERSION = 1


# ---------------------------------------------------------------- DPAPI
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _is_windows() -> bool:
    return os.name == "nt"


def _blob_from_bytes(data: bytes) -> tuple[_DataBlob, Any]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def dpapi_available() -> bool:
    if not _is_windows():
        return False
    try:
        _crypt32()
        return True
    except OSError:
        return False


def _crypt32():
    lib = ctypes.WinDLL("crypt32.dll", use_last_error=True)
    lib.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    lib.CryptProtectData.restype = wintypes.BOOL
    lib.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    lib.CryptUnprotectData.restype = wintypes.BOOL
    return lib


def protect(plaintext: bytes) -> bytes:
    if not dpapi_available():
        raise OSError("DPAPI 不可用（非 Windows 环境）")
    crypt32 = _crypt32()
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]

    in_blob, _keep = _blob_from_bytes(plaintext)
    ent_blob, _ent_keep = _blob_from_bytes(_ENTROPY)
    out_blob = _DataBlob()
    ok = crypt32.CryptProtectData(ctypes.byref(in_blob), "MCSR-Launcher accounts",
                                  ctypes.byref(ent_blob), None, None,
                                  _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def unprotect(ciphertext: bytes) -> bytes:
    if not dpapi_available():
        raise OSError("DPAPI 不可用（非 Windows 环境）")
    crypt32 = _crypt32()
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]

    in_blob, _keep = _blob_from_bytes(ciphertext)
    ent_blob, _ent_keep = _blob_from_bytes(_ENTROPY)
    out_blob = _DataBlob()
    ok = crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, ctypes.byref(ent_blob),
                                    None, None, _CRYPTPROTECT_UI_FORBIDDEN,
                                    ctypes.byref(out_blob))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


# ---------------------------------------------------------------- 数据模型
@dataclass
class Account:
    """一个已登录的微软账号。

    refresh_token 与签发它的 client_id 绑定：client_id 变了就必须重新登录，
    所以这里把 client_id 一起存下来做校验。
    """
    uuid: str = ""
    name: str = ""
    refresh_token: str = ""
    client_id: str = ""
    endpoint_preset: str = "live"
    added_at: float = field(default_factory=time.time)
    last_used: float = 0.0
    session: dict[str, Any] | None = None   # 缓存的 Minecraft 令牌（同样加密保存）

    @property
    def display(self) -> str:
        return f"{self.name}（{self.uuid[:8]}…）" if self.uuid else self.name


@dataclass
class StoreData:
    version: int = _STORE_VERSION
    selected: str = ""                       # 选中的账号 uuid
    accounts: list[Account] = field(default_factory=list)


# ---------------------------------------------------------------- 存储
class AccountStore:
    """读写 accounts.dat。

    加密可用时是整个 JSON blob 的 DPAPI 密文；不可用时（非 Windows）
    退化为 base64 明文并在日志里明确告警 —— 数据目录仍在仓库之外。
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or accounts_file()

    # -------- 低层 --------
    def _read_raw(self) -> StoreData:
        if not self.path.exists():
            return StoreData()
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            log.error("读取账号文件失败：%s", exc)
            return StoreData()
        if not raw.strip():
            return StoreData()
        try:
            if raw[:1] == b"{":
                payload = json.loads(raw.decode("utf-8"))
                log.warning("账号文件是明文格式（可能是早期版本写的），将在下次保存时加密")
            else:
                payload = json.loads(unprotect(raw).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 解密失败原因很多，统一降级处理
            log.error("解密账号文件失败（%s）。若更换过 Windows 用户/机器，需要重新登录。", exc)
            return StoreData()
        accounts = [Account(**{k: v for k, v in a.items() if k in Account.__dataclass_fields__})
                    for a in payload.get("accounts", [])]
        return StoreData(version=payload.get("version", _STORE_VERSION),
                         selected=payload.get("selected", ""), accounts=accounts)

    def _write_raw(self, data: StoreData) -> None:
        blob = json.dumps(asdict(data), ensure_ascii=False).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if dpapi_available():
            out = protect(blob)
        else:
            log.warning("DPAPI 不可用，账号文件将以 base64 明文保存（仅限非 Windows 调试用）")
            out = base64.b64encode(blob)
        tmp = self.path.with_suffix(".dat.tmp")
        tmp.write_bytes(out)
        try:  # 尽量收紧权限（只有当前用户可读写）
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.path)

    # -------- 高层 --------
    def load(self) -> StoreData:
        return self._read_raw()

    def save(self, data: StoreData) -> None:
        self._write_raw(data)

    def upsert(self, account: Account, *, select: bool = True) -> StoreData:
        data = self._read_raw()
        for i, existing in enumerate(data.accounts):
            if existing.uuid and existing.uuid == account.uuid:
                data.accounts[i] = account
                break
        else:
            data.accounts.append(account)
        if select or not data.selected:
            data.selected = account.uuid
        self._write_raw(data)
        return data

    def remove(self, uuid: str) -> StoreData:
        data = self._read_raw()
        data.accounts = [a for a in data.accounts if a.uuid != uuid]
        if data.selected == uuid:
            data.selected = data.accounts[0].uuid if data.accounts else ""
        self._write_raw(data)
        return data

    def update_session(self, uuid: str, session: dict[str, Any] | None) -> None:
        data = self._read_raw()
        for acc in data.accounts:
            if acc.uuid == uuid:
                acc.session = session
                acc.last_used = time.time()
                break
        self._write_raw(data)
