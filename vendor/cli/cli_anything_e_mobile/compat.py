"""版本兼容层:识别 E-Mobile 客户端版本,选出对应的能力档案。

实测事实(2026-09-24,开发机 + 正式环境):
  * 开发机 E-Mobile 7.2.6,登录态存在 localStorage 的 `nc-current-info`
  * 正式环境 E-Mobile 7.1.4,键名是 `gn-current-info` / `en-current-info`
    (字节级比对:`nc-current-info`、`currentUser`、`access_token`、`base_user_id`
     在 7.1.4 的 leveldb 里均未命中)
  * 正式环境 exe 在 `C:\\软件\\E-Mobile\\E-Mobile\\E-Mobile.exe`,不在传统 Program Files 下

本模块只做两件事:
  1. 探测版本(环境变量覆盖 > exe FileVersion > emlogs 里的 [system version])
  2. 给出该版本的能力档案(要试哪些键、用什么 UA、DOM 候选选择器有哪些)

设计原则:**不写死版本常量,而是"探测 → 选档案 → 逐个候选试 → 失败就说清缺什么"**。
未知版本不抛异常,返回 unknown 档案(候选并集),由调用方决定是否继续。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "ClientVersion",
    "VersionProfile",
    "PROFILES",
    "UNKNOWN_PROFILE",
    "detect_client_version",
    "detect_server_version",
    "select_profile",
    "profile_for",
]


# --------------------------------------------------------------------------- #
# 版本号
# --------------------------------------------------------------------------- #
_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def _to_version(value: Any) -> Optional[Tuple[int, ...]]:
    """把任意输入转成版本元组;认不出返回 None。

    支持 ClientVersion 实例 / 字符串("7.1.4") / 数字序列(7,1,4)。
    """
    if value is None:
        return None
    # ClientVersion 实例:直接取 parts(避免再走正则丢 patch)
    if isinstance(value, ClientVersion):
        return value.parts
    if isinstance(value, (tuple, list)):
        try:
            return tuple(int(x) for x in value)
        except (TypeError, ValueError):
            return None
    m = _VERSION_RE.search(str(value))
    if not m:
        return None
    # 只取实际出现的段,不做 "7.2" → (7,2,0) 的补零:
    # 补零会让 "7.2" 永远匹配不上 (7,2,6),档案选择全落 unknown。
    return tuple(int(x) for x in m.groups() if x is not None)


def _cmp(a: Tuple[int, ...], b: Tuple[int, ...]) -> int:
    for x, y in zip(a, b):
        if x != y:
            return -1 if x < y else 1
    return 0


def _ge(a: Tuple[int, ...], b: Tuple[int, ...]) -> bool:
    return _cmp(a, b) >= 0


def _le(a: Tuple[int, ...], b: Tuple[int, ...]) -> bool:
    return _cmp(a, b) <= 0


def _lt(a: Tuple[int, ...], b: Tuple[int, ...]) -> bool:
    return _cmp(a, b) < 0


@dataclass(frozen=True)
class ClientVersion:
    """客户端版本号:可比较、可渲染。"""

    parts: Tuple[int, ...]
    raw: str

    @classmethod
    def parse(cls, raw: str) -> Optional["ClientVersion"]:
        norm = _to_version(raw)
        if norm is None:
            return None
        return cls(parts=norm, raw=str(raw).strip())

    @property
    def minor(self) -> int:
        return self.parts[1] if len(self.parts) > 1 else 0

    @property
    def major(self) -> int:
        return self.parts[0] if self.parts else 0

    @property
    def patch(self) -> int:
        return self.parts[2] if len(self.parts) > 2 else 0

    def at_least(self, other: str) -> bool:
        o = _to_version(other)
        return o is not None and _ge(self.parts, o)

    def at_most(self, other: str) -> bool:
        o = _to_version(other)
        return o is not None and _le(self.parts, o)

    def below(self, other: str) -> bool:
        o = _to_version(other)
        return o is not None and _lt(self.parts, o)

    def matches(self, pattern: str) -> bool:
        """按档案名匹配。支持前缀匹配("7.1" 匹配 7.1.4/7.1.9)
        与全等匹配("7.2.6" 匹配 7.2.6)。"""
        want = _to_version(pattern)
        if want is None:
            return False
        return self.parts[: len(want)] == want

    def __str__(self) -> str:
        return ".".join(str(p) for p in self.parts)


# --------------------------------------------------------------------------- #
# 能力档案
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VersionProfile:
    """某一客户端版本的能力集合。

    localstorage_token_keys: 按优先级排列的 localStorage 键名候选。
    dom: 功能名 → 选择器候选列表(按优先级)。
    user_agent: 该版本对外报告的 UA。
    supports_node_require: GUI 拖放是否依赖页面里的 require('fs')。
    """

    name: str
    localstorage_token_keys: Tuple[str, ...]
    user_agent: str
    dom: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    supports_node_require: bool = True
    notes: str = ""


PROFILES: Dict[str, VersionProfile] = {
    # 正式环境实测:gn-current-info / en-current-info
    "7.1": VersionProfile(
        name="7.1",
        localstorage_token_keys=("gn-current-info", "en-current-info"),
        user_agent="E-Mobile/7.1.4",
        dom={
            "editor": (".ql-editor", '[contenteditable="true"]'),
            "chat_list": (".chat-list-wrapper",
                          '[class*="chat-list-wrapper"]',
                          '[class*="chat-list"]',
                          '[class*="session-list"]'),
            "drop_zone": (".oli-drop-area", '[class*="drop-area"]'),
            "send_button": ('button:text-is("发送")',
                            'button:has-text("发送")',
                            '[class*="send"]'),
        },
        supports_node_require=True,
        notes="正式环境 E-Mobile 7.1.4 实测;DOM 未在 7.1.4 上验证,仅候选化待探测",
    ),
    # 开发机实测:nc-current-info
    "7.2": VersionProfile(
        name="7.2",
        localstorage_token_keys=("nc-current-info",),
        user_agent="E-Mobile/7.2.6",
        dom={
            "editor": (".ql-editor", '[contenteditable="true"]'),
            "chat_list": (".chat-list-wrapper",
                          '[class*="chat-list-wrapper"]'),
            "drop_zone": (".oli-drop-area", '[class*="drop-area"]'),
            "send_button": ('button:text-is("发送")',
                            'button:has-text("发送")'),
        },
        supports_node_require=True,
        notes="开发机 E-Mobile 7.2.6 实测;原有硬编码行为",
    ),
    # 未识别版本:全候选并集,不猜,交由探测报告
    "unknown": VersionProfile(
        name="unknown",
        localstorage_token_keys=("nc-current-info", "gn-current-info",
                                 "en-current-info"),
        user_agent="E-Mobile/unknown",
        dom={
            "editor": (".ql-editor", '[contenteditable="true"]',
                       '[contenteditable=""][role="textbox"]'),
            "chat_list": (".chat-list-wrapper",
                          '[class*="chat-list-wrapper"]',
                          '[class*="chat-list"]',
                          '[class*="session-list"]',
                          '[class*="conversation-list"]'),
            "drop_zone": (".oli-drop-area", '[class*="drop-area"]'),
            "send_button": ('button:text-is("发送")',
                            'button:has-text("发送")',
                            '[class*="send"]'),
        },
        supports_node_require=True,
        notes="未能识别客户端版本;使用候选并集,尽量跑通",
    ),
}

UNKNOWN_PROFILE = PROFILES["unknown"]


def select_profile(version: Any) -> VersionProfile:
    """按 major.minor 选档案;识别不出回 unknown(不抛异常)。

    版本号可能是 ClientVersion、字符串或元组 —— 统一走 `_to_version`。
    """
    ver = _to_version(version)
    if ver is None:
        return UNKNOWN_PROFILE
    for name in sorted(PROFILES, reverse=True):
        if name == "unknown":
            continue
        want = _to_version(name)
        if want and ver[: len(want)] == want:
            return PROFILES[name]
    return UNKNOWN_PROFILE


def profile_for(version: Any) -> VersionProfile:
    """select_profile 的别名,便于调用方按语义取名。"""
    return select_profile(version)


# --------------------------------------------------------------------------- #
# 版本探测
# --------------------------------------------------------------------------- #
def detect_client_version(exe_path: Optional[str] = None,
                          user_data_dir: Optional[str] = None) -> Optional[ClientVersion]:
    """识别 E-Mobile 客户端版本。优先级:

        1. 环境变量 EMCLI_CLIENT_VERSION(最强覆盖)
        2. exe 的 VersionInfo.FileVersion
        3. emlogs 里最近一条 [system version]: X
    取不到返回 None(由上层用 unknown 档案)。
    """
    # 1) 手动覆盖
    forced = (os.environ.get("EMCLI_CLIENT_VERSION") or "").strip()
    if forced:
        parsed = ClientVersion.parse(forced)
        if parsed:
            return parsed

    # 2) exe 版本
    if exe_path:
        raw = _exe_file_version(exe_path)
        if raw:
            parsed = ClientVersion.parse(raw)
            if parsed:
                return parsed

    # 3) 日志
    logs_dir = user_data_dir or _default_user_data_dir()
    if logs_dir:
        raw = _latest_logged_version(logs_dir)
        if raw:
            parsed = ClientVersion.parse(raw)
            if parsed:
                return parsed
    return None


def _exe_file_version(exe_path: str) -> Optional[str]:
    """读 Windows exe 的 FileVersion。非 Windows / 读不到返回 None。"""
    if not exe_path or not os.path.isfile(exe_path):
        return None
    try:  # Windows
        import subprocess

        script = (
            "(Get-Item -LiteralPath '{p}').VersionInfo.FileVersion"
        ).format(p=exe_path.replace("'", "''"))
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=15)
        raw = (out.stdout or "").strip()
        if raw:
            return raw
    except Exception:  # noqa: BLE001
        pass
    try:  # Ctypes 方案(不走 powershell)
        import ctypes

        size = ctypes.windll.version.GetFileVersionInfoSizeW(exe_path, None)
        if not size:
            return None
        raw = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(
                exe_path, None, size, raw):
            return None
        u_len = ctypes.c_uint()
        lplp_buffer = ctypes.c_void_p()
        if not ctypes.windll.version.VerQueryValueW(
                raw, "\\StringFileInfo\\040904b0\\FileVersion",
                ctypes.byref(lplp_buffer), ctypes.byref(u_len)):
            # 简体中文 locale code page 040904b4 / 英文 040904e4 兜底
            for cp in ("040904b4", "040904e4"):
                if ctypes.windll.version.VerQueryValueW(
                        raw, "\\StringFileInfo\\" + cp + "\\FileVersion",
                        ctypes.byref(lplp_buffer), ctypes.byref(u_len)):
                    break
            else:
                return None
        text = ctypes.wstring_at(lplp_buffer, u_len.value - 1
                                 if u_len.value else 0)
        text = (text or "").strip()
        if text:
            return text
    except Exception:  # noqa: BLE001
        pass
    return None


def _default_user_data_dir() -> Optional[str]:
    return os.environ.get("APPDATA") and os.path.join(
        os.environ["APPDATA"], "E-Mobile") or None


_LOG_VERSION_RE = re.compile(r"\[system version\]\s*[:：]\s*([0-9][0-9.]*)")


def _latest_logged_version(user_data_dir: str) -> Optional[str]:
    """扫 emlogs 下最近的日志,取 [system version]。"""
    import glob

    emlogs = os.path.join(user_data_dir, "emlogs")
    if not os.path.isdir(emlogs):
        return None
    files: List[str] = []
    for pattern in ("*/*.log", "*.log"):
        files.extend(glob.glob(os.path.join(emlogs, pattern)))
    if not files:
        return None
    files.sort(key=lambda p: (os.path.getmtime(p), p), reverse=True)
    for path in files[:8]:
        try:
            text = _read_tail(path, 200 * 1024)
        except OSError:
            continue
        m = _LOG_VERSION_RE.findall(text)
        if m:
            return m[-1]
    return None


def _read_tail(path: str, limit: int) -> str:
    with open(path, "rb") as fh:
        try:
            fh.seek(0, os.SEEK_END)
        except OSError:
            fh.seek(0)
        else:
            start = max(0, fh.tell() - limit)
            fh.seek(start)
        data = fh.read()
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def detect_server_version(base_url: str, token: str) -> Optional[str]:
    """从 /emp/passport/getsetting 读服务端版本(仅报告,不用于分支)。"""
    if not base_url or not token:
        return None
    try:
        import requests
    except ImportError:  # pragma: no cover
        return None
    try:
        resp = requests.get(
            base_url.rstrip("/") + "/emp/passport/getsetting",
            params={"getTime": int(__import__("time").time() * 1000)},
            headers={"access_token": token, "emaccesstk": token},
            timeout=10)
        data = resp.json()
        return str(data.get("server_version") or "") or None
    except Exception:  # noqa: BLE001
        return None
