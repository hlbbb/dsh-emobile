"""E-Mobile 登录页自动登录(账号 + 密码 + 图形验证码)。

存在意义
--------
E-Mobile 是单点互踢:同一账号在别处登录会把本机顶掉,服务端随即对
`/emp/api/msg/gettoken` 返回 `errcode=42001`(errmsg 形如
"If this is not your operation, please modify your login password in time")。
此时 OA 群发送/读取全部失效,必须重新登录一次。本模块把这一步自动化。

唯一卡点:图片验证码
--------------------
`{base}/emp/captcha?key=<uuid>` 每次 GET 都会**生成一张新验证码**,所以
"重新下载 img.src 再识别"必然和服务端记住的那张不一致 —— 服务端只认
页面**当前显示**的那张图的答案。因此这里用 canvas `drawImage` 取当前
显示像素(`_READ_CAPTCHA_JS`),而不是重新 fetch。

验证码识别率无法做到 100%(实测 ddddocr 的大小写与首字符都不稳),
所以设计成:**识别 → 提交 → 看服务端判据 → 失败就换一张重试**,
而不是"识别一次就当真"。另留 `--code` 人工兜底。

安全约束(与项目凭证规则一致)
------------------------------
* 账号/密码只在进程内存里存在,从不写入日志、配置或输出;
* 任何输出只报告**长度**与**指纹**(djb2),不报告值;
* 验证码是一次性短时凭证,识别结果可在 `--json` 中回传用于排查,
  但不写盘。

选择器与版本
------------
登录表单选择器均按**候选列表**逐个试,与 `compat`/`gui` 的风格一致。
7.2.6 已实测(见 `_LOGIN_DOM`)。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:
    import websocket  # type: ignore
except ImportError:  # pragma: no cover
    websocket = None

from .errors import EMError
from .gui import _pick_main_page

# --------------------------------------------------------------------------- #
# 凭证文件
# --------------------------------------------------------------------------- #
#: 环境变量:指定凭证文件路径(优先级最高)
ENV_CRED_FILE = "EMCLI_LOGIN_ENV"

#: 默认文件名(按序查找:显式/环境变量 → 配置记录 → 当前目录 →
#: 各级父目录 → 本地配置目录;详见 `_iter_cred_candidate_records`)
DEFAULT_CRED_FILES = ("OA账号密码.env", "oa_login.env", ".oa_login.env")

#: 从当前目录向上逐级查找父目录的最大层数。
#: 实测场景:用户的 cwd 常在 `<项目>\A6-cli-anything` 或
#: `<项目>\.agents\skills\xrm-emobile-cli`,而凭证文件放在 `<项目>\` 根,
#: 只查 cwd 必然找不到 —— 向上查父目录正是为此。
CRED_PARENT_LEVELS = 6

#: 凭证文件来源标签(诊断输出用;只描述"从哪一类位置找到的",不含任何值)
CRED_SOURCES: Dict[str, str] = {
    "param": "显式 --env-file",
    "env": f"环境变量 {ENV_CRED_FILE}",
    "config": "配置记录",
    "cwd": "当前工作目录",
    "parent": "上级父目录",
    "app": "本地配置目录",
}

#: 账号 / 密码的键名候选(中英文都认,标签式 `账号: xxx` 也能解析)
USERNAME_KEYS = ("账号", "登录名", "用户名", "登陆名", "account", "username",
                 "loginid", "login_id", "login", "user", "name")
PASSWORD_KEYS = ("密码", "登陆密码", "登录密码", "password", "passwd", "pwd")

#: 解析 `键<分隔符>值` 时的分隔符
_SEPARATORS = (":", "：", "=")

_COMMENT_PREFIXES = ("#", "//", ";", "[")


class LoginError(EMError):
    """自动登录失败。"""


class CredentialError(LoginError):
    """凭证文件缺失或键名不符(报错里**只给路径与键名**,不给值)。"""


@dataclass
class Credentials:
    """账号/密码。`repr` 已脱敏,避免意外回显。"""

    username: str = field(repr=False)
    password: str = field(repr=False)
    source: str = ""
    username_label: str = ""
    password_label: str = ""
    encrypted: bool = False

    def __repr__(self) -> str:  # noqa: D105
        return (f"Credentials(username=<{len(self.username)} chars>, "
                f"password=<{len(self.password)} chars>, "
                f"source={self.source!r}, encrypted={self.encrypted})")

    __str__ = __repr__

    def brief(self) -> Dict[str, Any]:
        """可安全打印的摘要(长度 + 指纹,不含值)。"""
        return {
            "username_len": len(self.username),
            "password_len": len(self.password),
            "username_label": self.username_label,
            "password_label": self.password_label,
            "source": self.source,
            "encrypted": self.encrypted,
        }


def _fingerprint(text: str) -> str:
    """非可逆指纹:用于判断"值有没有变",不用来还原值。"""
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def parse_env_text(text: str) -> Dict[str, str]:
    """把凭证文件正文解析成 `{标签: 值}`。

    支持三种写法(实测的凭证文件用的是 `标签: 值`):
        KEY=value
        账号: value
        密码：value
    忽略空行与注释行。不校验值的语义。
    """
    out: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.lstrip("\ufeff").strip()
        if not line or line.startswith(_COMMENT_PREFIXES):
            continue
        cut = -1
        for sep in _SEPARATORS:
            idx = line.find(sep)
            if idx > 0 and (cut < 0 or idx < cut):
                cut = idx
        if cut <= 0:
            continue
        label = line[:cut].strip().strip('"').strip("'")
        value = line[cut + 1:].strip().strip('"').strip("'")
        if label and value:
            out[label] = value
    return out


def _pick_key(mapping: Dict[str, str], keys: Tuple[str, ...]) -> Tuple[str, str]:
    """按候选键名取第一个命中的 `(标签, 值)`;大小写不敏感。"""
    lowered = {k.lower(): k for k in mapping}
    for want in keys:
        hit = lowered.get(want.lower())
        if hit is not None:
            return hit, mapping[hit]
    return "", ""


def _emcli_home_dir() -> str:
    """CLI 本地目录:`$EMCLI_HOME`,否则 `%LOCALAPPDATA%\\emcli`。

    这里**动态**读 `config.APP_DIR`(而不是在导入时绑定),测试才能用
    monkeypatch 把它隔离到 tmp_path,不会读到真实用户的 token 与白名单。
    """
    from . import config as configmod

    home = (os.environ.get("EMCLI_HOME") or "").strip()
    if home:
        return home
    return str(configmod.APP_DIR)


def _recorded_cred_file() -> str:
    """读配置里记录的凭证文件路径(`Config.login_env_file`)。

    没记过 / 配置读不出来一律返回空串 —— 查找顺序往后走即可,**不报错**。
    """
    from . import config as configmod

    try:
        return str(getattr(configmod.Config.load(), "login_env_file", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def _parent_walk_stop_dir() -> str:
    """向上查找的停止边界:当前用户主目录。

    再往上是 `C:\\Users`、盘根这类**共享**目录,在里面翻凭证文件既没用也不安全,
    所以查到用户主目录(含)就停。抽成函数便于测试打桩。
    """
    return os.path.abspath(os.path.expanduser("~"))


def _iter_parent_dirs(base: str,
                      levels: int = CRED_PARENT_LEVELS) -> List[str]:
    """列出从 `base` 向上最多 `levels` 层的父目录(不含 `base` 自身)。

    遇到盘根或用户主目录即停:用户主目录**本身**会被列出(它归当前用户所有),
    但绝不再往上 —— `C:\\Users`、盘根这类共享目录里翻凭证文件既没用也不安全。
    `base` 已经在用户主目录时直接返回空列表。
    """
    out: List[str] = []
    stop = os.path.normcase(_parent_walk_stop_dir())
    current = os.path.abspath(base)
    for _ in range(max(0, int(levels))):
        if os.path.normcase(current) == stop:
            break                       # 已到用户主目录,不再向上
        parent = os.path.dirname(current)
        if not parent or parent == current:
            break                       # 已到盘根(如 C:\)
        out.append(parent)
        current = parent
    return out


def _iter_cred_candidate_records(path: Optional[str] = None,
                                 cwd: Optional[str] = None,
                                 levels: int = CRED_PARENT_LEVELS,
                                 ignore_config: bool = False
                                 ) -> Iterator[Tuple[str, str]]:
    """按查找顺序产出 `(来源标签, 候选路径)`,每个候选都必须 `isfile` 才算命中。

    顺序(先命中先返回):
      1. 显式参数 `path`;
      2. 环境变量 `EMCLI_LOGIN_ENV`;
      3. 配置里记录的路径(`Config.login_env_file`,上次成功用过的那份);
      4. `cwd` 下的 `DEFAULT_CRED_FILES`;
      5. 从 `cwd` 向上最多 `levels` 层父目录下的 `DEFAULT_CRED_FILES`
         (本机场景的关键:从 `...\\<项目目录>\\A6-cli-anything` 向上能找到
         `...\\<项目目录>\\OA账号密码.env`);
      6. 本地配置目录(`%LOCALAPPDATA%\\emcli`)下的 `DEFAULT_CRED_FILES`。

    1/2 若被**显式指定却不存在**,不再往后找(保持历史语义);
    3 若记录的文件已不存在,静默跳过继续往后找(不报错)。

    `ignore_config=True` 只用于**诊断**:跳过第 3 步,回答"假如没有配置记录,
    这次会命中哪一份"。默认 `False`,查找行为与历史完全一致。

    产出的是**完整**的候选序列(不截断);"实际试到哪一条"由
    `_resolve_cred_candidate` 记录,报错文案与诊断展示都用它。
    """
    if path:
        yield "param", path
        return
    env = (os.environ.get(ENV_CRED_FILE) or "").strip()
    if env:
        yield "env", env
        return

    recorded = _recorded_cred_file()
    if recorded and not ignore_config:
        yield "config", recorded

    base = os.path.abspath(cwd or os.getcwd())
    searched: set = set()

    def _fresh(directory: str) -> bool:
        """同一目录不重复产出(父目录可能与本地配置目录重合)。"""
        key = os.path.normcase(os.path.abspath(directory))
        if key in searched:
            return False
        searched.add(key)
        return True

    groups: List[Tuple[str, str]] = [(base, "cwd")]
    groups += [(d, "parent") for d in _iter_parent_dirs(base, levels)]
    groups.append((_emcli_home_dir(), "app"))
    for directory, source in groups:
        if not _fresh(directory):
            continue
        for name in DEFAULT_CRED_FILES:
            yield source, os.path.join(directory, name)


def _iter_cred_candidates(path: Optional[str] = None,
                          cwd: Optional[str] = None,
                          levels: int = CRED_PARENT_LEVELS,
                          ignore_config: bool = False) -> List[str]:
    """凭证文件的**有序候选路径**列表(列表顺序 = 查找顺序)。

    找不到文件时的报错文案与 `emcli compat` 诊断都基于它,这样"找过哪些位置"
    与实现永远一致,不会出现文案与代码各说各话。
    """
    return [cand for _, cand in _iter_cred_candidate_records(
        path, cwd, levels, ignore_config)]


def _resolve_cred_candidate(path: Optional[str] = None,
                            cwd: Optional[str] = None,
                            ignore_config: bool = False
                            ) -> Tuple[Optional[str], str, List[str]]:
    """返回 `(命中的路径或 None, 来源标签, 实际尝试过的候选路径)`。

    候选按顺序逐个 `isfile` 试;命中即停,**只把试到的那一段**算作"尝试过",
    这样报错文案与诊断里的"找过哪些位置"永远说真话。
    `ignore_config=True` 仅用于诊断(跳过配置记录),不改变默认行为。
    """
    records = list(_iter_cred_candidate_records(path=path, cwd=cwd,
                                                ignore_config=ignore_config))
    for idx, (source, candidate) in enumerate(records):
        if os.path.isfile(candidate):
            return candidate, source, [cand for _, cand in records[:idx + 1]]
    return None, "", [cand for _, cand in records]


def resolve_cred_file(path: Optional[str] = None,
                      cwd: Optional[str] = None) -> Optional[str]:
    """定位凭证文件(只要路径,找不到返回 None)。

    顺序:显式参数 → 环境变量 → 配置记录 → 当前目录 → 各级父目录 → 本地配置目录。
    """
    return _resolve_cred_candidate(path=path, cwd=cwd)[0]


def resolve_cred_file_detail(path: Optional[str] = None,
                             cwd: Optional[str] = None,
                             ignore_config: bool = False) -> Dict[str, Any]:
    """定位凭证文件并给出**来源标签**与尝试过的候选路径(供诊断命令展示)。

    返回的只有**路径与布尔**:`path` / `found` / `source` / `source_label` /
    `candidates_tried` / `config_recorded_path` / `config_recorded_exists` /
    `config_recorded_stale` / `parent_levels`。**不含**账号、密码或任何凭证值。

    另外无条件给出"**忽略配置记录**后的首个命中"
    (`first_hit_ignoring_config` / `first_source_ignoring_config` /
    `first_source_label_ignoring_config`)。这是为了不让配置记录掩盖
    父目录自动查找的回归:登录成功后配置里会记下路径,此后来源恒为
    "配置记录",单看 `source` 永远发现不了父目录查找坏掉。

    `ignore_config=True` 时本次查找本身就跳过配置记录(只读诊断用,
    默认 `False` 时查找行为与历史完全一致)。
    """
    hit, source, candidates = _resolve_cred_candidate(
        path=path, cwd=cwd, ignore_config=ignore_config)
    recorded = _recorded_cred_file()
    recorded_exists = bool(recorded) and os.path.isfile(recorded)

    if ignore_config:
        ignored_hit, ignored_source = hit, source
    else:
        ignored_hit, ignored_source, _ = _resolve_cred_candidate(
            path=path, cwd=cwd, ignore_config=True)

    return {
        "path": hit or "",
        "found": bool(hit),
        "source": source,
        "source_label": CRED_SOURCES.get(source, "(未找到)") if hit else "",
        "candidates_tried": candidates,
        "config_recorded_path": recorded,
        "config_recorded_exists": recorded_exists,
        # 配置里记了路径、但文件已经不在 → 明确告诉调用方"这条记录已失效"
        "config_recorded_stale": bool(recorded) and not recorded_exists,
        "first_hit_ignoring_config": ignored_hit or "",
        "first_source_ignoring_config": ignored_source if ignored_hit else "",
        "first_source_label_ignoring_config": (
            CRED_SOURCES.get(ignored_source, "(未找到)") if ignored_hit else ""),
        "parent_levels": CRED_PARENT_LEVELS,
    }


def resolve_cred_file_ignoring_config(path: Optional[str] = None,
                                      cwd: Optional[str] = None
                                      ) -> Dict[str, Any]:
    """只读诊断:跳过配置记录后,本次会命中哪一份凭证文件。

    等价于 `resolve_cred_file_detail(..., ignore_config=True)`,返回结构相同;
    不改变 `resolve_cred_file` / `load_credentials` 的默认查找顺序。
    """
    return resolve_cred_file_detail(path=path, cwd=cwd, ignore_config=True)


def load_credentials(path: Optional[str] = None,
                     cwd: Optional[str] = None,
                     username: Optional[str] = None,
                     password: Optional[str] = None) -> Credentials:
    """读取账号密码。

    值可能是 `enc:v1:` 开头的 **Windows DPAPI 密文**(推荐),也可能是明文
    (兼容老文件,但会给出未加密提示)。

    显式传入的 username/password 优先(便于人工兜底与测试)。
    失败时只报**尝试过的候选路径 + 期望键名 + 实际标签名**,绝不回显值。
    """
    if username and password:
        return Credentials(username=username, password=password,
                           source="explicit",
                           username_label="(params)", password_label="(params)")

    resolved, _source, tried = _resolve_cred_candidate(path=path, cwd=cwd)
    if not resolved:
        target = path or os.path.join(cwd or os.getcwd(), DEFAULT_CRED_FILES[0])
        tried_text = "\n".join(f"    {i}) {cand}"
                               for i, cand in enumerate(tried, 1))
        raise CredentialError(
            "找不到凭证文件。已按顺序找过这些位置(都不存在):\n"
            + (tried_text or "    (没有可尝试的位置)") + "\n"
            "三种办法(任选一种):\n"
            f"  1) 用 `emcli login init` 生成模板,再填账号密码;\n"
            f"  2) 手工建文件 {target},内容两行:\n"
            "         账号: <你的登录名>\n"
            "         密码: <你的密码>\n"
            f"  3) 用 --env-file <路径> 指定已有的凭证文件,"
            f"或设 {ENV_CRED_FILE}。\n"
            "填好后建议跑 `emcli login protect` 加密(明文不许长期落盘)。")

    try:
        with open(resolved, "r", encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError as exc:
        raise CredentialError(f"读取凭证文件失败:{resolved}({exc})") from exc

    mapping = parse_env_text(text)
    user_label, user_value = _pick_key(mapping, USERNAME_KEYS)
    pass_label, pass_value = _pick_key(mapping, PASSWORD_KEYS)

    missing: List[str] = []
    if not user_value:
        missing.append("账号(候选键名:" + "/".join(USERNAME_KEYS[:4]) + ")")
    if not pass_value:
        missing.append("密码(候选键名:" + "/".join(PASSWORD_KEYS[:4]) + ")")
    if missing:
        raise CredentialError(
            f"凭证文件 {resolved} 缺少 " + "、".join(missing)
            + "。实际存在的标签:" + ("、".join(mapping.keys()) or "(无)"))

    # 密文则解密(明文原样返回,兼容老文件)
    from . import credcrypto

    encrypted = credcrypto.is_encrypted(user_value) and \
        credcrypto.is_encrypted(pass_value)
    if credcrypto.is_encrypted(user_value) != credcrypto.is_encrypted(pass_value):
        raise CredentialError(
            f"凭证文件 {resolved} 里账号与密码的加密状态不一致"
            "(一个加密、一个明文)。请用 `emcli login protect` 统一重新加密。")
    if encrypted:
        try:
            user_value = credcrypto.decrypt_value(user_value)
            pass_value = credcrypto.decrypt_value(pass_value)
        except credcrypto.CryptoError as exc:
            raise CredentialError(
                f"凭证文件 {resolved} 解密失败:{exc}。"
                "密文由 Windows DPAPI 按**当前登录用户**加密,"
                "换机器或换 Windows 账号都解不开;请在本机重新执行 "
                "`emcli login protect` 生成。") from exc

    creds = Credentials(username=user_value, password=pass_value,
                        source=resolved, username_label=user_label,
                        password_label=pass_label)
    creds.encrypted = encrypted
    return creds


def init_cred_file(path: Optional[str] = None,
                   cwd: Optional[str] = None,
                   account: str = "",
                   password: str = "",
                   force: bool = False) -> Dict[str, Any]:
    """生成凭证文件模板(同事首次使用最省事的一条命令)。

    - 不给 account/password 时只写占位符,由使用者自己填;
    - 给了就直接写入(仍建议随后 `emcli login protect` 加密);
    - 文件已存在且未加 force 时**不覆盖**,避免把已有凭证冲掉。
    """
    target = path or os.path.join(cwd or os.getcwd(), DEFAULT_CRED_FILES[0])
    target = os.path.abspath(target)
    existed = os.path.isfile(target)
    if existed and not force:
        raise CredentialError(
            f"凭证文件已存在,未改动:{target}。"
            "要覆盖请加 --force(会冲掉现有账号密码,慎用)。")

    body = [
        "# E-Mobile 自动登入凭证。",
        "# 填好后务必执行 `emcli login protect` 加密(Windows DPAPI,",
        "# 仅本机当前 Windows 用户可解密);明文不要长期留在磁盘上。",
        "",
    ]
    body.append(f"账号: {account}" if account else "账号: ")
    body.append(f"密码: {password}" if password else "密码: ")
    text = "\n".join(body).rstrip() + "\n"

    try:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        raise CredentialError(f"写入凭证文件失败:{target}({exc})") from exc

    return {"ok": True, "file": target, "overwrote": existed,
            "filled": bool(account and password)}


def protect_file(path: str, log: Optional[Any] = None) -> Dict[str, Any]:
    """把明文凭证文件加密成 DPAPI 密文,**原地覆盖**。

    返回 `{ok, file, keys, encrypted_keys, plaintext_found}`。
    幂等:已是密文的行不动;写回后**回读校验**能解出账号密码,否则报错
    (否则等于把凭证弄丢了)。

    供 `emcli login protect` 调用;抽成模块级函数便于离线测试。
    """
    logger = log or (lambda *a, **k: None)
    from . import credcrypto

    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError as exc:
        raise CredentialError(f"读取凭证文件失败:{path}({exc})") from exc

    mapping = parse_env_text(text)
    if not mapping:
        raise CredentialError(
            f"凭证文件 {path} 里没有可解析的键值对"
            "(需要 `键: 值` 或 `键=值` 形式的行)")

    out_lines: List[str] = []
    done: List[str] = []
    kept: List[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.lstrip("﻿").strip()
        # 注释/空行原样保留
        if not stripped or stripped.startswith(_COMMENT_PREFIXES):
            out_lines.append(raw_line)
            continue
        cut = -1
        sep_char = ""
        for sep in _SEPARATORS:
            idx = stripped.find(sep)
            if idx > 0 and (cut < 0 or idx < cut):
                cut, sep_char = idx, sep
        if cut <= 0:
            out_lines.append(raw_line)
            continue
        label = stripped[:cut].strip().strip('"').strip("'")
        value = stripped[cut + 1:].strip().strip('"').strip("'")
        if not label or not value:
            out_lines.append(raw_line)
            continue
        if credcrypto.is_encrypted(value):
            kept.append(label)
            out_lines.append(raw_line)
            continue
        # 前缀部分(可能是 "键: " 或 "键=" )保持不变,只替换值
        prefix = raw_line[:raw_line.find(sep_char) + 1] if sep_char in raw_line \
            else f"{label}{sep_char}"
        out_lines.append(prefix + " " + credcrypto.encrypt_value(value))
        done.append(label)

    header = ("# EMCLI-ENC v1:Windows DPAPI 密文,"
              "仅本机当前 Windows 用户可解密。由 `emcli login protect` "
              "生成,请勿手工编辑值。\n")
    new_text = header + "\n".join(out_lines).rstrip("\n") + "\n"
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_text)
    except OSError as exc:
        raise CredentialError(f"写入凭证文件失败:{path}({exc})") from exc

    logger(f"已加密 {len(done)} 个键:{'、'.join(done) or '(无,全部已是密文)'}")

    # 回读校验:必须能解出账号密码,否则说明写坏了
    try:
        creds = load_credentials(path=path)
    except Exception as exc:  # noqa: BLE001
        raise CredentialError(
            f"已写入 {path} 但回读解密失败:{exc}。"
            "文件可能已损坏,请用手头备份重跑。") from exc

    return {
        "ok": True,
        "file": path,
        "keys": sorted(mapping.keys()),
        "encrypted_keys": sorted(done),
        "already_encrypted_keys": sorted(kept),
        **creds.brief(),
    }


def load_cred_file(path: Optional[str] = None,
                   cwd: Optional[str] = None) -> Optional[str]:
    """仅定位凭证文件路径(内容读不读由调用方决定)。

    查找顺序同 `resolve_cred_file`:显式参数 → 环境变量 → 配置记录 →
    当前目录 → 各级父目录 → 本地配置目录。
    """
    return resolve_cred_file(path, cwd)


# --------------------------------------------------------------------------- #
# 登录页选择器(候选列表,与 compat/gui 同风格)
# --------------------------------------------------------------------------- #
#: 功能名 → 选择器候选(按优先级)。末项通常是通用兜底。
_LOGIN_DOM: Dict[str, Tuple[str, ...]] = {
    "username": ('input[placeholder="登录名"]',
                 "#loginid",
                 'input[name="loginid"]',
                 'input[placeholder*="登录名"]',
                 'input[type="text"]'),
    "password": ("input#password", 'input[type="password"]'),
    "captcha_input": ("input#upcaptcha",
                      'input[placeholder="验证码"]',
                      'input[placeholder*="验证码"]'),
    "captcha_img": ("img.captcha-img", 'img[src*="captcha"]'),
    "submit": ("button.login-form-button",
               'button[type="submit"]',
               ".ant-btn-primary"),
    "remember": ("input#upremember",),
    "form": (".login-form", "input#password"),
}


def login_dom() -> Dict[str, Tuple[str, ...]]:
    """登录页选择器候选表的只读副本。"""
    return {k: tuple(v) for k, v in _LOGIN_DOM.items()}


def _candidates(feature: str) -> List[str]:
    return list(_LOGIN_DOM.get(feature) or ())


# --------------------------------------------------------------------------- #
# 页面内 JS
# --------------------------------------------------------------------------- #
#: 上报登录页状态 + token **指纹**(不含 token 本身)
_STATE_JS = r"""
(() => {
  const pick = (sels) => {
    for (const s of sels) { let e = null; try { e = document.querySelector(s); } catch (x) {}
      if (e) return {sel: s, el: e}; }
    return null;
  };
  const cfg = %s;
  const form = pick(cfg.form);
  const user = pick(cfg.username);
  const pwd = pick(cfg.password);
  const capIn = pick(cfg.captcha_input);
  const capImg = pick(cfg.captcha_img);
  const btn = pick(cfg.submit);

  // token 指纹(djb2):只为判断"登录后 token 变没变",不导出 token
  let tokenFp = '', tokenLen = 0, token = '';
  const keys = ['nc-current-info', 'gn-current-info', 'en-current-info'];
  for (const k of keys) {
    const raw = localStorage.getItem(k);
    if (!raw) continue;
    let obj = null; try { obj = JSON.parse(raw); } catch (e) { continue; }
    const u = (obj && obj.currentUser) || obj || {};
    const t = u.access_token || u.accessToken || '';
    if (t) { token = t; tokenLen = t.length; break; }
  }
  if (token) {
    let h = 5381;
    for (let i = 0; i < token.length; i++) { h = ((h << 5) + h + token.charCodeAt(i)) | 0; }
    tokenFp = (h >>> 0).toString(16);
  }

  const msgs = [...document.querySelectorAll(
      '.ant-message-notice-content, .ant-form-explain, .ant-alert-message, ' +
      '[class*="error-hint"]')]
    .map(e => (e.textContent || '').trim()).filter(Boolean).slice(0, 8);
  const bodyText = (document.body ? (document.body.innerText || '') : '');

  const capSrc = capImg ? (capImg.el.getAttribute('src') || '') : '';
  const capKeyMatch = capSrc.match(/[?&]key=([^&]+)/);
  return JSON.stringify({
    href: location.href,
    readyState: document.readyState,
    has_form: !!form,
    form_sel: form ? form.sel : null,
    username_sel: user ? user.sel : null,
    password_sel: pwd ? pwd.sel : null,
    captcha_input_sel: capIn ? capIn.sel : null,
    captcha_img_sel: capImg ? capImg.sel : null,
    submit_sel: btn ? btn.sel : null,
    captcha_key: capKeyMatch ? capKeyMatch[1] : '',
    token_len: tokenLen,
    token_fp: tokenFp,
    messages: msgs,
    body_len: bodyText.length
  });
})()
"""

#: 用 canvas 取**当前显示**的验证码像素(重新 fetch 会换一张,不可用)
_READ_CAPTCHA_JS = r"""
(() => {
  const sels = %s;
  let img = null;
  for (const s of sels) { try { img = document.querySelector(s); } catch (e) {}
    if (img) break; }
  if (!img) return JSON.stringify({ok: false, err: '找不到验证码图片'});
  if (!img.complete || !img.naturalWidth) {
    return JSON.stringify({ok: false, err: '验证码图片尚未加载完成'});
  }
  try {
    const c = document.createElement('canvas');
    c.width = img.naturalWidth; c.height = img.naturalHeight;
    const ctx = c.getContext('2d');
    ctx.drawImage(img, 0, 0, c.width, c.height);
    const src = img.getAttribute('src') || '';
    const m = src.match(/[?&]key=([^&]+)/);
    return JSON.stringify({ok: true, dataUrl: c.toDataURL('image/png'),
                           key: m ? m[1] : '',
                           w: c.width, h: c.height});
  } catch (e) {
    return JSON.stringify({ok: false, err: '读取验证码像素失败: ' + e});
  }
})()
"""

#: 填写三个输入框(React 受控:必须走原生 setter + input 事件)
_FILL_JS = r"""
(() => {
  const cfg = %s;
  const vals = %s;
  const pick = (sels) => {
    for (const s of sels) { let e = null; try { e = document.querySelector(s); } catch (x) {}
      if (e) return {sel: s, el: e}; }
    return null;
  };
  const setNative = (el, value) => {
    const proto = el.tagName === 'TEXTAREA'
      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) { desc.set.call(el, value); }
    else { el.value = value; }
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const out = {};
  const u = pick(cfg.username), p = pick(cfg.password), c = pick(cfg.captcha_input);
  if (!u) return JSON.stringify({ok: false, err: '找不到账号输入框', out});
  if (!p) return JSON.stringify({ok: false, err: '找不到密码输入框', out});
  if (!c) return JSON.stringify({ok: false, err: '找不到验证码输入框', out});
  u.el.focus(); setNative(u.el, vals.username);
  p.el.focus(); setNative(p.el, vals.password);
  c.el.focus(); setNative(c.el, vals.code);
  // 回读:只回报长度,不回显内容
  out.username_len = String(u.el.value || '').length;
  out.password_len = String(p.el.value || '').length;
  out.code_len = String(c.el.value || '').length;
  out.sel = {username: u.sel, password: p.sel, captcha: c.sel};
  return JSON.stringify({ok: true, out});
})()
"""

#: 点提交(优先点表单里的提交按钮;拿不到就提交表单)
_SUBMIT_JS = r"""
(() => {
  const cfg = %s;
  let btn = null;
  for (const s of cfg.submit) { try { btn = document.querySelector(s); } catch (e) {}
    if (btn) break; }
  if (btn && !btn.disabled) { btn.click(); return JSON.stringify({ok: true, via: 'button'}); }
  // 兜底:文案里含 "登" 的按钮
  const cands = [...document.querySelectorAll('button')]
    .filter(b => (b.textContent || '').includes('登') && !b.disabled);
  if (cands.length) { cands[cands.length - 1].click();
    return JSON.stringify({ok: true, via: 'button-text'}); }
  const form = document.querySelector('form');
  if (form) { form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
    return JSON.stringify({ok: true, via: 'form-submit'}); }
  return JSON.stringify({ok: false, err: '找不到登录按钮'});
})()
"""

#: 换一张验证码(点图片刷新,页面会换 key)
_REFRESH_CAPTCHA_JS = r"""
(() => {
  const sels = %s;
  let img = null;
  for (const s of sels) { try { img = document.querySelector(s); } catch (e) {}
    if (img) break; }
  if (!img) return JSON.stringify({ok: false, err: '找不到验证码图片'});
  const before = img.getAttribute('src') || '';
  img.click();
  return JSON.stringify({ok: true, before});
})()
"""


# --------------------------------------------------------------------------- #
# 验证码识别
# --------------------------------------------------------------------------- #
class OcrEngine:
    """ddddocr 的薄封装(可注入替身,便于离线测试)。

    识别率说明(2026-09-29 实测,6 张样本):
      * 同一张图两次识别结果**稳定**;
      * 但**大小写**与**首字符**会错(首字符常被整段漏读);
      * 因此不能"识别一次就当真",必须靠服务端判据 + 换图重试。
    """

    def __init__(self, log: Optional[Any] = None,
                 models: Tuple[str, ...] = ("default",)) -> None:
        self._log = log or (lambda *a, **k: None)
        self._models = models
        self._engines: Optional[List[Any]] = None

    def _ensure(self) -> List[Any]:
        if self._engines is not None:
            return self._engines
        try:
            import ddddocr  # type: ignore
        except ImportError as exc:
            raise LoginError(
                "缺少验证码识别依赖 ddddocr。请安装:\n"
                "    pip install ddddocr\n"
                "或改用 `emcli login --code <验证码>` 手工输入一次。"
            ) from exc
        engines: List[Any] = []
        for name in self._models:
            try:
                if name == "beta":
                    engines.append(ddddocr.DdddOcr(show_ad=False, beta=True))
                elif name == "old":
                    engines.append(ddddocr.DdddOcr(show_ad=False, old=True))
                else:
                    engines.append(ddddocr.DdddOcr(show_ad=False))
            except Exception as exc:  # noqa: BLE001
                self._log(f"模型 {name} 不可用: {exc}")
        if not engines:
            raise LoginError("ddddocr 加载失败(所有模型都不可用)")
        self._engines = engines
        return engines

    def classify(self, png_bytes: bytes) -> str:
        """识别一张验证码;多模型时取"出现次数最多"的结果。"""
        engines = self._ensure()
        results = []
        for engine in engines:
            try:
                text = str(engine.classification(png_bytes) or "").strip()
                text = re.sub(r"\s+", "", text)
                if text:
                    results.append(text)
            except Exception as exc:  # noqa: BLE001
                self._log(f"识别失败: {exc}")
        if not results:
            return ""
        if len(results) == 1:
            return results[0]
        counts: Dict[str, int] = {}
        for r in results:
            counts[r] = counts.get(r, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0])))[0][0]


# --------------------------------------------------------------------------- #
# CDP 客户端(只做登录需要的事)
# --------------------------------------------------------------------------- #
class LoginPage:
    """连到 E-Mobile 主窗口的 CDP 页面(登录页也在同一个 target 里)。"""

    def __init__(self, port: Optional[int] = None,
                 log: Optional[Any] = None,
                 auto_launch: bool = True) -> None:
        if websocket is None:
            raise LoginError("需要 websocket-client:pip install websocket-client")
        self._log = log or (lambda *a, **k: None)
        self._ws = None
        self._next_id = 1
        self._auto_launch = auto_launch
        self.port = self._resolve_port(port)

    def _resolve_port(self, port: Optional[int]) -> int:
        from . import app as appmod
        found = appmod.find_live_port() if port in (None, 0) else port
        if found:
            return int(found)
        if not self._auto_launch:
            raise LoginError(
                f"调试端口不可用(试过 {port or '自动发现'})。"
                "请以 `E-Mobile.exe --remote-debugging-port=9333` 启动,"
                "或用 `emcli setup` 让本工具自动启动。")
        return int(appmod.ensure_port(preferred=port))

    # ---- 连接 ---- #
    def connect(self) -> None:
        self.close()
        try:
            targets = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/list", timeout=5).read())
        except Exception as exc:  # noqa: BLE001
            raise LoginError(
                f"连不上 E-Mobile 调试端口 {self.port}({exc})。"
                f"请先 `emcli setup`,或以 "
                f"`E-Mobile.exe --remote-debugging-port={self.port}` 启动") from exc
        page = _pick_main_page(targets)
        if page is None:
            raise LoginError("没找到 E-Mobile 页面目标(客户端可能没起来)")
        self._ws = websocket.create_connection(page["webSocketDebuggerUrl"],
                                               timeout=60)

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    def __enter__(self) -> "LoginPage":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- CDP ---- #
    def _call(self, method: str, params: Optional[dict] = None) -> dict:
        msg_id = self._next_id
        self._next_id += 1
        body: Dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            body["params"] = params
        try:
            self._ws.send(json.dumps(body))
            while True:
                msg = json.loads(self._ws.recv())
                if msg.get("id") == msg_id:
                    return msg
        except Exception as exc:  # noqa: BLE001
            # 登录成功后页面可能整页重载 → 连接被掐断,属正常现象
            raise LoginError(f"与 E-Mobile 页面通信中断({exc});页面可能正在重载") from exc

    def eval(self, expression: str, await_promise: bool = False) -> Any:
        params: Dict[str, Any] = {"expression": expression, "returnByValue": True}
        if await_promise:
            params["awaitPromise"] = True
        res = self._call("Runtime.evaluate", params)
        result = res.get("result") or {}
        if "exceptionDetails" in result:
            raise LoginError("页面求值异常: "
                             + json.dumps(result["exceptionDetails"],
                                          ensure_ascii=False)[:300])
        return (result.get("result") or {}).get("value")

    def eval_json(self, expression: str) -> Dict[str, Any]:
        raw = self.eval(expression)
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise LoginError(f"页面返回非 JSON:{raw[:200]}") from exc
        if isinstance(raw, dict):
            return raw
        raise LoginError(f"页面返回类型异常:{type(raw)}")

    # ---- 只读状态 ---- #
    def state(self) -> Dict[str, Any]:
        """登录页状态 + token 指纹(不含 token 值)。"""
        return self.eval_json(_STATE_JS % json.dumps(
            {k: list(v) for k, v in _LOGIN_DOM.items()}))

    def read_captcha(self) -> Tuple[bytes, str]:
        """取**当前显示**的验证码:`(PNG 字节, key)`。"""
        info = self.eval_json(_READ_CAPTCHA_JS % json.dumps(
            _candidates("captcha_img")))
        if not info.get("ok"):
            raise LoginError("读取验证码失败:" + str(info.get("err")))
        data_url = str(info.get("dataUrl") or "")
        if "," not in data_url:
            raise LoginError("读取验证码失败:返回的 dataUrl 非法")
        return base64.b64decode(data_url.split(",", 1)[1]), str(info.get("key") or "")

    def refresh_captcha(self) -> bool:
        """点验证码图换一张。返回是否触发成功。"""
        try:
            info = self.eval_json(_REFRESH_CAPTCHA_JS % json.dumps(
                _candidates("captcha_img")))
            return bool(info.get("ok"))
        except LoginError:
            return False

    # ---- 写 ---- #
    def fill(self, username: str, password: str, code: str) -> Dict[str, Any]:
        cfg = {k: list(v) for k, v in _LOGIN_DOM.items()}
        vals = {"username": username, "password": password, "code": code}
        info = self.eval_json(_FILL_JS % (json.dumps(cfg), json.dumps(vals)))
        if not info.get("ok"):
            raise LoginError("填写登录表单失败:" + str(info.get("err")))
        return dict(info.get("out") or {})

    def submit(self) -> str:
        info = self.eval_json(_SUBMIT_JS % json.dumps(
            {k: list(v) for k, v in _LOGIN_DOM.items()}))
        if not info.get("ok"):
            raise LoginError("提交登录失败:" + str(info.get("err")))
        return str(info.get("via") or "")


# --------------------------------------------------------------------------- #
# 自动登录主流程
# --------------------------------------------------------------------------- #
#: 失败判据(服务端/前端文案)
_FAILURE_HINTS = (
    "验证码", "密码错误", "账号错误", "用户名或密码", "密码不正确",
    "captcha", "invalid", "failed", "错误",
)


@dataclass
class LoginOutcome:
    ok: bool
    attempts: int = 0
    reason: str = ""
    messages: List[str] = field(default_factory=list)
    codes: List[str] = field(default_factory=list)
    already: bool = False
    token_changed: bool = False
    seconds: float = 0.0

    def brief(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "attempts": self.attempts,
            "already_logged_in": self.already,
            "token_changed": self.token_changed,
            "reason": self.reason,
            "messages": self.messages[:4],
            "seconds": round(self.seconds, 1),
        }


def _looks_like_failure(messages: List[str], new_messages: List[str]) -> str:
    for text in new_messages:
        low = str(text).lower()
        if any(hint.lower() in low for hint in _FAILURE_HINTS):
            return str(text)
    if new_messages:
        return str(new_messages[0])
    return ""


def perform_login(page: LoginPage,
                  creds: Credentials,
                  ocr: Optional[OcrEngine] = None,
                  manual_code: str = "",
                  max_attempts: int = 5,
                  min_code_len: int = 3,
                  timeout_s: float = 25.0,
                  log: Optional[Any] = None) -> LoginOutcome:
    """在登录页完成一次登录。

    流程(每次尝试):
      取当前显示的验证码 → 识别 → 填账号/密码/验证码 → 提交
      → 等服务端判据 → 成功即返回;失败则换一张验证码重试。

    `manual_code` 非空时只用它,不再识别(人工兜底)。
    """
    logger = log or (lambda *a, **k: None)
    started = time.time()
    engine = ocr or OcrEngine(log=logger)

    before = page.state()
    if not before.get("has_form"):
        return LoginOutcome(ok=True, already=True,
                            reason="当前不在登录页(已是登录状态)",
                            token_changed=False,
                            seconds=time.time() - started)
    before_fp = str(before.get("token_fp") or "")
    logger(f"登录页就绪:href={before.get('href')}")

    codes: List[str] = []
    last_messages: List[str] = []
    attempts = 0

    for attempt in range(1, max(1, max_attempts) + 1):
        # 1) 取验证码
        if manual_code and attempt == 1:
            code = manual_code.strip()
        else:
            png, key = page.read_captcha()
            code = engine.classify(png)
            if len(code) < min_code_len:
                logger(f"第 {attempt} 次:识别结果过短({len(code)} 字符),换一张验证码")
                page.refresh_captcha()
                time.sleep(1.5)
                continue
        codes.append(code)
        logger(f"第 {attempt} 次:验证码识别 {len(code)} 字符")

        # 2) 填写并提交
        filled = page.fill(creds.username, creds.password, code)
        if int(filled.get("code_len") or 0) != len(code):
            logger("警告:验证码输入框回读长度与识别结果不一致")
        via = page.submit()
        attempts += 1
        logger(f"第 {attempt} 次:已提交(via={via}),等待结果…")

        # 3) 等判据
        deadline = time.time() + timeout_s
        outcome_reason = ""
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                now = page.state()
            except LoginError:
                # 页面重载往往就是登录成功
                logger("页面重载,按登录成功处理")
                return LoginOutcome(ok=True, attempts=attempts,
                                    reason="登录后页面已重载",
                                    codes=codes, token_changed=True,
                                    seconds=time.time() - started)
            if not now.get("has_form"):
                changed = str(now.get("token_fp") or "") not in ("", before_fp)
                return LoginOutcome(ok=True, attempts=attempts,
                                    reason="登录表单已消失",
                                    token_changed=changed, codes=codes,
                                    seconds=time.time() - started)
            now_fp = str(now.get("token_fp") or "")
            if now_fp and now_fp != before_fp:
                return LoginOutcome(ok=True, attempts=attempts,
                                    reason="token 已更新",
                                    token_changed=True, codes=codes,
                                    seconds=time.time() - started)
            messages = [str(m) for m in (now.get("messages") or [])]
            new_messages = [m for m in messages if m not in
                            (before.get("messages") or [])]
            hit = _looks_like_failure(messages, new_messages)
            if hit:
                outcome_reason = hit
                last_messages = messages
                break

        if outcome_reason:
            logger(f"第 {attempt} 次失败:{outcome_reason}")
        else:
            logger(f"第 {attempt} 次:等待超时,未见成功也未见图示错误")
        if attempt < max_attempts and not manual_code:
            page.refresh_captcha()
            time.sleep(1.5)

    return LoginOutcome(
        ok=False, attempts=attempts,
        reason=("登录未成功(已尝试 %d 次)" % attempts
                + (f":{last_messages[0]}" if last_messages else "")),
        messages=last_messages, codes=codes,
        seconds=time.time() - started)


# --------------------------------------------------------------------------- #
# 会话状态(判断"要不要重新登录")
# --------------------------------------------------------------------------- #
#: 需要重新登录的服务端判据
RELOGIN_ERRCODES = (42001, "42001")


def session_status(base_url: str, token: str,
                   timeout: float = 15.0) -> Dict[str, Any]:
    """探一次 `/emp/api/msg/gettoken`,判断 OA 会话是否需要重新登录。

    token 只在内存里用,返回值里不含 token。
    """
    if not base_url or not token:
        return {"ok": False, "needs_login": True,
                "reason": "本地没有可用登录态(未登录)"}
    url = base_url.rstrip("/") + "/emp/api/msg/gettoken"
    try:
        import requests
    except ImportError:  # pragma: no cover
        return {"ok": False, "needs_login": True, "reason": "缺少 requests 依赖"}
    try:
        resp = requests.get(url, params={"getTime": int(time.time() * 1000),
                                         "client_type": 1},
                            headers={"access_token": token,
                                     "emaccesstk": token,
                                     "User-Agent": "E-Mobile/unknown"},
                            timeout=timeout)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "needs_login": True,
                "reason": f"探测失败:{type(exc).__name__}"}
    errcode = data.get("errcode", 0)
    if errcode in RELOGIN_ERRCODES:
        return {"ok": True, "needs_login": True, "errcode": errcode,
                "errmsg": str(data.get("errmsg") or ""),
                "reason": "OA 会话已失效(被别处登录顶掉或超时)"}
    if errcode not in (0, "0", None):
        return {"ok": True, "needs_login": True, "errcode": errcode,
                "errmsg": str(data.get("errmsg") or ""),
                "reason": "gettoken 返回异常"}
    return {"ok": True, "needs_login": False, "errcode": 0,
            "reason": "会话有效"}
