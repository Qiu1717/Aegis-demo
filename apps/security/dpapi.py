# -*- coding: utf-8 -*-
"""
apps/security/dpapi.py — Windows DPAPI 的 ctypes 实现（不依赖 pywin32）

通过 crypt32.dll 的 CryptProtectData / CryptUnprotectData 做用户态加密：
密文绑定当前 Windows 账号，换账号/换机器后自动失效（AEG-K02）。

非 Windows 平台调用会抛出 DpapiUnavailable，由 keystore 回退到其它后端。

参考：
    CryptProtectData(
        pDataIn:      DATA_BLOB*,
        szDataDescr:  LPCWSTR,
        pOptionalEntropy: DATA_BLOB*,
        pvReserved:   PVOID,
        pPromptStruct: CRYPTPROTECT_PROMPTSTRUCT*,
        dwFlags:      DWORD,
        pDataOut:     DATA_BLOB*
    ) -> BOOL
"""
import ctypes
import ctypes.wintypes as wintypes
import sys
from typing import Optional

#: 描述串，仅用于标识密文用途，不影响解密
_DESCRIPTION = "AegisSecret"

#: CRYPTPROTECT_UI_FORBIDDEN：禁止任何 UI 提示（服务/后台进程必须）
CRYPTPROTECT_UI_FORBIDDEN = 0x01

#: CRYPTPROTECT_LOCAL_MACHINE：绑机器而非账号（我们不用，默认绑账号）
CRYPTPROTECT_LOCAL_MACHINE = 0x04


class DpapiUnavailable(Exception):
    """DPAPI 在当前平台/环境不可用"""


class DpapiError(Exception):
    """DPAPI 加解密失败"""


class _DATA_BLOB(ctypes.Structure):
    """对应 Win32 DATA_BLOB"""
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def is_available() -> bool:
    """当前平台是否具备 DPAPI 能力"""
    if not sys.platform.startswith("win"):
        return False
    try:
        ctypes.WinDLL("crypt32.dll")  # noqa: F841 - 仅探测可加载性
        return True
    except (OSError, AttributeError):
        return False


def _crypt32():
    """加载 crypt32.dll 并声明函数原型"""
    if not sys.platform.startswith("win"):
        raise DpapiUnavailable("DPAPI 仅在 Windows 平台可用")
    try:
        dll = ctypes.WinDLL("crypt32.dll")
    except (OSError, AttributeError) as e:
        raise DpapiUnavailable("无法加载 crypt32.dll：%s" % e)

    dll.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR,
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    dll.CryptProtectData.restype = wintypes.BOOL
    dll.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(wintypes.LPCWSTR),
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    dll.CryptUnprotectData.restype = wintypes.BOOL
    dll.LocalFree.argtypes = [ctypes.c_void_p]
    dll.LocalFree.restype = ctypes.c_void_p
    return dll


class _BlobHolder:
    """
    持有 DATA_BLOB 及其底层缓冲区。

    必须把 buffer 与 blob 绑在同一个对象上：ctypes.byref(blob) 只借用指针，
    不会延长 buffer 的生命周期，分开持有会导致释放后使用（use-after-free）。
    """

    def __init__(self, raw: bytes):
        self._buf = ctypes.create_string_buffer(raw, len(raw))
        self.blob = _DATA_BLOB()
        self.blob.cbData = len(raw)
        self.blob.pbData = ctypes.cast(self._buf,
                                       ctypes.POINTER(ctypes.c_char))


def _blob_to_bytes(blob: _DATA_BLOB) -> bytes:
    """把 DATA_BLOB 拷成 Python bytes"""
    if not blob.cbData or not blob.pbData:
        return b""
    return ctypes.string_at(blob.pbData, blob.cbData)


def _free(dll, blob: _DATA_BLOB) -> None:
    """释放由 DPAPI 分配的输出缓冲区"""
    if blob.pbData:
        try:
            dll.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))
        except (OSError, ctypes.ArgumentError):
            pass


def protect(data: bytes, entropy: Optional[bytes] = None) -> bytes:
    """
    用当前 Windows 账号加密数据。

    :param data: 明文字节
    :param entropy: 可选的附加熵（额外保护因子），两端必须一致
    :return: 密文字节
    :raises DpapiUnavailable: 平台不支持
    :raises DpapiError: 加密失败
    """
    if not isinstance(data, (bytes, bytearray)):
        raise DpapiError("protect() 只接受字节数据")
    if not data:
        return b""

    dll = _crypt32()
    in_holder = _BlobHolder(bytes(data))
    out_blob = _DATA_BLOB()

    ent_holder = _BlobHolder(bytes(entropy)) if entropy else None

    ok = dll.CryptProtectData(
        ctypes.byref(in_holder.blob),
        _DESCRIPTION,
        ctypes.byref(ent_holder.blob) if ent_holder else None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise DpapiError("CryptProtectData 调用失败（错误码 %d）"
                         % ctypes.get_last_error())
    try:
        return _blob_to_bytes(out_blob)
    finally:
        _free(dll, out_blob)


def unprotect(blob: bytes, entropy: Optional[bytes] = None) -> bytes:
    """
    用当前 Windows 账号解密数据（换账号/换机器会失败）。

    :param blob: 密文字节
    :param entropy: 与 protect 时一致的附加熵
    :return: 明文字节
    :raises DpapiUnavailable: 平台不支持
    :raises DpapiError: 解密失败（含换账号/换机器场景）
    """
    if not isinstance(blob, (bytes, bytearray)):
        raise DpapiError("unprotect() 只接受字节数据")
    if not blob:
        return b""

    dll = _crypt32()
    in_holder = _BlobHolder(bytes(blob))
    out_blob = _DATA_BLOB()

    ent_holder = _BlobHolder(bytes(entropy)) if entropy else None

    ok = dll.CryptUnprotectData(
        ctypes.byref(in_holder.blob),
        None,
        ctypes.byref(ent_holder.blob) if ent_holder else None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise DpapiError("CryptUnprotectData 调用失败（错误码 %d）"
                         % ctypes.get_last_error())
    try:
        return _blob_to_bytes(out_blob)
    finally:
        _free(dll, out_blob)
