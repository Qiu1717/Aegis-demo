# -*- coding: utf-8 -*-
"""
apps/security/keystore.py — 密钥托管（分层后端，AEG-K02）

后端优先级：
    ① keyring    Windows → 凭据管理器(WinVault)；macOS → Keychain（**首选**）
    ② dpapi      Windows 用户态 CryptProtectData，密文落 <data_dir>/secrets.enc
    ③ fallback   机器标识派生 + 随机 salt 的对称混淆（**保护级别低**，UI 必须标注）

统一接口（上层不感知平台差异）：
    save_key(provider_key, secret) -> bool
    load_key(provider_key)         -> str      # 失败返回 ""，绝不抛异常到上层
    delete_key(provider_key)       -> bool
    list_keys()                    -> {provider_key: has_key}
    backend_name()                 -> "keyring" | "dpapi" | "fallback"

硬约束：
    · 密钥只在本机凭据区与内存流转，绝不写入 config.json / config.user.json / 日志；
    · 从本模块读出的任何明文立即登记到 redact，保证日志中搜不到完整 Key；
    · 所有失败都被吞掉并返回安全的空值，绝不把异常抛给路由层。
"""
import base64
import getpass
import hashlib
import hmac
import json
import os
import platform
import sys
import uuid
from typing import Dict, List, Optional

from .. import paths
from . import redact

# ── 常量 ──────────────────────────────────────────────────
#: keyring 的 service 名（Windows 凭据管理器 / macOS Keychain 中可见）
SERVICE_NAME = "Aegis"
#: keyring 中 username 的前缀
USERNAME_PREFIX = "aegis:"

#: 后端名称
BACKEND_KEYRING = "keyring"
BACKEND_DPAPI = "dpapi"
BACKEND_FALLBACK = "fallback"

#: 保护级别
LEVEL_HIGH = "high"
LEVEL_LOW = "low"

#: 回退后端的派生参数
_PBKDF2_ROUNDS = 120_000
_SALT_BYTES = 16

_keyring_module = None       # type: Optional[object]
_keyring_checked = False


# ── 后端可用性探测 ────────────────────────────────────────
def _keyring():
    """惰性导入 keyring（较重依赖 + 可能触发后端探测，只在真正用到时加载）"""
    global _keyring_module, _keyring_checked
    if _keyring_checked:
        return _keyring_module
    _keyring_checked = True
    try:
        import keyring  # type: ignore
        _keyring_module = keyring
    except Exception:
        _keyring_module = None
    return _keyring_module


def _dpapi():
    """惰性导入 dpapi（ctypes，非 Windows 平台不可用）"""
    try:
        from . import dpapi
        return dpapi if dpapi.is_available() else None
    except Exception:
        return None


def _ordered_backends(preferred: Optional[str] = None) -> List[str]:
    """后端尝试顺序：preferred 优先，其余按 keyring → dpapi → fallback"""
    order = [BACKEND_KEYRING, BACKEND_DPAPI, BACKEND_FALLBACK]
    if preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    return order


def backend_name() -> str:
    """当前环境下**首选**使用的后端名称（不触发真实读写）"""
    if _keyring():
        return BACKEND_KEYRING
    if _dpapi():
        return BACKEND_DPAPI
    return BACKEND_FALLBACK


def protection_level(backend: Optional[str] = None) -> str:
    """保护级别：keyring/dpapi 为 high，末位回退为 low（UI 需明确标注）"""
    name = backend or backend_name()
    return LEVEL_LOW if name == BACKEND_FALLBACK else LEVEL_HIGH


# ── 索引文件（只记录 provider_key 与所用后端，不含任何密钥）──
def _index_read() -> Dict[str, dict]:
    try:
        p = paths.key_index_path()
        if not p.is_file():
            return {}
        with open(str(p), "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("providers") if isinstance(data, dict) else None
        return items if isinstance(items, dict) else {}
    except (OSError, ValueError):
        return {}


def _index_write(items: Dict[str, dict]) -> bool:
    try:
        paths.ensure_dirs()
        p = paths.key_index_path()
        with open(str(p), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "providers": items},
                      f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def _index_set(provider_key: str, backend: str) -> None:
    items = _index_read()
    items[provider_key] = {"backend": backend}
    _index_write(items)


def _index_drop(provider_key: str) -> None:
    items = _index_read()
    if provider_key in items:
        items.pop(provider_key, None)
        _index_write(items)


# ── 文件型后端（dpapi / fallback）─────────────────────────
def _file_read() -> dict:
    """读取密文文件：{"backend":..., "salt":..., "items": {key: b64}}"""
    try:
        p = paths.secrets_path()
        if not p.is_file():
            return {}
        with open(str(p), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _file_write(backend: str, items: Dict[str, str], salt: str = "") -> bool:
    try:
        paths.ensure_dirs()
        p = paths.secrets_path()
        payload = {"version": 1, "backend": backend, "salt": salt, "items": items}
        with open(str(p), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        if not sys.platform.startswith("win"):
            try:
                os.chmod(str(p), 0o600)
            except OSError:
                pass
        return True
    except OSError:
        return False


def _machine_seed() -> str:
    """机器 + 用户标识（用于末位回退后端的密钥派生，换机即失效）"""
    parts = [
        platform.node() or "",
        platform.machine() or "",
        str(uuid.getnode()),
    ]
    try:
        parts.append(getpass.getuser() or "")
    except Exception:
        pass
    for env_key in ("USERNAME", "USER", "COMPUTERNAME", "USERDOMAIN"):
        parts.append(os.environ.get(env_key, ""))
    return "|".join(parts)


def _derive(seed: str, salt: bytes) -> bytes:
    """从机器标识 + salt 派生对称密钥材料"""
    return hashlib.pbkdf2_hmac(
        "sha256", seed.encode("utf-8"), salt, _PBKDF2_ROUNDS)


def _xor_stream(key_material: bytes, salt: bytes, data: bytes) -> bytes:
    """HMAC-SHA256 计数器模式密钥流（对称、可还原，仅作混淆级保护）"""
    out = bytearray()
    counter = 0
    pos = 0
    while pos < len(data):
        block = hmac.new(key_material, salt + counter.to_bytes(8, "big"),
                         hashlib.sha256).digest()
        chunk = data[pos:pos + len(block)]
        out.extend(b ^ k for b, k in zip(chunk, block))
        pos += len(block)
        counter += 1
    return bytes(out)


def _fallback_encrypt(items: Dict[str, str], salt: bytes) -> Dict[str, str]:
    material = _derive(_machine_seed(), salt)
    plain = json.dumps(items, ensure_ascii=False).encode("utf-8")
    return {"blob": base64.b64encode(_xor_stream(material, salt, plain)).decode("ascii")}


def _fallback_decrypt(payload: dict, salt: bytes) -> Dict[str, str]:
    material = _derive(_machine_seed(), salt)
    cipher = base64.b64decode(payload.get("blob") or "")
    plain = _xor_stream(material, salt, cipher)
    data = json.loads(plain.decode("utf-8"))
    return data if isinstance(data, dict) else {}


# ── 各后端的读写实现 ──────────────────────────────────────
def _username_of(provider_key: str) -> str:
    return "%s%s" % (USERNAME_PREFIX, provider_key)


def _keyring_save(provider_key: str, secret: str) -> bool:
    kr = _keyring()
    if not kr:
        return False
    kr.set_password(SERVICE_NAME, _username_of(provider_key), secret)
    return True


def _keyring_load(provider_key: str) -> str:
    kr = _keyring()
    if not kr:
        return ""
    return kr.get_password(SERVICE_NAME, _username_of(provider_key)) or ""


def _keyring_delete(provider_key: str) -> bool:
    kr = _keyring()
    if not kr:
        return False
    try:
        kr.delete_password(SERVICE_NAME, _username_of(provider_key))
    except Exception:
        # 凭据不存在时部分后端会抛异常，视作删除成功
        return True
    return True


def _read_all(backend: str) -> Dict[str, str]:
    """读取某个文件型后端的全部条目（明文 dict）"""
    payload = _file_read()
    if not payload or not payload.get("items"):
        return {}
    if payload.get("backend") != backend:
        return {}
    try:
        salt = base64.b64decode(payload.get("salt") or "")
    except Exception:
        salt = b""
    items = payload.get("items") or {}

    if backend == BACKEND_DPAPI:
        mod = _dpapi()
        if not mod:
            return {}
        plain = {}
        for k, v in items.items():
            try:
                plain[k] = mod.unprotect(base64.b64decode(v)).decode("utf-8")
            except Exception:
                continue
        return plain

    if backend == BACKEND_FALLBACK:
        try:
            return _fallback_decrypt(items, salt)
        except Exception:
            return {}
    return {}


def _write_all(backend: str, plain: Dict[str, str]) -> bool:
    """把全部条目写回某个文件型后端"""
    if backend == BACKEND_DPAPI:
        mod = _dpapi()
        if not mod:
            return False
        items = {}
        for k, v in plain.items():
            try:
                items[k] = base64.b64encode(
                    mod.protect(v.encode("utf-8"))).decode("ascii")
            except Exception:
                return False
        return _file_write(backend, items)

    if backend == BACKEND_FALLBACK:
        import secrets as _secrets
        salt = _secrets.token_bytes(_SALT_BYTES)
        items = _fallback_encrypt(plain, salt)
        return _file_write(backend, items, base64.b64encode(salt).decode("ascii"))

    return False


def _file_save(provider_key: str, secret: str, backend: str) -> bool:
    target = _read_all(backend)
    # 兼容历史数据：如果目标后端文件为空但另一个文件型后端有数据，先迁移过来
    if not target:
        other = BACKEND_FALLBACK if backend == BACKEND_DPAPI else BACKEND_DPAPI
        target = _read_all(other)
    target[provider_key] = secret
    return _write_all(backend, target)


def _file_delete(provider_key: str, backend: str) -> bool:
    target = _read_all(backend)
    if provider_key not in target:
        return True
    target.pop(provider_key, None)
    return _write_all(backend, target)


# ── 对外接口 ──────────────────────────────────────────────
def save_key(provider_key: str, secret: str) -> bool:
    """
    保存（或覆盖）一个服务商的密钥。

    :return: 是否成功；失败时调用方应提示用户「本机密钥库不可用」
    """
    if not provider_key or not secret:
        return False
    secret = secret.strip()
    if not secret:
        return False

    for backend in _ordered_backends():
        try:
            if backend == BACKEND_KEYRING:
                ok = _keyring_save(provider_key, secret)
            else:
                ok = _file_save(provider_key, secret, backend)
            if ok:
                _index_set(provider_key, backend)
                # 明文一旦进入内存立即登记，保证日志脱敏
                redact.register_secret(secret)
                return True
        except Exception:
            continue
    return False


def load_key(provider_key: str) -> str:
    """
    读取一个服务商的密钥。

    :return: 明文密钥；**任何失败都返回空串**，绝不抛异常到上层
    """
    if not provider_key:
        return ""

    recorded = (_index_read().get(provider_key) or {}).get("backend")
    for backend in _ordered_backends(recorded):
        try:
            if backend == BACKEND_KEYRING:
                value = _keyring_load(provider_key)
            else:
                value = _read_all(backend).get(provider_key) or ""
            if value:
                redact.register_secret(value)
                return value
        except Exception:
            continue

    # 索引缺失（如手工拷贝了数据目录）时，兜底扫一遍文件型后端
    for backend in (BACKEND_DPAPI, BACKEND_FALLBACK):
        if recorded and backend == recorded:
            continue
        try:
            value = _read_all(backend).get(provider_key) or ""
            if value:
                _index_set(provider_key, backend)
                redact.register_secret(value)
                return value
        except Exception:
            continue
    return ""


def delete_key(provider_key: str) -> bool:
    """删除一个服务商的密钥（尽力而为，全部后端都清一遍）"""
    if not provider_key:
        return False
    ok = False
    for backend in _ordered_backends():
        try:
            if backend == BACKEND_KEYRING:
                if _keyring_delete(provider_key):
                    ok = True
            else:
                if _file_delete(provider_key, backend):
                    ok = True
        except Exception:
            continue
    _index_drop(provider_key)
    return ok


def list_keys() -> Dict[str, bool]:
    """
    返回 {provider_key: 是否已存过密钥}。

    只做「是否存在」判定，**不读出明文**，避免无谓地把密钥拉进内存。
    """
    result: Dict[str, bool] = {}
    for provider_key in _index_read().keys():
        result[provider_key] = True
    # 兼容：索引丢失但密文文件里还有数据的情况
    for backend in (BACKEND_DPAPI, BACKEND_FALLBACK):
        try:
            for provider_key in _read_all(backend).keys():
                result[provider_key] = True
        except Exception:
            continue
    return result


def backend_of(provider_key: str) -> str:
    """某个密钥实际所用的后端（未存过则返回当前首选后端）"""
    recorded = (_index_read().get(provider_key) or {}).get("backend")
    return recorded or backend_name()


def describe() -> dict:
    """供设置页展示的密钥库概况（不含任何密钥）"""
    name = backend_name()
    return {
        "backend": name,
        "backend_label": {
            BACKEND_KEYRING: "系统凭据管理器（推荐）",
            BACKEND_DPAPI: "Windows DPAPI（绑定当前账号）",
            BACKEND_FALLBACK: "本机标识派生（保护级别低）",
        }.get(name, name),
        "protection": protection_level(name),
        "protection_low": protection_level(name) == LEVEL_LOW,
        "providers": sorted(list_keys().keys()),
    }
