"""凭证加密存储:用 Windows DPAPI 保护账号密码,避免明文落盘。

为什么需要它
------------
自动登入要读账号密码,但**明文放 `.env` 是不行的**(本机实测就踩到过:
文件在项目目录里、无 git 保护,一旦被同步或误传就等于泄露)。

方案:Windows **DPAPI**(`CryptProtectData` / `CryptUnprotectData`)
----------------------------------------------------------------
* 加密密钥由 Windows 按**当前登录用户**派生并保管,不落在文件里 ——
  复制走密文文件到别的机器/别的 Windows 账号下**解不开**;
* 不需要我们保存任何主密钥(没有"密钥和密文放一起"的问题);
* 系统自带,pywin32 或 ctypes 都能调,无额外依赖。

文件格式(向后兼容)
-------------------
`.env` 里每个**值**单独加密,键名仍是明文(便于人肉确认有哪些字段):

    # EMCLI-ENC v1 (Windows DPAPI)
    账号: enc:v1:Base64(DPAPI密文)
    密码: enc:v1:Base64(DPAPI密文)

* 读到 `enc:v1:` 前缀 → 解密;
* 读到明文值 → 直接使用(**兼容老文件**),但可用 `emcli login protect`
  一键把它转成加密形式,并提示不要再留明文。
* 前缀带版本号,将来换算法不影响老文件。

非 Windows / DPAPI 不可用时
---------------------------
`dpapi_available()` 返回 False,此时 `encrypt_value` 直接报错(而不是
静默退回明文),避免"以为加密了其实是明文"。读取侧仍能处理明文文件,
保证在没 DPAPI 的环境里老文件还能用。
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wintypes
import sys
from typing import Callable, List, Optional

#: 密文值的前缀(含版本号);改算法时递增
ENC_PREFIX = "enc:v1:"

#: DPAPI 附加熵:让密文只对"本工具 + 本用户"有意义
_ENTROPY = b"xrm-emobile-cli/credential/v1"

_IS_WINDOWS = sys.platform == "win32"


class CryptoError(RuntimeError):
    """加解密失败(不含任何明文/密文内容)。"""


# --------------------------------------------------------------------------- #
# DPAPI(优先 pywin32,回落 ctypes;都不可用则视为不支持)
# --------------------------------------------------------------------------- #
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_from(data: bytes) -> _DataBlob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _blob_to_bytes(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _dpapi_ctypes(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    blob_in = _blob_from(data)
    blob_entropy = _blob_from(_ENTROPY)
    blob_out = _DataBlob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN:绝不弹对话框

    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, ctypes.byref(blob_entropy),
            None, None, flags, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, ctypes.byref(blob_entropy),
            None, None, flags, ctypes.byref(blob_out))

    if not ok:
        raise CryptoError(
            "DPAPI 调用失败(密文可能来自别的 Windows 账号或别的机器)")
    try:
        return _blob_to_bytes(blob_out)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_pywin32(data: bytes, protect: bool) -> bytes:  # pragma: no cover
    import win32crypt  # type: ignore

    if protect:
        return win32crypt.CryptProtectData(data, None, _ENTROPY, None, None, 0)
    return win32crypt.CryptUnprotectData(data, _ENTROPY, None, None, 0)[1]


def _dpapi(data: bytes, protect: bool) -> bytes:
    """优先 pywin32(更稳),失败回落 ctypes;两条路都不行才报错。"""
    errors: List[str] = []

    pywin32_route = _pywin32_impl()
    if pywin32_route is not None:
        try:
            return pywin32_route(data, protect)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"pywin32:{type(exc).__name__}")

    try:
        return _dpapi_ctypes(data, protect)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"ctypes:{type(exc).__name__}")
    raise CryptoError("DPAPI 不可用(" + "、".join(errors) + ")")


def _pywin32_impl() -> Optional[Callable]:
    """返回 pywin32 的加解密函数;没装则返回 None(不装也能跑)。"""
    try:
        import win32crypt  # type: ignore
    except ImportError:
        return None

    def _call(data: bytes, protect: bool) -> bytes:
        if protect:
            return win32crypt.CryptProtectData(data, None, _ENTROPY,
                                               None, None, 0)
        return win32crypt.CryptUnprotectData(data, _ENTROPY, None, None, 0)[1]

    return _call


def dpapi_available() -> bool:
    """本机是否能做 DPAPI 加解密(只读自检,不抛异常)。"""
    if not _IS_WINDOWS:
        return False
    try:
        return _dpapi(b"probe", True) != b"probe"
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# 值级加解密
# --------------------------------------------------------------------------- #
def encrypt_value(plain: str) -> str:
    """把明文值加密成 `enc:v1:<base64>`。

    DPAPI 不可用时**报错**,不静默退回明文(否则会让人以为已经加密)。
    """
    if not _IS_WINDOWS:
        raise CryptoError(
            "凭证加密依赖 Windows DPAPI,当前平台不支持。"
            "请改用环境变量注入凭证,或把明文文件放在受控位置。")
    if not dpapi_available():
        raise CryptoError(
            "本机 DPAPI 不可用,无法加密凭证。"
            "请安装 pywin32(`pip install pywin32`)后重试。")
    blob = _dpapi(plain.encode("utf-8"), protect=True)
    return ENC_PREFIX + base64.b64encode(blob).decode("ascii")


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


def decrypt_value(value: str) -> str:
    """解密 `enc:v1:` 值;非密文原样返回(兼容明文老文件)。"""
    if not is_encrypted(value):
        return value
    if not _IS_WINDOWS:
        raise CryptoError("凭证是 Windows DPAPI 加密的,当前平台无法解密")
    payload = value[len(ENC_PREFIX):].strip()
    try:
        blob = base64.b64decode(payload, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(f"密文不是合法 Base64({type(exc).__name__})") from exc
    try:
        return _dpapi(blob, protect=False).decode("utf-8")
    except CryptoError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(f"解密失败({type(exc).__name__})") from exc
