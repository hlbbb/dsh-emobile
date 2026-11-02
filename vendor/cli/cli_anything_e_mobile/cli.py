"""cli_anything_e_mobile — E-Mobile(泛微移动办公)agent-native CLI。

仅本机使用,不发布。

命令结构:
  emcli status                          配置与连通性总览
  emcli compat                          版本兼容能力探测(只读,诊断用)
  emcli setup                           首次使用引导(端口 / 启动入口 / 登录态)
  emcli launcher show|install|remove    启动入口:让客户端以后默认带调试端口启动
  emcli token refresh|set|clear         token 管理
  emcli config show|set                 本机配置
  emcli chat list                       会话列表(未读/搜索)
  emcli chat history <会话>             历史消息
  emcli chat send <会话> <消息>          发消息(XMPP,当前服务端不接收)
  emcli chat send-file <会话> <文件>     发文件(驱动 GUI,已验证可用)
  emcli chat watch                      监听新消息
  emcli contact search <关键词>          搜人
  emcli group list / group members      我的群组
  emcli ding list                       必达列表
  emcli policy show|add|remove|clear   发送白名单(防误发护栏)
  emcli db overview|urls|users|items    本地 em7.db 只读查询
  emcli repl                            裸命令进入交互模式

版本兼容:
  E-Mobile 7.1.4 与 7.2.6 的登录态 localStorage 键名不同(正式环境实测 7.1.4
  用 `gn-current-info`/`en-current-info`,开发机 7.2.6 用 `nc-current-info`),
  安装路径也不同(正式环境在 `C:\\软件\\E-Mobile\\...`)。
  token 读取与 exe 发现已做自适应;GUI 的 DOM 选择器已候选化但尚未在
  7.1.4 上验证 —— 换新版本先跑 `emcli compat` 看缺哪项能力。

安全模型(零信任,默认禁止写):
  `chat send` / `chat send-file` 需要 ① 会话在发送白名单内 ② 交互确认(或 --yes)。
  消息一旦发出无法收回,群消息更是广播,所以默认一律禁止。
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import click

from . import __prog__, __version__
from .config import (CDP_PORT, Config, ConfigError, TOKEN_TTL_SECONDS,
                     mask_jid, mask_secret, mask_user_id, mask_user_label_text)
from .gui import GuiClient
from .output import Output
from .im.xmpp import XMPPClient

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


class Ctx:
    """跨命令共享的上下文。"""

    def __init__(self) -> None:
        self.started_at = time.time()
        self.verbose = False
        #: 显式排障开关:默认所有**展示/排障**输出面都给掩码后的工号 / JID
        #: (显式原始数据命令 `db users` 等除外)。只有 `--show-identity`
        #: 才把完整值放出来(见 `_mask_jid`)。
        self.show_identity = False
        self._output: Optional[Output] = None
        self._session: Optional[Any] = None

    @property
    def out(self) -> Output:
        if self._output is None:
            self._output = Output()
        return self._output

    def set_output(self, out: Output) -> None:
        self._output = out

    @property
    def session(self):
        if self._session is None:
            from .session import EMSession

            def _log(msg):
                if self.verbose:
                    self.out.info(_restore_connected_identity(self, msg))
            self._session = EMSession(log=_log)
        return self._session

    @session.setter
    def session(self, value) -> None:
        self._session = value


pass_ctx = click.make_pass_decorator(Ctx, ensure=True)


def _emit(ctx: Ctx, payload: Any, human: str) -> None:
    ctx.out.data(payload) if ctx.out.json_mode else ctx.out.info(human)


#: `config.access_token_from` 的机器值 → 人话(供 status / setup / compat 统一口径)
TOKEN_SOURCE_LABELS: Dict[str, str] = {
    "cdp": "E-Mobile 当前登录会话",
    "localstore": "本机 E-Mobile 本地登录态(无需调试端口)",
    "manual": "手工写入(emcli token set)",
    "cache": "本地缓存的登录态",
}


def _token_source_text(source: Optional[str]) -> str:
    """把登录态来源的机器值(`cdp` / `localstore` / `manual`)翻成人话。"""
    key = str(source or "").strip().lower()
    if not key:
        return "-"
    return TOKEN_SOURCE_LABELS.get(key, f"{key}(未知来源)")


def _token_source_of(ctx: Ctx, info: Optional[Dict[str, Any]] = None) -> str:
    """判断本次登录态实际来自哪个后端。

    优先读 `config.access_token_from`(session 写入的机器值:``cdp`` /
    ``localstore``);拿不到时从返回值的 source 文案里识别,避免依赖 session
    的内部实现细节。
    """
    try:
        raw = str(getattr(ctx.session.config, "access_token_from", "") or "")
    except Exception:  # noqa: BLE001
        raw = ""
    if raw:
        return raw
    text = str((info or {}).get("source") or "")
    return "localstore" if "leveldb" in text.lower() else "cdp"


def _masked_user_fields(cfg: Any) -> Dict[str, Any]:
    """当前登录用户的**脱敏**展示字段(组合逻辑只在 `Config.summary()` 里做一次)。

    姓名与工号都是个人敏感值:**登录态展示面**(人类文本 / `--json`)默认
    不给完整值。这里只挑出展示层要用的几个字段,不在展示层重复切片。
    功能数据面(`chat list` 等)里的会话 id 属于操作入口,不经过这里。
    """
    summary = cfg.summary()
    return {
        "user": summary.get("user", "-"),
        "user_masked": summary.get("user_masked", "-"),
        "user_len": summary.get("user_len", 0),
        "user_id_masked": summary.get("user_id_masked", "-"),
        "user_id_len": summary.get("user_id_len", 0),
    }


def _masked_session_info(info: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """复制一份 session 登录态摘要,把其中的**完整**用户标识换成脱敏值。

    `refresh_token_from_app()` 的 `user` 是 `姓名(工号)` 组合串;它会被塞进
    `--json` 的 `steps[*].detail`,所以必须在进入任何输出前替换掉。
    """
    data = dict(info or {})
    if "user" in data:
        data["user"] = mask_user_label_text(data["user"])
    return data


def _mask_jid(ctx: Ctx, jid: Any) -> str:
    """JID 的统一展示出口:**node**(`@` 之前,含工号)默认掩码。

    E-Mobile 的 JID 形如 `{工号}|{设备标识}@domain/resource`;排障输出面
    (`status --probe` / `compat --probe` / `chat send --dry-run` / `--verbose`
    连接日志)都会带它,而这类输出常被落盘或转发,所以默认只给掩码形态
    (见 `config.mask_jid`),domain / resource 保留。

    `--show-identity` 是显式排障开关:只有调用方确认"这份输出不会外泄"时
    才原样给出完整 JID。
    """
    text = str(jid or "").strip()
    if not text:
        return "-"
    return mask_jid(text, reveal=ctx.show_identity)


def _looks_like_user_id(segment: str) -> bool:
    """判断一段字符串是否**看起来像**工号 / 设备标识(纯数字或短字母数字串)。

    只用于 `_mask_chat_id` 的逐段判定:群 id 一般带 `-`(如 `grp-1`)、应用
    会话带 `_`(如 `a_xxx`)、`@domain/resource` 带 `.` 或 `/`,都不在此列,
    因此原样保留;纯数字(工号)与 `ab12cd34` 这类设备标识会被掩码。
    """
    text = str(segment or "")
    if not text or len(text) > 64:
        return False
    return all(ch.isascii() and (ch.isdigit() or ch.isalpha()) for ch in text)


def _mask_chat_id(ctx: Ctx, cid: Any) -> str:
    """**会话 id** 的统一排障面出口:逐段掩码,保留分隔符与 `@domain/resource`。

    为什么需要单独一个函数:单聊的会话 id 是**复合串** ``对方id,我的id``
    (发消息时再拼成 ``对方id,我的id|设备标识@domain``)。只掩 `from` 不够 ——
    `to` / `conversation.id` / stanza 的 `to` 都带同一个复合串,里面既有对方
    工号也有**本人**工号。这里按 ``,`` 与 ``|`` 拆段,每段若像用户 id 就走
    `config.mask_user_id()`,其余段(群 id、应用会话 id、`@domain/resource`)
    原样保留,分隔符结构不变。

    `--show-identity` 时原样返回 —— 它**只**服务排障面
    (`chat send --dry-run` / `--probe` / `--verbose`)。功能数据面
    (`chat list` / `chat history` / 发送结果 / `policy show`)是用户的操作
    入口,**不要**调这里。
    """
    text = str(cid or "").strip()
    if not text:
        return "-"
    if ctx.show_identity:
        return text
    node, sep, rest = text.partition("@")
    pipe_parts = []
    for pipe_part in node.split("|"):
        parts = [mask_user_id(p) if _looks_like_user_id(p) else p
                 for p in pipe_part.split(",")]
        pipe_parts.append(",".join(parts))
    masked = "|".join(pipe_parts)
    return f"{masked}@{rest}" if sep else masked


#: `im/xmpp.py` 连接日志的固定前缀(那一行在**源头**已掩码)。
_CONNECTED_LOG_PREFIX = "已连接 "


def _restore_connected_identity(ctx: Ctx, msg: str) -> str:
    """`--show-identity` 时把已掩码的「已连接 <jid>」还原成完整 node。

    `im/xmpp.py` 在**源头**就把 node 掩码了(任何调用方的日志都不会带工号),
    所以这里只做"显式排障开关打开后还原"这一件事:node 用配置里的真实值,
    domain / resource 沿用原消息(不重建)。

    默认(未加 `--show-identity`)原样返回 —— 掩码形态保持不变。
    """
    text = str(msg)
    if not ctx.show_identity or not text.startswith(_CONNECTED_LOG_PREFIX):
        return text
    body = text[len(_CONNECTED_LOG_PREFIX):]
    _, sep, rest = body.partition("@")
    cfg = getattr(getattr(ctx, "_session", None), "config", None)
    if not sep or cfg is None or not str(getattr(cfg, "base_user_id", "") or ""):
        return text
    node = f"{cfg.base_user_id}|{cfg.msg_udid}"
    return f"{_CONNECTED_LOG_PREFIX}{node}@{rest}"


# --------------------------------------------------------------------------- #
# 根命令
# --------------------------------------------------------------------------- #
@click.group(context_settings=CONTEXT_SETTINGS, invoke_without_command=True)
@click.option("--json", "json_mode", is_flag=True,
              help="结构化 JSON 输出(供 agent 消费)")
@click.option("--cdp-port", type=int, default=None,
              help=f"E-Mobile 调试端口(默认 {CDP_PORT})")
@click.option("--quiet", is_flag=True, help="抑制非 JSON 的说明输出")
@click.option("--verbose", is_flag=True,
              help="打印 XMPP/REST 握手过程(排障用;连接日志里的工号默认掩码)")
@click.option("--show-identity", is_flag=True,
              help="排障用:输出完整工号 / JID(默认掩码)。"
                   "仅在确认输出不会落盘、转发或外泄时使用")
@click.version_option(version=__version__, prog_name=__prog__)
@click.pass_context
def cli(ctx: click.Context, json_mode: bool, cdp_port: Optional[int],
        quiet: bool, verbose: bool, show_identity: bool) -> None:
    """E-Mobile CLI — 让泛微移动办公客户端 agent-native。"""
    # 复用调用方注入的上下文(便于测试/嵌入),否则新建
    state = ctx.obj if isinstance(ctx.obj, Ctx) else Ctx()
    state.set_output(Output(json_mode=json_mode, quiet=quiet))
    state.verbose = verbose or bool(__import__("os").environ.get("EMCLI_VERBOSE"))
    state.show_identity = bool(show_identity)
    if cdp_port:
        __import__("os").environ["EMCLI_CDP_PORT"] = str(cdp_port)
    ctx.obj = state

    if ctx.invoked_subcommand is None:
        # 裸命令 → REPL
        ctx.invoke(repl)


# --------------------------------------------------------------------------- #
# config:本机配置查看/修改
# --------------------------------------------------------------------------- #
@cli.group()
def config() -> None:
    """本机配置(配置文件在 %LOCALAPPDATA%\\emcli\\config.json)。"""


@config.command("show")
@pass_ctx
def config_show(ctx: Ctx) -> None:
    """查看当前配置(完整,含白名单)。"""
    cfg = ctx.session.config
    if ctx.out.json_mode:
        ctx.out.data(Config.load().summary(with_secrets=False))
        return
    rows = [[k, v] for k, v in cfg.summary().items()]
    ctx.out.table(rows, headers=["项", "值"], title="本机配置")


@config.command("set")
@click.argument("key")
@click.argument("value")
@pass_ctx
def config_set(ctx: Ctx, key: str, value: str) -> None:
    """改一个配置项。可改:transport(tcp/ws)、proxy(host:port)、resource。"""
    cfg = ctx.session.config
    allowed = {
        "transport": lambda v: v if v in ("tcp", "ws") else _bad(v, "tcp|ws"),
        "proxy": str,
        "resource": str,
    }
    if key not in allowed:
        ctx.out.error(f"不允许修改 {key!r};可改:{', '.join(sorted(allowed))}")
        raise SystemExit(1)
    if key == "transport" and value not in ("tcp", "ws"):
        ctx.out.error("transport 只能是 tcp 或 ws")
        raise SystemExit(1)
    setattr(cfg, key, value)
    cfg.save()
    if ctx.out.json_mode:
        ctx.out.data({"ok": True, "key": key, "value": value})
    else:
        ctx.out.success(f"{key} = {value}")


def _bad(value: str, expect: str) -> str:
    raise ValueError(f"{value!r} 不合法,应为 {expect}")


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
@cli.command()
@click.option("--probe", is_flag=True, help="实际探测 REST/XMPP 连通性")
@pass_ctx
def status(ctx: Ctx, probe: bool) -> None:
    """显示配置与连通状态。"""
    from . import app as appmod
    from . import cdp as cdpmod
    cfg = ctx.session.config
    summary = cfg.summary()
    # 登录态来源:cdp / localstore / manual → 人话(同时保留机器值给 agent)
    summary["token_source"] = _token_source_text(cfg.access_token_from)
    summary["access_token_from"] = cfg.access_token_from or ""

    cdp_ok = cdpmod.cdp_available(CDP_PORT)
    summary["e_mobile_app"] = ("运行中(调试端口 "
                               f"{CDP_PORT} 可用)" if cdp_ok else
                               f"未检测到调试端口 {CDP_PORT}")
    summary["local_db"] = {"path": str(__import__(
        "cli_anything_e_mobile.localdb", fromlist=["db_path"]).db_path()),
        "exists": __import__("cli_anything_e_mobile.localdb", fromlist=["exists"]
                             ).exists()}

    # 启动入口标量(只读探测;失败不影响 status 退出码,也不阻塞其余输出)
    try:
        launcher = appmod.launcher_status()
        summary["launcher_configured"] = bool(launcher.get("configured"))
        summary["launcher_port"] = launcher.get("port")
    except Exception:  # noqa: BLE001
        summary["launcher_configured"] = False
        summary["launcher_port"] = None

    if probe:
        checks: List[dict] = []
        try:
            info = ctx.session.fetch_im_params()
            checks.append({"name": "gettoken", "ok": True, "detail": info})
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": "gettoken", "ok": False, "error": str(exc)[:300]})
        try:
            with ctx.session.xmpp() as client:
                jid = client.negotiated_jid
                convs = client.get_conversations(count=1)
                checks.append({"name": "xmpp", "ok": True,
                               "detail": {"jid": _mask_jid(ctx, jid),
                                          "conversations": len(convs.get("conversations") or [])}})
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": "xmpp", "ok": False, "error": str(exc)[:300]})
        summary["checks"] = checks

    if ctx.out.json_mode:
        ctx.out.data(summary)
        return
    scalar = {k: v for k, v in summary.items()
              if isinstance(v, (str, int, float, bool)) or v is None}
    ctx.out.table([[k, v] for k, v in scalar.items()],
                  headers=["项", "值"], title="E-Mobile CLI 状态")
    for k, v in summary.items():
        if isinstance(v, dict):
            ctx.out.info(f"\n[{k}]")
            for kk, vv in v.items():
                ctx.out.info(f"  {kk}: {vv}")
    checks = summary.get("checks")
    if isinstance(checks, list) and checks:
        ctx.out.info("\n[连通性探测]")
        for check in checks:
            mark = "✓" if check.get("ok") else "✗"
            detail = check.get("detail") or check.get("error")
            ctx.out.info(f"  {mark} {check.get('name')}: {detail}")
    if not summary.get("access_token") or summary["access_token"] == "-":
        ctx.out.info("\n提示:先 `emcli token refresh` 获取登录态")
    if not cdp_ok:
        # 不写死安装路径 —— 正式环境的 E-Mobile 在 C:\软件\... 下
        exe = appmod.executable_from_env_or_default()
        launch = f"  & '{exe}' --remote-debugging-port={CDP_PORT}" if exe \
            else f"  设 $env:EMCLI_EMOBILE_EXE 指向 E-Mobile.exe 后重试 " \
                 f"(或用 `emcli compat` 看发现路径)"
        ctx.out.info(
            "\n提示:读类命令(会话/未读/历史/搜人)**不需要**调试端口 —— "
            "会自动读本机 E-Mobile 本地登录态;\n"
            "      发送类命令(send-gui/send-file/send-image)才需要,"
            "CLI 会自动兜底(必要时优雅重启客户端,登录态保留)。\n"
            "      想让客户端以后默认带端口启动:`emcli launcher install`"
            "(回滚:`emcli launcher remove`)。\n"
            "      也可以手动启动:\n" + launch)


# --------------------------------------------------------------------------- #
# token
# --------------------------------------------------------------------------- #
@cli.group()
def token() -> None:
    """token 管理。"""


@token.command("refresh")
@click.option("--force", is_flag=True, help="忽略缓存强制刷新")
@pass_ctx
def token_refresh(ctx: Ctx, force: bool) -> None:
    """从运行中的 E-Mobile 读取登录态,并取 IM 参数。"""
    out = ctx.out
    steps: List[dict] = []
    try:
        info = ctx.session.refresh_token_from_app()
        steps.append({"step": "read app session", "ok": True,
                      "detail": _masked_session_info(info)})
        im = ctx.session.fetch_im_params(force=True)
        steps.append({"step": "get im params", "ok": True, "detail": im})
        payload = {"ok": True, "steps": steps, "config": ctx.session.config.summary()}
        if out.json_mode:
            out.data(payload)
        else:
            # 姓名脱敏:不直接回显 info["user"](那是完整姓名 + 工号)
            masked_user = _masked_session_info(info).get("user") or "-"
            out.success(f"已读取 {masked_user} 的登录态")
            for k, v in im.items():
                if k not in ("msg_token",):
                    out.info(f"  {k}: {v}")
            out.success("token 与 IM 参数已保存")
    except Exception as exc:  # noqa: BLE001
        out.error(str(exc))
        raise SystemExit(1)


@token.command("set")
@click.argument("access_token")
@click.option("--base-url", default="", help="服务器地址,如 http://oam.example.com:8999")
@pass_ctx
def token_set(ctx: Ctx, access_token: str, base_url: str) -> None:
    """手动写入 access_token(无法自动读取时用)。"""
    cfg = ctx.session.config
    cfg.access_token = access_token.strip()
    cfg.access_token_from = "manual"
    cfg.access_token_at = time.time()
    if base_url:
        cfg.base_url = base_url.strip()
    cfg.save()
    out = ctx.out
    if out.json_mode:
        out.data({"ok": True, "config": cfg.summary()})
    else:
        out.success("已写入 access_token")
        try:
            out.success("IM 参数:" + ", ".join(
                f"{k}={v}" for k, v in ctx.session.fetch_im_params().items()
                if k != "msg_token"))
        except Exception as exc:  # noqa: BLE001
            out.error(f"写入成功,但取 IM 参数失败: {exc}")
            raise SystemExit(1)


@token.command("clear")
@pass_ctx
def token_clear(ctx: Ctx) -> None:
    """清除本机保存的登录态。"""
    cfg = ctx.session.config
    for field in ("access_token", "msg_token", "access_token_from", "access_token_at"):
        setattr(cfg, field, "" if field != "access_token_at" else 0)
    cfg.save()
    if ctx.out.json_mode:
        ctx.out.data({"ok": True})
    else:
        ctx.out.success("已清除本机保存的登录态")


# --------------------------------------------------------------------------- #
# login:自动登录(账号 + 密码 + 图形验证码)
# --------------------------------------------------------------------------- #
@cli.group(invoke_without_command=True)
@click.option("--env-file", "env_file", default="",
              help="凭证文件路径。不指定时按顺序自动查找:"
                   "$EMCLI_LOGIN_ENV → 配置里记录的路径 → 当前目录 → "
                   "各级父目录(最多 6 层)→ %LOCALAPPDATA%\\emcli")
@click.option("--code", "manual_code", default="",
              help="手工指定验证码(OCR 失败时兜底,一次性)")
@click.option("--attempts", type=int, default=5,
              help="验证码识别失败时的最大重试次数(默认 5,每次换一张验证码)")
@click.option("--timeout", "timeout_s", type=float, default=25.0,
              help="每次提交后等待登录结果的最长秒数(默认 25)")
@click.option("--ocr-models", default="default",
              help="识别模型,逗号分隔:default,beta,old(多模型取多数投票)")
@click.option("--force", is_flag=True,
              help="即使当前会话有效也重新登录")
@click.option("--no-launch", is_flag=True, help="不自动启动 E-Mobile")
@click.pass_context
def login(ctx: click.Context, env_file: str, manual_code: str, attempts: int,
          timeout_s: float, ocr_models: str, force: bool, no_launch: bool) -> None:
    """自动登录 E-Mobile(解决"OA 会话被顶掉需要重新登录"的场景)。

    唯一卡点是图片验证码:识别 → 提交 → 按服务端判据重试,失败就换一张。
    账号密码从凭证文件读(**只读不落盘、不回显**),可用 `--env-file` 指定;
    不指定时自动查找(配置记录 → 当前目录 → 各级父目录 → 本地目录),
    用过的路径会记进本机配置,下次换个目录也能直接用。

    首次使用:emcli login init → 填账号密码 → emcli login protect → emcli login
    """
    if ctx.invoked_subcommand is not None:
        return
    state = ctx.ensure_object(Ctx)
    _do_login(state, env_file=env_file, manual_code=manual_code,
              attempts=attempts, timeout_s=timeout_s,
              ocr_models=ocr_models, force=force, no_launch=no_launch)


@login.command("status")
@pass_ctx
def login_status(ctx: Ctx) -> None:
    """只看 OA 会话是否需要重新登录(只读,不登录)。

    注意:OA 会在登录后**再次轮换 token**,本地缓存那个几秒后就失效了,
    所以这里先让 `ensure_token()` 拿缓存去问一次服务端、不可用就回客户端重读,
    否则会看到"刚登录成功却报 42001"的假故障。
    """
    out = ctx.out
    from .login import session_status
    cfg = ctx.session.config
    cred = _cred_file_brief()
    cred_text = cred.get("credentials_file") or "(未找到)"
    try:
        token = ctx.session.ensure_token()
    except Exception as exc:  # noqa: BLE001
        payload = {"ok": False, "needs_login": True,
                   "reason": f"读不到可用登录态:{exc}", **cred}
        if out.json_mode:
            out.data(payload)
        else:
            out.error(payload["reason"])
            out.info(f"凭证文件: {cred_text}")
        raise SystemExit(1)
    status = session_status(cfg.base_url, token)
    status["base_url"] = cfg.base_url
    # 姓名 / 工号默认脱敏(唯一组合逻辑在 Config.summary;登录态展示面不给完整值)
    status.update(_masked_user_fields(cfg))
    status.update(cred)
    if out.json_mode:
        out.data(status)
        return
    out.info(f"服务器: {cfg.base_url or '-'}")
    out.info(f"用户  : {status.get('user') or '-'}")
    out.info(f"凭证文件: {cred_text}")
    if cred.get("credentials_found"):
        # 来源=配置记录时会带上"忽略配置记录后的首个命中",父目录查找若回归
        # 这里会立刻变成"未命中",不会被配置记录掩盖。
        out.info(f"  来源: {cred.get('credentials_source_display') or '-'}")
    if cred.get("credentials_config_stale"):
        out.info(f"  配置里记录的 {cred.get('credentials_config_file')} 已失效"
                 f"(文件不存在),已回退到 "
                 f"{cred.get('credentials_source') or '未找到'}")
    if status.get("needs_login"):
        out.error(f"需要重新登录:{status.get('reason')}")
        raise SystemExit(1)
    out.success("OA 会话有效,不需要重新登录")


@login.command("init")
@click.option("--env-file", "env_file", default="",
              help="凭证文件路径(默认当前目录的 OA账号密码.env)")
@click.option("--account", default="", help="登录名(可留空,之后自己填)")
@click.option("--password", default="", help="密码(可留空;建议填完立刻 protect 加密)")
@click.option("--force", is_flag=True, help="文件已存在时覆盖(会冲掉现有凭证)")
@pass_ctx
def login_init(ctx: Ctx, env_file: str, account: str, password: str,
               force: bool) -> None:
    """生成凭证文件模板(首次使用最省事)。

    只写模板、不联网、不登录。填好后跑 `emcli login protect` 加密。
    """
    out = ctx.out
    from .login import CredentialError, init_cred_file

    try:
        result = init_cred_file(path=env_file or None, account=account,
                                password=password, force=force)
    except CredentialError as exc:
        if out.json_mode:
            out.data({"ok": False, "stage": "init", "error": str(exc)})
        else:
            out.error(str(exc))
        raise SystemExit(1)

    if out.json_mode:
        out.data({"stage": "init", **result})
        return
    out.success(f"已生成凭证文件模板:{result['file']}")
    if result["filled"]:
        out.info("  已写入账号与密码。")
    else:
        out.info("  请打开该文件填入 `账号:` 与 `密码:`。")
    out.info("  下一步:`emcli login protect`(加密,明文不许长期落盘)")
    out.info("  然后:`emcli login`")


@login.command("protect")
@click.option("--env-file", "env_file", default="",
              help="要加密的凭证文件。不指定时与 `emcli login` 同一套自动查找顺序")
@click.option("--yes", "-y", is_flag=True, help="跳过交互确认")
@pass_ctx
def login_protect(ctx: Ctx, env_file: str, yes: bool) -> None:
    """把明文凭证文件加密成 Windows DPAPI 密文(**原地覆盖**)。

    本机当前 Windows 登录用户可解密;换机器 / 换账号都解不开。
    加密后 `emcli login` 自动读密文,行为与明文完全一致。
    """
    out = ctx.out
    from . import credcrypto
    from .login import (CredentialError, DEFAULT_CRED_FILES,
                       load_cred_file, protect_file)

    def _fail(message: str, payload: dict) -> None:
        if out.json_mode:
            out.data(payload)
        else:
            out.error(message)
        raise SystemExit(1)

    resolved = load_cred_file(env_file or None)
    if not resolved:
        hint = env_file or "、".join(DEFAULT_CRED_FILES)
        _fail(f"找不到凭证文件:{hint}",
              {"ok": False, "stage": "protect", "error": f"找不到凭证文件:{hint}"})
        return

    if not credcrypto.dpapi_available():
        _fail("本机 DPAPI 不可用(非 Windows 或调用失败),无法加密。"
              "请改用环境变量注入凭证。",
              {"ok": False, "stage": "protect",
               "error": "本机 DPAPI 不可用,无法加密"})
        return

    # 已有键名预览(交互确认用;只给标签,不给值)
    mapping = credcrypto and None
    try:
        with open(resolved, "r", encoding="utf-8-sig") as fh:
            from .login import parse_env_text
            mapping = parse_env_text(fh.read())
    except OSError as exc:
        _fail(f"读取凭证文件失败:{resolved}({exc})",
              {"ok": False, "stage": "protect", "error": f"读取失败:{exc}"})
        return

    if not yes:
        click.echo(f"即将加密凭证文件并【原地覆盖】:{resolved}")
        click.echo(f"  待处理键:{'、'.join(mapping.keys()) or '(无)'}")
        click.echo("  加密方式:Windows DPAPI(仅本机当前 Windows 用户可解密)")
        click.echo("  原明文会被覆盖,无法从本文件恢复 —— 请确认密码已备份。")
        if not click.confirm("确认加密此文件?"):
            out.info("已取消,文件未改动")
            raise SystemExit(1)

    try:
        result = protect_file(resolved, log=lambda m: out.info(m))
    except CredentialError as exc:
        _fail(str(exc), {"ok": False, "stage": "protect", "error": str(exc)})
        return

    payload = {"ok": True, "stage": "protect", **result}
    if out.json_mode:
        out.data(payload)
    else:
        out.success(f"凭证文件已加密:{resolved}")
        done = result.get("encrypted_keys") or []
        if done:
            out.info(f"  本次加密:{'、'.join(done)}")
        else:
            out.info("  文件中已全部是密文,无需处理")
        out.info(f"  账号 {result.get('username_len')} 字符 / "
                 f"密码 {result.get('password_len')} 字符(回读解密校验通过)")
        out.info("  提示:同一文件在另一台机器/另一个 Windows 账号下解不开,"
                 "请在各机器上分别执行 `emcli login protect`。")


def _remember_login_env_file(state: Ctx, source: str) -> None:
    """把这次成功解析到的凭证文件路径记进本机配置(`Config.login_env_file`)。

    只记**绝对路径**;账号/密码永远不落配置。任何失败都静默忽略 ——
    记不住不影响本次登录,只是下次换个目录还得再找一遍。
    """
    import os

    path = str(source or "").strip()
    if not path or path == "explicit":
        return          # 显式传入账号密码时没有文件路径可记
    try:
        absolute = os.path.abspath(path)
        cfg = state.session.config
        if str(getattr(cfg, "login_env_file", "") or "") == absolute:
            return
        cfg.login_env_file = absolute
        cfg.save()
    except Exception:  # noqa: BLE001
        pass


def _cred_file_brief() -> Dict[str, Any]:
    """凭证文件自动查找结果摘要(**只有路径与布尔,绝无账号/密码**)。

    供 `emcli compat` / `emcli login status` 做只读诊断。

    除"用的哪一份"外,还给出**忽略配置记录后的首个命中**:登录成功后
    配置里会记下路径,此后来源恒为"配置记录",单看来源永远发现不了
    父目录自动查找的回归;带上这一项就能一眼看出。
    """
    from .login import resolve_cred_file_detail

    try:
        detail = resolve_cred_file_detail()
    except Exception as exc:  # noqa: BLE001
        return {"credentials_found": False, "credentials_file": "",
                "credentials_source": "",
                "credentials_source_display": "",
                "credentials_source_ignoring_config": "",
                "credentials_file_ignoring_config": "",
                "credentials_candidates": 0,
                "credentials_config_file": "",
                "credentials_config_exists": False,
                "credentials_config_stale": False,
                "credentials_error": exc.__class__.__name__}

    source_key = str(detail.get("source") or "")
    source_label = str(detail.get("source_label") or "")
    ignored_label = str(detail.get("first_source_label_ignoring_config") or "")
    display = source_label
    if source_key == "config":
        # 只在来源=配置记录时附注;其他来源本身就是"真实的首个命中"。
        display = f"{source_label}(忽略后首个命中: {ignored_label or '未命中'})"
    return {
        "credentials_found": bool(detail.get("found")),
        "credentials_file": str(detail.get("path") or ""),
        "credentials_source": source_label,
        "credentials_source_display": display,
        "credentials_source_ignoring_config": ignored_label,
        "credentials_file_ignoring_config": str(
            detail.get("first_hit_ignoring_config") or ""),
        "credentials_candidates": len(detail.get("candidates_tried") or []),
        "credentials_config_file": str(detail.get("config_recorded_path") or ""),
        "credentials_config_exists": bool(
            detail.get("config_recorded_exists")),
        "credentials_config_stale": bool(
            detail.get("config_recorded_stale")),
    }


def _do_login(state: Ctx, env_file: str, manual_code: str, attempts: int,
              timeout_s: float, ocr_models: str, force: bool,
              no_launch: bool, emit_json: bool = True) -> None:
    """login 命令的实现(命令组与 ensure_login 共用)。

    `emit_json=False` 时**不**在 json 模式输出自己的 JSON 文档 —— 供 `setup`
    这种"自己会输出唯一一份 JSON"的调用方使用,避免一次命令吐出多份 JSON。
    人类模式的文案不受影响。
    """
    out = state.out
    from .errors import GUIError
    from .login import (CredentialError, LoginError, LoginPage, OcrEngine,
                        load_credentials, perform_login, session_status)

    def _fail(payload: dict, message: str) -> None:
        if out.json_mode:
            if emit_json:
                out.data(payload)
        else:
            out.error(message)
        raise SystemExit(1)

    # 1) 会话还有效就不折腾(除非 --force)
    if not force:
        try:
            token = state.session.ensure_token()
            status = session_status(state.session.config.base_url, token)
            if status.get("ok") and not status.get("needs_login"):
                payload = {"ok": True, "already": True, **status,
                           **_masked_user_fields(state.session.config)}
                if out.json_mode:
                    if emit_json:
                        out.data(payload)
                else:
                    out.success("OA 会话有效,无需重新登录"
                                "(要强制重登加 --force)")
                return
            if out.json_mode:
                out.info(f"会话失效,开始自动登录:{status.get('reason')}")
        except Exception as exc:  # noqa: BLE001
            out.info(f"会话探测未通过({exc});尝试自动登录")

    # 2) 读凭证
    try:
        creds = load_credentials(path=env_file or None)
    except CredentialError as exc:
        _fail({"ok": False, "stage": "credentials", "error": str(exc)},
              str(exc))
        return
    out.info(f"凭证来源:{creds.source}"
             f"(账号 {len(creds.username)} 字符 / 密码 {len(creds.password)} 字符)")
    # 解析成功就记住这个文件(只记路径):无论本轮最终登录成功与否,
    # 下次在别的目录跑都能直接用上同一份凭证。
    _remember_login_env_file(state, creds.source)

    models = tuple(m.strip() for m in (ocr_models or "default").split(",")
                   if m.strip()) or ("default",)

    # 3) 在登录页完成登录
    try:
        with LoginPage(log=lambda m: out.info(m),
                       auto_launch=not no_launch) as page:
            outcome = perform_login(
                page, creds,
                ocr=OcrEngine(log=lambda m: out.info(m), models=models),
                manual_code=manual_code, max_attempts=attempts,
                timeout_s=timeout_s, log=lambda m: out.info(m))
    except (LoginError, GUIError) as exc:
        _fail({"ok": False, "stage": "login", "error": str(exc)}, str(exc))
        return

    # 4) 刷新本机登录态,并用 gettoken **实测**会话是否真的可用
    #
    #    踩过的坑(2026-09-29 实测):登录页跳走后 localStorage 会先写入一个
    #    **过渡态 token**,`/emp/api/msg/gettoken` 对它仍返回 42001;真正的
    #    token 要再等一两秒。所以"页面上没有登录表单了"只算 UI 信号,不能
    #    当作可用。这里以 gettoken 的 errcode 为准,拿不到就重新读、继续等。
    refreshed: Dict[str, Any] = {}
    verified = False
    verify_note = ""
    if outcome.ok:
        deadline = time.time() + 30.0
        while True:
            try:
                refreshed = state.session.refresh_token_from_app(cdp_port=page.port)
            except Exception as exc:  # noqa: BLE001
                verify_note = f"读取登录态失败:{exc}"
            if refreshed:
                probe = session_status(state.session.config.base_url,
                                       state.session.config.access_token)
                if probe.get("ok") and not probe.get("needs_login"):
                    verified = True
                    break
                verify_note = str(probe.get("reason") or "会话仍不可用")
            if time.time() >= deadline:
                break
            time.sleep(2.0)
        # gettoken 成功时会一并刷新 IM 参数,顺手建好发消息要用的东西
        if verified:
            try:
                state.session.fetch_im_params(force=True)
            except Exception as exc:  # noqa: BLE001
                out.info(f"IM 参数获取失败(不影响登录):{exc}")
        elif not verify_note:
            verify_note = "登录后 30s 内仍未能取得可用的 OA 会话"

    # 明文凭证告警:不阻塞登录,但每次都用明文的账号密码登 OA 风险太高
    plaintext_warning = ""
    if outcome.ok and not creds.encrypted:
        plaintext_warning = (
            f"凭证文件 {creds.source} 是**明文**存储,已用其登录。"
            "建议执行 `emcli login protect` 加密(Windows DPAPI,"
            "仅本机当前用户可解密)。")

    ok = bool(outcome.ok and verified)
    payload = {"ok": ok, "stage": "login", **outcome.brief(),
               "verified": verified}
    if verify_note:
        payload["verify_note"] = verify_note
    if plaintext_warning:
        payload["plaintext_credentials"] = True
    if refreshed:
        # refreshed["user"] 是完整的"姓名(工号)",绝不直接回显;统一走脱敏字段
        payload.update(_masked_user_fields(state.session.config))
        if refreshed.get("user"):
            payload["user"] = mask_user_label_text(refreshed["user"])
        payload["client_version"] = refreshed.get("client_version")
    if out.json_mode:
        if emit_json:
            out.data(payload)
    else:
        if ok:
            out.success(f"登录成功({outcome.attempts} 次尝试,"
                        f"{outcome.seconds:.1f}s):{outcome.reason}")
            if refreshed:
                out.info(f"当前用户:{payload.get('user') or '-'}")
            out.info("OA 会话已实测可用(gettoken errcode=0),可以继续收发。")
            if plaintext_warning:
                out.error(plaintext_warning)
        elif outcome.ok:
            out.error(f"已完成登录动作,但会话仍不可用:{verify_note}")
        else:
            out.error(outcome.reason or "登录失败")
    if not ok:
        raise SystemExit(1)


# --------------------------------------------------------------------------- #
# setup:一键就绪(分发用)
# --------------------------------------------------------------------------- #
#: 等待客户端完成登录时的重试间隔(秒)
LOGIN_WAIT_INTERVAL_S = 5.0


def _sleep(seconds: float) -> None:
    """可打桩的 sleep(测试里替换掉,避免真的等 90s)。"""
    time.sleep(seconds)


def _fail_out(ctx: Ctx, message: str) -> None:
    """失败提示:json 模式走 `out.info`(不写 stdout),人类模式仍走 `out.error`。

    `out.error()` 在 json 模式下会强制往 stdout 写一份 `{"ok": false, ...}`,
    后面再 `out.data()` 就会吐出**多份 JSON 文档**,把 agent 的结构化解析打坏。
    所以 json 模式下失败信息一律走 info(该模式下是 no-op),如实信息由 steps
    与最终的 JSON 承载。
    """
    if ctx.out.json_mode:
        ctx.out.info(message)
    else:
        ctx.out.error(message)


def _wait_login_ready(ctx: Ctx, port: int, wait_s: int, last_error: Exception
                      ) -> Tuple[Optional[Dict[str, Any]], Exception]:
    """客户端刚启动/重启后,等它把登录态加载完(每 5s 重试,最多 `wait_s` 秒)。

    真机实测(2026-09-30):自动重启客户端后**同一轮**里读登录态会报
    "E-Mobile 会话里没有 access_token(可能未登录)",等约 35s 后重跑同一条
    命令就全绿。所以这里做有上限的等待-重试,进度走 `out.info`(json 模式下
    是 no-op,不污染 stdout)。

    返回 `(登录态信息, 最后一次异常)`;成功时异常位为 None。
    """
    out = ctx.out
    waited = 0.0
    total = float(wait_s)
    error: Exception = last_error
    while waited < total:
        step = min(LOGIN_WAIT_INTERVAL_S, total - waited)
        _sleep(step)
        waited += step
        out.info(f"等待客户端完成登录…({int(waited)}/{int(total)}s)")
        try:
            return ctx.session.refresh_token_from_app(cdp_port=port), error
        except Exception as exc:  # noqa: BLE001
            error = exc
    return None, error


@cli.command()
@click.option("--no-launch", is_flag=True, help="不自动启动 E-Mobile")
@click.option("--no-launcher", is_flag=True,
              help="不配置启动入口(不改桌面快捷方式)")
@click.option("--wait-login", "wait_login", type=int, default=90,
              show_default=True,
              help="客户端刚由本工具启动/重启后,等待登录态就绪的秒数"
                   "(0 = 不等待;等待期间每 5s 重试一次)")
@pass_ctx
def setup(ctx: Ctx, no_launch: bool, no_launcher: bool,
          wait_login: int) -> None:
    """首次使用引导:端口就绪 → 配置启动入口 → 读登录态 → 取 IM 参数。

    每一步都可独立降级:客户端在跑但没开调试端口时会自动**优雅重启**它
    (登录态保留,`EMCLI_NO_RESTART=1` 可退回"只提示不处理");配置启动入口
    失败**不影响**其余步骤;登录态 CDP 优先,失败自动回退磁盘 LevelDB。

    客户端刚被本工具启动/重启时,登录态要等它加载完 —— 这段时间按
    `--wait-login`(默认 90s)每 5s 重试一次,成功即继续;超时才走原有的
    自动登录兜底。`--wait-login 0` 表示不等待(旧行为)。
    """
    out = ctx.out
    from . import app as appmod

    steps: List[dict] = []
    wait_login = max(0, int(wait_login))

    # 1. 调试端口 / E-Mobile 进程(notify 让"正在为你重启客户端"这类提示可见)
    #    state 回传"本次是不是由本工具启动了客户端",供第 3 步决定是否等待登录态
    port_state: Dict[str, Any] = {}
    try:
        port = appmod.ensure_port(launch_if_needed=not no_launch,
                                  notify=lambda message: out.info(message),
                                  state=port_state)
        steps.append({"step": "e-mobile debug port", "ok": True, "port": port})
        out.success(f"E-Mobile 调试端口就绪: {port}")
    except appmod.AppError as exc:
        steps.append({"step": "e-mobile debug port", "ok": False,
                      "error": str(exc)})
        _fail_out(ctx, str(exc))
        if out.json_mode:
            out.data({"ok": False, "steps": steps})
        raise SystemExit(1)
    restarted = bool(port_state.get("restarted"))

    # 2. 配置启动入口(可降级步骤:失败只如实反映,不让 setup 整体失败)
    if no_launcher:
        steps.append({"step": "configure launcher", "ok": True,
                      "skipped": True, "reason": "--no-launcher"})
        out.info("已按 --no-launcher 跳过启动入口配置"
                 "(以后可随时 `emcli launcher install`)。")
    else:
        try:
            launcher = appmod.configure_launcher(port=port)
        except Exception as exc:  # noqa: BLE001
            launcher = {"ok": False, "action": "none", "changed": False,
                        "degraded": False, "port": port, "lnk": None,
                        "reason": f"{exc.__class__.__name__}: {exc}"}
        steps.append({"step": "configure launcher",
                      "ok": bool(launcher.get("ok")), "detail": launcher})
        _report_launcher_install(out, launcher)

    # 3. 登录态(CDP 优先;失败回退磁盘 LevelDB,不需要调试端口)
    info: Optional[Dict[str, Any]] = None
    login_error: Optional[Exception] = None
    try:
        info = ctx.session.refresh_token_from_app(cdp_port=port)
    except Exception as exc:  # noqa: BLE001
        login_error = exc

    # 刚由本工具启动/重启的客户端还没加载完登录态 → 等一会儿再读
    if info is None and login_error is not None and restarted \
            and wait_login > 0:
        out.info(f"客户端刚启动,等待登录态就绪(最多 {wait_login}s)…")
        info, login_error = _wait_login_ready(ctx, port, wait_login,
                                              login_error)

    if info is not None:
        source = _token_source_of(ctx, info)
        steps.append({"step": "read login session", "ok": True,
                      "source": source,
                      "detail": _masked_session_info(info)})
        # info["user"] 是完整"姓名(工号)";展示层统一取脱敏值
        masked_user = _masked_session_info(info).get("user") or "-"
        out.success(f"已读取登录态: {masked_user}"
                    f"(来源:{_token_source_text(source)})")
    else:
        exc = login_error if login_error is not None else RuntimeError(
            "读取登录态失败")
        steps.append({"step": "read login session", "ok": False,
                      "error": str(exc)})
        _fail_out(ctx, f"读取登录态失败: {exc}")
        # 落在登录页时,自动用凭证文件登录一次(唯一卡点是图形验证码)。
        # _do_login 内部已用 gettoken 实测会话是否真的可用,这里不再重复读。
        out.info("尝试自动登录(凭证文件 + 图形验证码识别)…")
        try:
            # emit_json=False:setup 自己只输出一份 JSON,这里不能再吐一份
            _do_login(ctx, env_file="", manual_code="", attempts=5,
                      timeout_s=25.0, ocr_models="default", force=True,
                      no_launch=True, emit_json=False)
            steps.append({"step": "auto login", "ok": True})
            out.success("自动登录完成")
        except SystemExit:
            steps.append({"step": "auto login", "ok": False})
            if out.json_mode:
                out.data({"ok": False, "steps": steps})
            raise

    # 4. IM 参数
    try:
        im = ctx.session.fetch_im_params(force=True)
        steps.append({"step": "im params", "ok": True,
                      "detail": {k: v for k, v in im.items()
                                 if k != "msg_token"}})
        out.success(f"消息服务器: {im.get('xmpp_host')}"
                    f"  transport={im.get('transport')}")
    except Exception as exc:  # noqa: BLE001
        steps.append({"step": "im params", "ok": False, "error": str(exc)})
        _fail_out(ctx, f"取 IM 参数失败: {exc}")
        if out.json_mode:
            out.data({"ok": False, "steps": steps})
        raise SystemExit(1)

    if out.json_mode:
        out.data({"ok": True, "steps": steps,
                  "config": ctx.session.config.summary()})
        return
    out.info("\n可以用了。常用命令:")
    out.info("  emcli chat list                会话列表 / 未读")
    out.info("  emcli chat history <会话>      历史消息")
    out.info("  emcli chat send-file <会话> <文件> --yes    发文件")
    out.info("  emcli policy add <会话>        授权发送(防误发)")
    out.info("  读类命令(会话/未读/历史/搜人)不需要调试端口;"
             "发送类命令需要(见 `emcli launcher show`)。")


def _report_launcher_install(out: Output, result: Dict[str, Any]) -> None:
    """按 `app.configure_launcher()` 的结果输出中文说明(本函数不抛异常)。

    四种结果分别对应:改了公共桌面快捷方式 / 降级创建用户桌面启动器 /
    已配置过 / 失败。失败时给出原因与手工处理建议,由调用方决定是否失败。
    """
    action = str(result.get("action") or "none")
    lnk = result.get("lnk") or "-"
    backup = result.get("backup")
    reason = str(result.get("reason") or "").strip()
    port = result.get("port")

    if result.get("ok") and action == "appended":
        out.success("已让 E-Mobile 以后默认带调试端口启动"
                    f"(已修改:{lnk})")
        if backup:
            out.info(f"  原快捷方式已备份到:{backup}")
        else:
            out.info("  注意:本次未能备份原快捷方式,"
                     "如需回滚请用 `emcli launcher remove`。")
        out.info("  回滚:`emcli launcher remove`(只去掉本工具追加的参数)")
    elif result.get("ok") and action == "created-user-launcher":
        out.info("公共桌面快捷方式改不动(需要管理员权限),已降级处理:"
                 f"在用户桌面创建了带调试端口的启动器 {lnk}")
        out.info("  **以后请用桌面上的 `E-Mobile (CLI).lnk` 启动客户端**"
                 "(或让管理员运行一次本工具,即可直接修改原快捷方式)。")
        if reason:
            out.info(f"  原因:{reason}")
        out.info("  回滚:`emcli launcher remove`"
                 "(启动器移入回收站,不做直接删除)")
    elif result.get("ok") and action == "already-configured":
        out.success(f"启动入口已就绪,无需改动(端口 {port};{lnk})")
    else:
        message = ("启动入口未配置成功(不影响其余步骤):"
                   + (reason or "原因未知"))
        # JSON 模式下 out.error() 会再吐一份 JSON 文档,污染结构化输出 ——
        # 那里只靠 steps 里的 ok/detail 如实反映,人类文本一律走 info。
        if out.json_mode:
            out.info(message)
        else:
            out.error(message)
        out.info("  手工处理:用管理员权限运行一次 `emcli launcher install`,"
                 "或手动给 E-Mobile 快捷方式加上 "
                 f"--remote-debugging-port={port}")


# --------------------------------------------------------------------------- #
# launcher:启动入口(让客户端以后默认带调试端口启动)
# --------------------------------------------------------------------------- #
@cli.group()
def launcher() -> None:
    """启动入口:让 E-Mobile 以后默认带调试端口启动。

    发送类命令(`chat send-gui` / `send-file` / `send-image`)需要 CDP 调试端口;
    把启动入口配好,客户端以后每次启动都自带端口,就不用再临时重启。
    """


@launcher.command("show")
@pass_ctx
def launcher_show(ctx: Ctx) -> None:
    """查看启动入口现状(只读,不改任何文件、不启停客户端)。"""
    from . import app as appmod

    out = ctx.out
    try:
        status = appmod.launcher_status()
    except Exception as exc:  # noqa: BLE001
        if out.json_mode:
            out.data({"ok": False, "error": str(exc)})
        else:
            out.error(f"读取启动入口状态失败:{exc}")
        raise SystemExit(1)

    if out.json_mode:
        out.data({"ok": True, **status})
        return

    public = status.get("public_lnk") or {}
    user = status.get("user_launcher") or {}
    rows = [
        ["公共桌面快捷方式", public.get("path") or "-",
         "存在" if public.get("exists") else "不存在",
         f"端口 {public.get('port')}" if public.get("configured")
         else "未带调试端口",
         "可写" if public.get("writable") else "不可写(改需管理员)"],
        ["用户桌面启动器", user.get("path") or "-",
         "存在" if user.get("exists") else "不存在",
         f"端口 {user.get('port')}" if user.get("configured")
         else "未带调试端口",
         "本工具创建" if user.get("created_by_us") else "-"],
    ]
    out.table(rows, headers=["启动入口", "路径", "状态", "调试端口", "备注"],
              title="启动入口状态(只读)")
    if status.get("configured"):
        out.success("客户端以后会默认带调试端口启动:"
                    f"{status.get('launcher')}(端口 {status.get('port')})")
    else:
        out.info("客户端目前**不会**默认带调试端口启动。")
        out.info(f"  让客户端默认带端口启动:emcli launcher install "
                 f"--port {CDP_PORT}")
        out.info("  提示:读类命令(会话/未读/历史/搜人)不需要调试端口,"
                 "只有发送类命令需要。")
    out.info(f"  {status.get('rollback')}")


@launcher.command("install")
@click.option("--port", type=int, default=CDP_PORT,
              help=f"调试端口(默认 {CDP_PORT})")
@pass_ctx
def launcher_install(ctx: Ctx, port: int) -> None:
    """让客户端以后默认带调试端口启动(改快捷方式;写不动会自动降级)。

    只改/建快捷方式,**不会**启动或关闭任何进程。公共桌面快捷方式会先备份。
    """
    from . import app as appmod

    out = ctx.out
    try:
        result = appmod.configure_launcher(port=port)
    except Exception as exc:  # noqa: BLE001
        if out.json_mode:
            out.data({"ok": False, "error": str(exc)})
        else:
            out.error(f"配置启动入口失败:{exc}")
        raise SystemExit(1)

    if out.json_mode:
        out.data(result)
        if not result.get("ok"):
            raise SystemExit(1)
        return
    _report_launcher_install(out, result)
    if not result.get("ok"):
        raise SystemExit(1)


@launcher.command("remove")
@click.option("--port", type=int, default=0, show_default=True,
              help="要移除的调试端口;0(默认)= 去掉快捷方式里**所有** "
                   "--remote-debugging-port 参数(推荐:安装时用的端口可能是 "
                   "9333/9334/9335/9222/9223)")
@pass_ctx
def launcher_remove(ctx: Ctx, port: int) -> None:
    """回滚启动入口改动(去掉追加的调试端口参数 / 回收本工具建的启动器)。

    默认(`--port 0`)去掉**所有**调试端口参数 —— `launcher install` 写进去的
    端口来自 `ensure_port()`,不一定是 9333,所以默认就该全清。指定具体端口时
    只去该端口,且**没改动就如实报 `changed=False`**;若剥离后仍残留其它端口
    参数,会明确告知"没有完全回滚"。只有 `clean=True`(实测确认两条 lnk 都不含
    任何调试端口参数)时才说"本来就是干净的";字段读不出来时按失败处理。
    """
    from . import app as appmod

    out = ctx.out
    try:
        result = appmod.remove_launcher(port=port)
    except Exception as exc:  # noqa: BLE001
        if out.json_mode:
            out.data({"ok": False, "error": str(exc)})
        else:
            out.error(f"回滚启动入口失败:{exc}")
        raise SystemExit(1)

    if out.json_mode:
        out.data(result)
        if not result.get("ok"):
            raise SystemExit(1)
        return

    if result.get("ok") and result.get("changed"):
        out.success("已回滚启动入口改动")
        for action in result.get("actions") or []:
            if action == "removed-arg-from-public-lnk":
                out.info("  已从公共桌面快捷方式去掉调试端口参数"
                         "(其余参数原样保留)")
            elif action == "recycled-user-launcher":
                out.info("  已把本工具创建的启动器移入回收站")
        for path in result.get("recycled") or []:
            out.info(f"    回收站:{path}")
        if result.get("backup"):
            out.info(f"  改动前的快捷方式已备份到:{result['backup']}")
    elif result.get("ok") and result.get("clean"):
        out.info("启动入口本来就是干净的,无需回滚")
    elif result.get("ok"):
        out.info("启动入口无需回滚(未发现本次要清理的调试端口参数)")
    else:
        out.error("回滚未完全成功:" + (result.get("reason") or "原因未知"))
    remaining = result.get("remaining_ports") or []
    if remaining:
        out.info("  注意:**没有完全回滚** —— 快捷方式里仍残留其它调试端口参数:"
                 + ", ".join(str(p) for p in remaining)
                 + "(如需一并去掉:`emcli launcher remove --port 0`)")
    if result.get("ok") and result.get("reason"):
        out.info(f"  说明:{result['reason']}")
    if not result.get("ok"):
        raise SystemExit(1)


# --------------------------------------------------------------------------- #
# chat
# --------------------------------------------------------------------------- #
@cli.group(invoke_without_command=True)
@click.option("-n", "--limit", type=int, default=50, help="最多列出多少会话")
@click.option("--unread", is_flag=True, help="只看有未读的会话")
@click.option("--name", "name_filter", default="", help="按会话名过滤")
@click.pass_context
def chat(ctx: click.Context, limit: int, unread: bool, name_filter: str) -> None:
    """消息与会话。`emcli chat list` 可省略子命令。"""
    if ctx.invoked_subcommand is None:
        ctx.invoke(chat_list, limit=limit, unread=unread,
                   name_filter=name_filter)


@chat.command("list")
@click.option("-n", "--limit", type=int, default=50, help="最多显示多少条")
@click.option("--unread", is_flag=True, help="只看有未读的会话")
@click.option("--name", "name_filter", default="", help="按会话名过滤")
@pass_ctx
def chat_list(ctx: Ctx, limit: int, unread: bool, name_filter: str) -> None:
    """会话列表(含未读数与最后一条消息)。"""
    out = ctx.out
    try:
        with ctx.session.xmpp() as client:
            data = client.get_conversations(count=100)
        convs = data.get("conversations") or []
        rows = []
        for c in convs:
            unread_count = int(c.get("unreadcount") or 0)
            if unread and unread_count <= 0:
                continue
            name = str(c.get("targetname") or "")
            if name_filter and name_filter not in name:
                continue
            rows.append({
                "id": c.get("targetid"),
                "type": {"0": "单聊", "1": "群", "111": "应用"}.get(
                    str(c.get("targettype")), c.get("targettype")),
                "name": name,
                "unread": unread_count,
                "last": _clean_content(c),
                "time": ctx.session.ts(c.get("lasttime")),
                "top": "是" if str(c.get("istop")) == "1" else "",
            })
        # 服务端可能忽略 count,这里在客户端侧按 limit 截断
        total = len(rows)
        rows = rows[:max(1, limit)]
        if out.json_mode:
            out.data({"ok": True, "count": len(rows), "total": total,
                      "conversations": rows})
        else:
            title = f"会话列表(共 {total} 个"
            if total > len(rows):
                title += f",显示前 {len(rows)})"
            else:
                title += ")"
            out.table([[r["unread"], r["type"], r["name"], r["last"], r["time"], r["id"]]
                       for r in rows],
                      headers=["未读", "类型", "会话", "最后消息", "时间", "会话 id"],
                      title=title)
    except Exception as exc:  # noqa: BLE001
        out.error(str(exc))
        raise SystemExit(1)


@chat.command("history")
@click.argument("conversation")
@click.option("-n", "--limit", type=int, default=20, help="条数")
@click.option("--before", default="", help="时间游标(用上页返回的 next_cursor)")
@pass_ctx
def chat_history(ctx: Ctx, conversation: str, limit: int, before: str) -> None:
    """读取会话历史消息。<会话> 可传 id 或会话名。"""
    out = ctx.out
    try:
        conv = ctx.session.resolve_conversation(conversation)
        target_id = str(conv.get("targetid"))
        with ctx.session.xmpp() as client:
            data = client.get_history(target_id, target_type="1", count=limit,
                                      last_time=int(before) if before else None)
        history = data.get("history") or []
        messages = []
        for item in history:
            body = _parse_op_body(item.get("opBody"))
            messages.append({
                "msgid": body.get("msgid") or item.get("msgid"),
                "sender": body.get("senderid") or item.get("fromUserId"),
                "time": ctx.session.ts(body.get("sendTime") or item.get("sendTime")),
                "type": body.get("objectName") or item.get("objectName"),
                "content": _preview(body.get("content")),
            })
        # id → 姓名
        names = ctx.session.resolve_names([m["sender"] for m in messages if m["sender"]])
        for m in messages:
            m["sender_name"] = names.get(m["sender"], m["sender"])
        if out.json_mode:
            out.data({"ok": True, "conversation": _conv_brief(conv),
                      "count": len(messages), "messages": messages,
                      "next_cursor": data.get("lastTime")})
        else:
            out.table([[m["time"], f"{m['sender_name']}({m['sender']})",
                        m["type"], m["content"]] for m in messages],
                      headers=["时间", "发送者", "类型", "内容"],
                      title=f"{conv.get('targetname')} 的历史消息({len(messages)} 条)")
            if data.get("lastTime"):
                out.info(f"更多消息:--before {data.get('lastTime')}")
    except Exception as exc:  # noqa: BLE001
        out.error(str(exc))
        raise SystemExit(1)


class SendBlocked(click.ClickException):
    """发送被安全护栏拦下。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.exit_code = 2


def _require_send_permission(ctx: Ctx, chat_id: str, conv: dict) -> None:
    """零信任护栏:只有白名单里的会话才允许发送。

    消息一旦发出无法收回(且群消息是广播),所以默认禁止。
    """
    allow = list(ctx.session.config.send_allowlist or [])
    if chat_id in allow:
        return
    name = conv.get("targetname") or chat_id
    is_group = ctx.session.is_group(conv)
    raise SendBlocked(
        f"已阻止发送:「{name}」({chat_id}) 不在发送白名单中。\n"
        f"消息发出后无法收回,因此本工具默认禁止向任何会话发送。\n"
        f"如确认要授权该会话,请执行:\n"
        f"    emcli policy add {chat_id}"
        + ("\n(注意:这是群会话,发送等于向群内所有人广播)" if is_group else "")
    )


def _confirm_send(ctx: Ctx, conv: dict, text: str, yes: bool) -> None:
    """发送前的人工确认。

    这里属于**功能数据面**:会话 id 原样给出(用户要拿它去 `policy add` /
    复述目标),因此**不掩码**;但必须**同时**给出会话名 —— 单聊 id 是
    ``对方id,我的id`` 这种长复合串,只看 id 很难确认到底发给谁。
    """
    chat_id = str(conv.get("targetid") or "")
    is_group = ctx.session.is_group(conv)
    kind = "群聊(广播给群内所有人)" if is_group else "单聊"
    if ctx.out.json_mode and not yes:
        raise SendBlocked("JSON 模式下必须显式加 --yes 才会真正发送")
    if yes or ctx.out.json_mode:
        return
    name = str(conv.get("targetname") or "").strip()
    if not name or name == chat_id:
        name = _lookup_conversation_name(ctx, chat_id) or "(未能解析出会话名)"
    click.echo(f"即将发送:")
    click.echo(f"  会话: {name}  [{kind}]")
    click.echo(f"  id  : {chat_id}")
    click.echo(f"  内容: {text!r}  ({len(text)} 字)")
    if not click.confirm("确认发送?(消息发出后无法撤回)"):
        raise click.Abort()


def _confirm_operation(ctx: Ctx, action: str, detail: str, yes: bool) -> None:
    """通用人工确认(文件发送等)。"""
    if ctx.out.json_mode and not yes:
        raise SendBlocked("JSON 模式下必须显式加 --yes 才会真正执行")
    if yes or ctx.out.json_mode:
        return
    click.echo(f"即将{action}:")
    click.echo(f"  详情: {detail}")
    if not click.confirm("确认执行?(发出后无法撤回)"):
        raise click.Abort()


@chat.command("send")
@click.argument("conversation")
@click.argument("text")
@click.option("--id", "use_id", is_flag=True,
              help="把 <会话> 当作原始 id(默认按名字解析)")
@click.option("--dry-run", is_flag=True,
              help="只打印将发送的 XMPP stanza,不真的发送")
@click.option("--yes", "-y", is_flag=True,
              help="跳过交互确认(仍需该会话在发送白名单内)")
@pass_ctx
def chat_send(ctx: Ctx, conversation: str, text: str, use_id: bool,
              dry_run: bool, yes: bool) -> None:
    """向会话发送文本消息。<会话> 可传 id 或会话名。

    安全护栏(默认零信任):
      1. 会话必须已在 `emcli policy add` 的白名单里,否则拒绝发送;
      2. 发送前需交互确认(--yes 跳过);
      3. `--dry-run` 只打印 stanza,不受以上限制。
    """
    out = ctx.out
    try:
        if use_id:
            conv = {"targetid": conversation, "targetname": conversation,
                    "targettype": "1" if "-" in conversation else "0"}
        else:
            conv = ctx.session.resolve_conversation(conversation)
        is_group = ctx.session.is_group(conv)
        chat_id = str(conv.get("targetid"))
        to_jid = XMPPClient.chat_jid(chat_id, ctx.session.config.msg_udid,
                                     ctx.session.config.msg_domain, is_group)
        if dry_run:
            cfg = ctx.session.config
            body = json.dumps({"objectName": "RC:TxtMsg", "content": text,
                               "extra": {}}, ensure_ascii=False)
            # `from` 是**自己的** JID,node 里含工号:排障输出默认掩码,
            # domain / resource 保留(--show-identity 时才给完整值)。
            own_jid = (f"{cfg.base_user_id}|{cfg.msg_udid}"
                       f"@{cfg.msg_domain}/{cfg.resource}")
            from_jid = _mask_jid(ctx, own_jid)
            # `to` / `conversation.id` 是会话 id:单聊为 ``对方id,我的id``,
            # 里面既有对方工号也有**本人**工号,排障面默认逐段掩码。
            shown_to = _mask_chat_id(ctx, to_jid)
            shown_conv = _masked_conv_brief(ctx, conv)
            stanza = ("<message id='<uuid>' to='%s' from='%s' type='chat'>"
                      "<body>%s</body></message>" % (
                          shown_to, from_jid, body))
            payload = {"ok": True, "dry_run": True, "to": shown_to,
                       "conversation": shown_conv, "stanza": stanza}
            if out.json_mode:
                out.data(payload)
            else:
                out.info(f"[dry-run] 目标: {shown_conv.get('name')}  <{shown_to}>")
                out.info(f"[dry-run] stanza:\n{stanza}")
            return

        _require_send_permission(ctx, chat_id, conv)
        _confirm_send(ctx, conv, text, yes)

        with ctx.session.xmpp() as client:
            msg_id = client.send_text(chat_id, text,
                                      udid=ctx.session.config.msg_udid,
                                      is_group=is_group)
        payload = {"ok": True, "msgid": msg_id,
                   "to": _conv_brief(conv, _lookup_conversation_name(ctx, chat_id)),
                   "chars": len(text)}
        if out.json_mode:
            out.data(payload)
        else:
            out.success(f"已发送到「{_conversation_name(ctx, conv)}」"
                        f"({len(text)} 字) msgid={msg_id}")
    except click.Abort:
        out.info("已取消,未发送")
        raise SystemExit(1)
    except SendBlocked as exc:
        if out.json_mode:
            out.data({"ok": False, "blocked": True, "error": exc.message})
        else:
            out.error(exc.message)
        raise SystemExit(2)
    except Exception as exc:  # noqa: BLE001
        out.error(str(exc))
        raise SystemExit(1)


@chat.command("send-file")
@click.argument("conversation")
@click.argument("file_path", type=click.Path())
@click.option("--yes", "-y", is_flag=True, help="跳过交互确认")
@click.option("--attempts", type=int, default=6,
              help="复核次数(默认 6,配 10s 间隔约 1 分钟)")
@click.option("--interval", "interval_s", type=int, default=10,
              help="每次复核间隔秒数(默认 10)")
@click.option("--first-delay", "first_delay", type=int, default=10,
              help="发送后首次复核前等待秒数(默认 10,别一发完就查)")
@click.option("--no-wait", is_flag=True, help="提交后立即返回,不复核")
@click.option("--no-open", is_flag=True,
              help="不自动切换到目标会话(默认会自动在左侧列表点开它)")
@pass_ctx
def chat_send_file(ctx: Ctx, conversation: str, file_path: str, yes: bool,
                   attempts: int, interval_s: int, first_delay: int,
                   no_wait: bool, no_open: bool) -> None:
    """发送文件(驱动官方客户端完成,走拖放+发送按钮)。

    会自动:确保 E-Mobile 带调试端口运行 → 在左侧会话列表点开目标会话
    → 拖放文件 → 点发送 → 轮询会话历史校验落地。
    """
    import os

    from .gui import GuiClient
    out = ctx.out
    try:
        path = os.path.abspath(file_path)
        if not os.path.isfile(path):
            raise click.ClickException(f"文件不存在: {path}")
        conv = ctx.session.resolve_conversation(conversation)
        chat_id = str(conv.get("targetid"))
        conv_name = _conversation_name(ctx, conv)
        size_mb = os.path.getsize(path) / 1024 / 1024

        _require_send_permission(ctx, chat_id, conv)
        _confirm_operation(ctx, f"发送文件到「{conv_name}」",
                           f"{os.path.basename(path)} ({size_mb:.1f} MB)", yes)

        # 发送前的最新消息时间,用于判断"新"文件消息(避免匹配到同名旧消息)
        def probe():
            with ctx.session.xmpp() as client:
                data = client.get_history(chat_id, target_type="1", count=10)
            msgs = []
            for item in (data.get("history") or []):
                try:
                    body = json.loads(item.get("opBody") or "{}")
                except Exception:  # noqa: BLE001
                    body = {}
                msgs.append({"content": str(body.get("content")),
                             "objectName": body.get("objectName"),
                             "sendTime": str(item.get("sendTime") or ""),
                             "extra": body.get("extra")})
            return msgs

        since = None
        try:
            before = probe()
            if before:
                since = before[0].get("sendTime")
        except Exception:  # noqa: BLE001
            pass

        with GuiClient(log=lambda m: out.info(m)) as gui:
            name = conv_name
            if no_open:
                gui.assert_conversation(name)
            else:
                out.info(f"正在打开会话「{name}」...")
                gui.open_conversation(name)
            result = gui.send_file(path, name)
            sent = {"ok": True,
                    "to": _conv_brief(conv, _lookup_conversation_name(ctx, chat_id)),
                    "file": result["fileName"], "fileSize": result["fileSize"]}
            if not no_wait:
                try:
                    item = gui.wait_for_file_message(
                        result["fileName"], probe, since_time=since,
                        attempts=attempts, interval_s=interval_s,
                        first_delay_s=first_delay)
                    sent["confirmed"] = True
                    sent["objectName"] = item.get("objectName")
                    sent["sendTime"] = item.get("sendTime")
                except Exception as exc:  # noqa: BLE001
                    sent["confirmed"] = False
                    sent["note"] = (f"已提交,但未能在约定窗口内确认: {exc}")

        if out.json_mode:
            out.data(sent)
        else:
            out.success(f"文件已发送到「{conv_name}」: "
                        f"{result['fileName']} ({size_mb:.1f} MB)")
            if sent.get("confirmed"):
                out.success("会话中已确认收到文件消息")
            elif "note" in sent:
                out.info(sent["note"])
    except SendBlocked as exc:      # 必须先于 ClickException(SendBlocked 是其子类)
        if out.json_mode:
            out.data({"ok": False, "blocked": True, "error": exc.message})
        else:
            out.error(exc.message)
        raise SystemExit(2)
    except click.Abort:
        out.info("已取消,未发送")
        raise SystemExit(1)
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001
        out.error(str(exc))
        raise SystemExit(1)


@chat.command("send-image")
@click.argument("conversation")
@click.argument("file_path", type=click.Path())
@click.option("--yes", "-y", is_flag=True, help="跳过交互确认")
@click.option("--wait", "wait_s", type=int, default=30,
              help="发送后等待输入框清空的秒数(默认 30)")
@click.option("--no-open", is_flag=True,
              help="不自动切换到目标会话(默认会自动在左侧列表点开它)")
@pass_ctx
def chat_send_image(ctx: Ctx, conversation: str, file_path: str, yes: bool,
                    wait_s: int, no_open: bool) -> None:
    """发送图片(以「图片消息」发出,不是文件附件)。

    与 `chat send-file` 的区别:后者把图片当**普通文件**拖放,客户端不接受
    image 类型的拖放(实测报「拖放后输入框里没有文件」)。本命令改走聊天页的
    隐藏 `input[type=file]`(兜底 quill insertEmbed),图片在输入框就绪后才点发送,
    因此收件方看到的是可预览的图片消息。
    """
    import os

    from .gui import GuiClient
    out = ctx.out
    try:
        path = os.path.abspath(file_path)
        if not os.path.isfile(path):
            raise click.ClickException(f"图片不存在: {path}")
        size_mb = os.path.getsize(path) / 1024 / 1024
        conv = ctx.session.resolve_conversation(conversation)
        chat_id = str(conv.get("targetid"))
        conv_name = _conversation_name(ctx, conv)

        _require_send_permission(ctx, chat_id, conv)
        _confirm_operation(ctx, f"发送图片到「{conv_name}」",
                           f"{os.path.basename(path)} ({size_mb:.1f} MB)", yes)

        with GuiClient(log=lambda m: out.info(m)) as gui:
            name = conv_name
            if no_open:
                gui.assert_conversation(name)
            else:
                out.info(f"正在打开会话「{name}」...")
            result = gui.send_image(path, name, timeout_s=wait_s)
            sent = {"ok": True,
                    "to": _conv_brief(conv, _lookup_conversation_name(ctx, chat_id)),
                    "image": result["fileName"], "fileSize": result["fileSize"],
                    "route": result.get("route"), "via": "gui-image"}

        if out.json_mode:
            out.data(sent)
        else:
            out.success(f"图片已发送到「{conv_name}」: "
                        f"{result['fileName']} ({size_mb:.1f} MB,"
                        f"路径={result.get('route')})")
    except SendBlocked as exc:
        if out.json_mode:
            out.data({"ok": False, "blocked": True, "error": exc.message})
        else:
            out.error(exc.message)
        raise SystemExit(2)
    except click.Abort:
        out.info("已取消,未发送")
        raise SystemExit(1)
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001
        out.error(str(exc))
        raise SystemExit(1)


@chat.command("send-gui")
@click.argument("conversation")
@click.argument("text", required=False)
@click.option("--text-file", type=click.Path(exists=True, dir_okay=False),
              default=None,
              help="从 UTF-8 文本文件读取正文(多行推荐;换行会原样保留)")
@click.option("--yes", "-y", is_flag=True, help="跳过交互确认")
@click.option("--no-open", is_flag=True,
              help="不自动切换到目标会话(默认会自动在左侧列表点开它)")
@pass_ctx
def chat_send_gui(ctx: Ctx, conversation: str, text: Optional[str],
                  text_file: Optional[str], yes: bool, no_open: bool) -> None:
    """发送文本(驱动官方客户端:打开会话 → 输入 → 点发送)。

    与 `chat send` 的区别:后者自拼 XMPP 消息,服务端会静默丢弃;
    本命令走 GUI,以本人身份经官方通道发出,与手敲等价。

    **多行正文**:直接写在命令行里时,换行会被 shell 与编辑器吃掉,所以多行
    请用 `--text-file <文件>`(推荐),或确保传入的 TEXT 本身含真实换行。
    写入统一走 quill `setText()`,换行会真正落进输入框。
    """
    out = ctx.out
    try:
        if text_file:
            with open(text_file, "r", encoding="utf-8") as fh:
                body = fh.read().strip("\r\n")
        elif text is not None:
            body = text
        else:
            raise click.ClickException("请给出 TEXT 或 --text-file <文件>")
        if not body.strip():
            raise click.ClickException("正文为空,已阻止发送")

        conv = ctx.session.resolve_conversation(conversation)
        chat_id = str(conv.get("targetid"))
        name = _conversation_name(ctx, conv)

        _require_send_permission(ctx, chat_id, conv)
        _confirm_send(ctx, conv, body, yes)

        with GuiClient(log=lambda m: out.info(m)) as gui:
            if no_open:
                gui.assert_conversation(name)
            else:
                out.info(f"正在打开会话「{name}」...")
            gui.send_text(body, expect_title=None if no_open else name)

        payload = {"ok": True,
                   "to": _conv_brief(conv, _lookup_conversation_name(ctx, chat_id)),
                   "chars": len(body),
                   "lines": body.count("\n") + 1, "via": "gui"}
        if out.json_mode:
            out.data(payload)
        else:
            out.success(f"已发送到「{name}」({len(body)} 字,"
                        f"{body.count(chr(10)) + 1} 行)")
    except SendBlocked as exc:
        if out.json_mode:
            out.data({"ok": False, "blocked": True, "error": exc.message})
        else:
            out.error(exc.message)
        raise SystemExit(2)
    except click.Abort:
        out.info("已取消,未发送")
        raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001
        out.error(str(exc))
        raise SystemExit(1)


# --------------------------------------------------------------------------- #
# policy:发送白名单(安全护栏)
# --------------------------------------------------------------------------- #
@cli.group()
def policy() -> None:
    """发送白名单 —— 防止误发的安全护栏。"""


@policy.command("show")
@pass_ctx
def policy_show(ctx: Ctx) -> None:
    """查看当前允许发送的会话。"""
    allow = list(ctx.session.config.send_allowlist or [])
    if ctx.out.json_mode:
        ctx.out.data({"ok": True, "count": len(allow), "allowlist": allow})
        return
    if not allow:
        ctx.out.info("白名单为空 → 当前禁止向任何会话发送。\n"
                     "授权:`emcli policy add <会话 id 或会话名>`")
        return
    rows = []
    for cid in allow:
        name = cid
        try:
            conv = ctx.session.resolve_conversation(cid)
            name = conv.get("targetname") or cid
        except Exception:  # noqa: BLE001
            name = f"{cid} (解析不到,可能已不存在)"
        rows.append([cid, name])
    ctx.out.table(rows, headers=["会话 id", "会话名"],
                  title=f"发送白名单({len(allow)})")


@policy.command("add")
@click.argument("conversation")
@click.option("--force", is_flag=True,
              help="跳过会话名校验,直接按给定值加入白名单")
@pass_ctx
def policy_add(ctx: Ctx, conversation: str, force: bool) -> None:
    """把会话加入发送白名单(可用会话名)。"""
    cfg = ctx.session.config
    query = (conversation or "").strip()
    if not query:
        ctx.out.error("请提供会话 id 或会话名")
        raise SystemExit(1)

    chat_id, name, note = query, query, ""
    if not force:
        try:
            convs = ctx.session.conversations(force=True)
        except Exception as exc:  # noqa: BLE001
            # 读不到会话列表时不阻塞配置(E-Mobile 可能没开),按原值加入并提示
            convs = []
            note = f"(未能校验会话是否存在:{exc})"
        for conv in convs:
            if str(conv.get("targetid")) == query:
                chat_id = query
                name = conv.get("targetname") or query
                break
        else:
            try:
                conv = ctx.session.resolve_conversation(query)
                chat_id = str(conv.get("targetid"))
                name = conv.get("targetname") or chat_id
            except Exception:  # noqa: BLE001
                if convs:
                    ctx.out.error(f"找不到会话 “{query}”。"
                                  "确认无误可加 --force 直接按该值加入白名单。")
                    raise SystemExit(1)
                # 没拿到会话列表,只能按原值信任

    if chat_id in (cfg.send_allowlist or []):
        ctx.out.info(f"「{name}」已在白名单中")
        return
    cfg.send_allowlist = list(cfg.send_allowlist or []) + [chat_id]
    cfg.save()
    if ctx.out.json_mode:
        ctx.out.data({"ok": True, "added": chat_id, "name": name,
                      "allowlist": cfg.send_allowlist})
    else:
        ctx.out.success(f"已授权发送:「{name}」({chat_id}){note}")
        ctx.out.info(f"现在可以 `emcli chat send \"{name}\" <消息> --yes`")


@policy.command("remove")
@click.argument("conversation")
@pass_ctx
def policy_remove(ctx: Ctx, conversation: str) -> None:
    """把会话从白名单移除。"""
    cfg = ctx.session.config
    allow = list(cfg.send_allowlist or [])
    if conversation in allow:
        allow.remove(conversation)
    else:  # 允许按名字移除
        try:
            conv = ctx.session.resolve_conversation(conversation)
            cid = str(conv.get("targetid"))
            if cid in allow:
                allow.remove(cid)
                conversation = cid
        except Exception:  # noqa: BLE001
            pass
    cfg.send_allowlist = allow
    cfg.save()
    if ctx.out.json_mode:
        ctx.out.data({"ok": True, "removed": conversation,
                      "allowlist": allow})
    else:
        ctx.out.success(f"已移除:{conversation}")


@policy.command("clear")
@pass_ctx
def policy_clear(ctx: Ctx) -> None:
    """清空白名单(禁止一切发送)。"""
    cfg = ctx.session.config
    cfg.send_allowlist = []
    cfg.save()
    if ctx.out.json_mode:
        ctx.out.data({"ok": True, "allowlist": []})
    else:
        ctx.out.success("白名单已清空,当前禁止向任何会话发送")


@chat.command("watch")
@click.option("--seconds", type=float, default=30.0, help="监听时长(秒)")
@click.option("--from", "sender", default="", help="只看某发送者 id")
@click.option("--no-receipt", is_flag=True, help="不发读后回执")
@pass_ctx
def chat_watch(ctx: Ctx, seconds: float, sender: str, no_receipt: bool) -> None:
    """监听新消息并自动回执(常用于配合自动化)。"""
    out = ctx.out
    try:
        my_id = ctx.session.config.base_user_id
        with ctx.session.xmpp() as client:
            messages = client.drain_messages(seconds)
        if sender:
            messages = [m for m in messages if m.get("sender") == sender]
        collected = [m for m in messages if m.get("sender") != my_id]
        if out.json_mode:
            out.data({"ok": True, "seconds": seconds, "count": len(collected),
                      "messages": collected})
        else:
            for m in collected:
                out.info(f"[{time.strftime('%H:%M:%S', time.localtime(m['received_at']))}] "
                         f"{m['sender']}: {m['content']}")
            out.info(f"监听结束,收到 {len(collected)} 条消息")
    except Exception as exc:  # noqa: BLE001
        out.error(str(exc))
        raise SystemExit(1)


# --------------------------------------------------------------------------- #
# contact / group / ding
# --------------------------------------------------------------------------- #
@cli.group()
def contact() -> None:
    """联系人。"""


@contact.command("search")
@click.argument("keyword")
@click.option("-n", "--limit", type=int, default=20)
@click.option("--simple", is_flag=True,
              help="用 baseuser/search(不需要 corpid,结果更全)")
@pass_ctx
def contact_search(ctx: Ctx, keyword: str, limit: int, simple: bool) -> None:
    """搜索组织内人员/群。"""
    out = ctx.out
    try:
        if simple:
            data = ctx.session.rest.baseuser_search(keyword, limit=limit)
        else:
            data = ctx.session.rest.user_search(keyword, limit=limit)
        users = data.get("userlist") or data.get("users") or []
        rows = []
        for u in users:
            rows.append({
                "id": u.get("base_user_id") or u.get("id"),
                "name": u.get("name") or u.get("base_user_name"),
                "dept": u.get("deptName") or u.get("subdeptname") or "",
                "position": u.get("position") or "",
                "state": u.get("work_cond_txt") or "",
                "mobile": _mask_mobile(u.get("mobile")),
            })
        if out.json_mode:
            out.data({"ok": True, "count": len(rows), "users": rows})
        else:
            out.table([[r["id"], r["name"], r["dept"], r["position"], r["state"]]
                       for r in rows],
                      headers=["id", "姓名", "部门", "职务", "状态"],
                      title=f"搜索 “{keyword}” 结果({len(rows)})")
    except Exception as exc:  # noqa: BLE001
        out.error(str(exc))
        raise SystemExit(1)


def _mask_mobile(mobile: Any) -> str:
    """手机号打码,避免明文外泄。

    统一走 `config.mask_secret`(可见字符 ≤ 4 + 长度提示)。旧实现是
    "前 3 + `****` + 后 4",11 位手机号会露 7 位 —— 等于半个号码,不符合
    "敏感标识可见字符 ≤ 4"的原则。现在 11 位手机号显示为 `13…89(11)`,
    仍能凭前 2 + 后 2 位与用户核对("是不是这个号"),但不足以还原号码。
    空值/过短值仍返回空串(与旧行为一致,避免把空字段渲染成 `-`)。
    """
    text = str(mobile or "")
    if len(text) < 7:
        return ""
    return mask_secret(text)


@cli.group()
def group() -> None:
    """群组。"""


@group.command("list")
@click.option("-n", "--limit", type=int, default=100)
@pass_ctx
def group_list(ctx: Ctx, limit: int) -> None:
    """我加入的群组。"""
    out = ctx.out
    try:
        data = ctx.session.rest.group_list(limit=limit)
        groups = data.get("groups") or data.get("mylist") or []
        if out.json_mode:
            out.data({"ok": True, "count": len(groups), "groups": groups})
        else:
            out.table([[g.get("groupid") or g.get("id"),
                        g.get("groupname") or g.get("name"),
                        g.get("membercount") or g.get("count"), g.get("createtime")]
                       for g in groups],
                      headers=["群 id", "群名", "人数", "创建时间"],
                      title=f"我的群组({len(groups)})")
    except Exception as exc:  # noqa: BLE001
        out.error(str(exc))
        raise SystemExit(1)


@cli.group()
def ding() -> None:
    """必达(强提醒)。"""


@ding.command("list")
@click.option("-n", "--limit", type=int, default=20)
@pass_ctx
def ding_list(ctx: Ctx, limit: int) -> None:
    """必达列表。"""
    out = ctx.out
    try:
        with ctx.session.xmpp() as client:
            data = client.ding_list(count=limit)
        items = data.get("dingList") or data.get("list") or []
        if out.json_mode:
            out.data({"ok": True, "count": len(items), "items": items})
        else:
            out.table([[i.get("id"), i.get("title") or i.get("content"),
                        i.get("sendername"), ctx.session.ts(i.get("sendtime"))]
                       for i in items],
                      headers=["id", "标题", "发送者", "时间"],
                      title=f"必达({len(items)})")
    except Exception as exc:  # noqa: BLE001
        out.error(str(exc))
        raise SystemExit(1)


# --------------------------------------------------------------------------- #
# db(本地只读)
# --------------------------------------------------------------------------- #
@cli.group()
def db() -> None:
    """本地 em7.db 只读查询。"""


@db.command("overview")
@pass_ctx
def db_overview(ctx: Ctx) -> None:
    """本地库概览。"""
    from . import localdb
    out = ctx.out
    if not localdb.exists():
        out.error(f"未找到 {localdb.db_path()}(先登录一次 E-Mobile)")
        raise SystemExit(1)
    conn = localdb.connect_ro()
    try:
        payload = {"ok": True, "path": str(localdb.db_path()),
                   "tables": localdb.tables(conn),
                   "items_by_type": localdb.q_items_summary(conn),
                   "urls": localdb.q_urls(conn)}
        if out.json_mode:
            out.data(payload)
        else:
            out.table([[t, c, (int(b or 0))] for t, c, b in
                       [(r["type_name"], r["count"], r["bytes"])
                        for r in payload["items_by_type"]]],
                      headers=["项目类型", "条数", "字节"],
                      title=f"本地库 {payload['path']}")
            out.info("\n已配置的服务器:")
            for u in payload["urls"]:
                out.info(f"  {u.get('url')}  (最后登录 "
                        f"{time.strftime('%F %T', time.localtime(int(u.get('lastLoginTime') or 0) / 1000))})")
    finally:
        conn.close()


@db.command("urls")
@pass_ctx
def db_urls(ctx: Ctx) -> None:
    """已配置的服务器地址。"""
    from . import localdb
    out = ctx.out
    conn = localdb.connect_ro()
    try:
        rows = localdb.q_urls(conn)
    finally:
        conn.close()
    if out.json_mode:
        out.data({"ok": True, "urls": rows})
    else:
        out.table([[r["url"], time.strftime('%F %T', time.localtime(
            int(r.get('lastLoginTime') or 0) / 1000))] for r in rows],
            headers=["服务器", "最后登录"], title="服务器地址")


@db.command("users")
@pass_ctx
def db_users(ctx: Ctx) -> None:
    """本地保存的账号(不含密码)。

    **显式原始数据命令**:`baseUserId` 是查询本机缓存账号的**数据本身**,
    因此这里**有意不掩码**(与登录态展示面/排障面不同)。文档里已把它和
    `db items --value` / `contact search` 一起列为脱敏例外;不要为了"统一"
    把它改掉,否则这个命令就失去了查询价值。
    """
    from . import localdb
    out = ctx.out
    conn = localdb.connect_ro()
    try:
        rows = localdb.q_users(conn)
    finally:
        conn.close()
    if out.json_mode:
        out.data({"ok": True, "users": rows})
    else:
        out.table([[r["loginId"], r["baseUserId"], r["password_saved"],
                    r["lastLoginTime"]] for r in rows],
                  headers=["登录名", "baseUserId", "已存密码", "最后登录"],
                  title="本地账号(密码不回显;baseUserId 为原始数据)")


@db.command("items")
@click.option("--type", "type_", default="", help="1 file/2 image/3 search/4 draft/5 system/6 chatlist")
@click.option("--key", default="", help="key 模糊匹配")
@click.option("-n", "--limit", type=int, default=50)
@click.option("--value", "show_value", is_flag=True, help="显示 value 原文")
@pass_ctx
def db_items(ctx: Ctx, type_: str, key: str, limit: int, show_value: bool) -> None:
    """KV 缓存列表。"""
    from . import localdb
    out = ctx.out
    conn = localdb.connect_ro()
    try:
        rows = localdb.q_items(conn, int(type_) if type_.isdigit() else None,
                               key, limit)
        if show_value:
            for r in rows:
                full = localdb.q_item_value(conn, r["key"])
                r["value"] = (full or {}).get("value")
    finally:
        conn.close()
    if out.json_mode:
        out.data({"ok": True, "count": len(rows), "items": rows})
    else:
        out.table([[r["id"], r["type_name"], r["key"], r["size"], r["url"]]
                   for r in rows],
                  headers=["id", "类型", "key", "大小", "url"],
                  title=f"KV 缓存({len(rows)} 条)")


@db.command("chatlist")
@pass_ctx
def db_chatlist(ctx: Ctx) -> None:
    """读取 chatlist 缓存(type=6)。"""
    from . import localdb
    out = ctx.out
    conn = localdb.connect_ro()
    try:
        payload = localdb.q_chatlist(conn)
    finally:
        conn.close()
    if not payload:
        out.error("本地没有 chatlist 缓存(在 E-Mobile 里点开会话列表后重试)")
        raise SystemExit(1)
    if out.json_mode:
        out.data({"ok": True, **payload})
    else:
        out.info(json.dumps(payload, ensure_ascii=False)[:4000])


# --------------------------------------------------------------------------- #
# REPL
# --------------------------------------------------------------------------- #
@cli.command()
@pass_ctx
def repl(ctx: Ctx) -> None:
    """交互模式(裸命令即进入)。"""
    if ctx.out.json_mode:
        ctx.out.set_output(Output(json_mode=False))
    cfg = ctx.session.config
    print(f"E-Mobile CLI v{__version__}  ·  仅本机使用,不发布")
    masked_user = _masked_user_fields(cfg)["user"]
    print(f"用户: {masked_user if masked_user != '-' else '(未登录)'}"
          f"  服务器: {cfg.base_url or '-'}")
    print("输入 help 查看命令,q 退出。\n")
    while True:
        try:
            raw = input("emcli> ")
        except (EOFError, KeyboardInterrupt):
            print("\n再见")
            return
        # 管道输入可能带 BOM / 首尾空白
        line = raw.lstrip("\ufeff").strip()
        if not line:
            continue
        if line in ("q", "quit", "exit"):
            print("再见")
            return
        if line in ("help", "?"):
            print("  status [--probe]                状态总览\n"
                  "  token refresh|set|clear         token 管理\n"
                  "  chat list [-n N] [--unread]     会话列表\n"
                  "  chat history <会话> [-n N]      历史消息\n"
                  "  chat send <会话> <消息>         发送消息(需白名单授权+确认)\n"
                  "  chat watch [--seconds S]        监听消息\n"
                  "  contact search <关键词>         搜人\n"
                  "  group list                      我的群组\n"
                  "  ding list                       必达\n"
                  "  policy show|add|remove|clear    发送白名单(防误发护栏)\n"
                  "  db overview|urls|users|items    本地库只读\n"
                  "  # 例: policy add 张三   然后 chat send 张三 你好 --yes")
            continue
        args = _repl_parse(line)
        try:
            cli.main(args=args, prog_name="emcli", standalone_mode=False, obj=ctx)
        except SystemExit:
            pass
        except click.ClickException as exc:
            exc.show()
        except Exception as exc:  # noqa: BLE001
            print(f"✗ {exc}")


def _repl_parse(line: str) -> list:
    """REPL 行 → argv。

    * 容忍首尾空白和 BOM(管道输入常带)。
    * `chat send <会话> <消息>` 的正文要保留空格,所以单独处理:
      第 3 段是会话名,之后的内容(去掉 -- 开头的开关)原样拼回正文。
    """
    line = (line or "").lstrip("\ufeff").strip()
    try:
        tokens = [t for t in shlex.split(line, posix=False) if t]
    except ValueError:
        tokens = line.split()
    if len(tokens) >= 3 and tokens[0] == "chat" and tokens[1] == "send":
        rest = tokens[2:]
        # 抽出开关(可能出现在正文里)
        flags, body = [], []
        for token in rest:
            if token.startswith("--"):
                flags.append(token)
            else:
                body.append(token)
        if len(body) < 2:
            print("✗ 用法: chat send <会话> <消息>")
            return ["--help"]
        conversation = body[0]
        text = " ".join(body[1:]).strip()
        return ["chat", "send", conversation, text] + flags
    return tokens


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _conv_brief(conv: dict, name: Optional[str] = None) -> dict:
    """会话摘要(**功能数据面**:id 原样,不掩码)。

    `name` 传入时优先用它 —— 发送类命令在 `targetname` 缺失时会按 id 去
    会话列表里补一个会话名(见 `_lookup_conversation_name`),避免输出里
    只有一串 `对方id,我的id` 而没有可读目标。
    """
    return {"id": conv.get("targetid"), "name": name or conv.get("targetname"),
            "type": conv.get("targettype")}


def _lookup_conversation_name(ctx: Ctx, chat_id: str) -> str:
    """按会话 id 从会话列表里补**会话名**;查不到(或离线)返回空串。

    只用于给功能数据面的输出补一个可读名字,绝不改变会话 id 本身,也
    绝不因为查不到名字就让命令失败。
    """
    cid = str(chat_id or "").strip()
    if not cid:
        return ""
    try:
        for item in ctx.session.conversations():
            if str(item.get("targetid") or "") == cid:
                return str(item.get("targetname") or "").strip()
    except Exception:  # noqa: BLE001
        # 离线 / 拿不到会话列表:名字补不上就算了,不影响主流程
        pass
    return ""


def _conversation_name(ctx: Ctx, conv: dict) -> str:
    """会话的**可读名**:优先 `targetname`;缺失时按 id 去会话列表补;再没有给 id。"""
    chat_id = str(conv.get("targetid") or "").strip()
    name = str(conv.get("targetname") or "").strip()
    if name and name != chat_id:
        return name
    found = _lookup_conversation_name(ctx, chat_id)
    return found or chat_id or "-"


def _masked_conv_brief(ctx: Ctx, conv: dict) -> dict:
    """**排障面**用的会话摘要:会话 id 走 `_mask_chat_id`。

    只在 `chat send --dry-run` 这类排障输出里用;功能数据面仍用
    `_conv_brief`。`--id` 模式下 `targetname` 可能就是 id 本身,同样掩码,
    否则 human 的「目标」行会把完整复合 id 漏出去。
    """
    brief = _conv_brief(conv)
    raw_id = str(conv.get("targetid") or "")
    brief["id"] = _mask_chat_id(ctx, brief.get("id"))
    if str(brief.get("name") or "") == raw_id:
        brief["name"] = brief["id"]
    return brief


def _clean_content(conv: dict) -> str:
    content = str(conv.get("msgcontent") or conv.get("content") or "")
    return content.replace("\n", " ")[:40]


def _parse_op_body(op_body: Any) -> dict:
    import json
    if isinstance(op_body, dict):
        return op_body
    if not op_body:
        return {}
    try:
        return json.loads(op_body)
    except (TypeError, ValueError):
        return {}


def _preview(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        if content.startswith("data:") or (len(content) > 200 and "/" in content[:8]):
            return "[二进制/图片]"
        return content.replace("\n", " ")[:80]
    return str(content)[:80]


# --------------------------------------------------------------------------- #
# compat:版本兼容能力探测(只读,诊断用)
# --------------------------------------------------------------------------- #
def _disk_diagnose_brief(disk: Dict[str, Any]) -> Dict[str, Any]:
    """把 `localstore.diagnose()` 的结果压成**不含任何值**的摘要。

    只保留:数据目录与是否存在、各文件的记录数、出现的 origin 与键名、
    登录态键的 origin/键名、token 是否存在与长度、选中的 origin/键名。

    安全红线:绝不带出 token 内容;也**不**带 `sensitive_fields` 这类敏感
    字段名清单(那是 `localstore.diagnose()` 的内部诊断字段)。
    """
    files = disk.get("files") or []
    files_text = "; ".join(
        f"{f.get('name')}({f.get('kind')}):{f.get('entries')} 条"
        + (f" [错误:{f.get('error')}]" if f.get("error") else "")
        for f in files) or "(无 leveldb 文件)"
    current = disk.get("current_info") or []
    current_text = "; ".join(
        f"{c.get('origin')}/{c.get('key')}"
        f"(token={'有' if c.get('has_token') else '无'},"
        f"长度={c.get('token_len') or 0},字节={c.get('value_bytes')})"
        for c in current) or "(未发现 *-current-info 键)"
    keys = list(disk.get("keys") or [])
    shown = keys[:20]
    keys_text = "、".join(shown) or "(无)"
    if len(keys) > len(shown):
        keys_text += f" …(共 {len(keys)} 个键)"
    return {
        "disk_dir": disk.get("directory"),
        "disk_dir_exists": disk.get("exists"),
        "disk_files": files_text,
        "disk_records": disk.get("records"),
        "disk_origins": "、".join(disk.get("origins") or []) or "(无)",
        "disk_keys": keys_text,
        "disk_current_info": current_text,
        "token_found": bool(disk.get("token_found")),
        "token_len": disk.get("token_len") or 0,
        "token_origin": disk.get("origin"),
        "token_key": disk.get("key"),
    }


@cli.command()
@click.option("--probe", is_flag=True,
              help="实际探测 REST/XMPP(会产生网络请求,只读)")
@pass_ctx
def compat(ctx: Ctx, probe: bool) -> None:
    """列出本机 E-Mobile 版本、选中档案与逐项能力,便于换版本后定位问题。

    只读:不发送任何消息,不需要白名单授权。任一项探测失败**不影响退出码**
    (这是诊断命令,不是断言命令)。缺哪项就补哪项,报告里会给出建议。
    """
    out = ctx.out
    from . import app as appmod, cdp as cdpmod, compat as compatmod
    from . import localdb as localdbmod
    from . import localstore as localstoremod

    checks: List[dict] = []

    # --- 1. exe 发现 --- #
    exes = appmod.discover_exes()
    exe = exes[0] if exes else None
    checks.append({
        "name": "e-mobile exe",
        "ok": bool(exes),
        "detail": {"found": exes,
                   "note": "未找到时可设 EMCLI_EMOBILE_EXE 指定完整路径"},
    })

    # --- 2. 客户端版本 --- #
    version = compatmod.detect_client_version(exe_path=exe)
    profile = compatmod.select_profile(version or "unknown")
    checks.append({
        "name": "client version",
        "ok": version is not None,
        "detail": {"detected": str(version) if version else None,
                   "profile": profile.name,
                   "notes": profile.notes},
    })

    # --- 3. 调试端口 / CDP --- #
    port = CDP_PORT
    cdp_ok = cdpmod.cdp_available(port)
    checks.append({
        "name": "cdp port",
        "ok": cdp_ok,
        "detail": {"port": port,
                   "hint": f"E-Mobile 需以 --remote-debugging-port={port} 启动"},
    })

    # --- 4. localStorage 登录态键 --- #
    ls_keys: List[str] = []
    hit_key = None
    if cdp_ok:
        try:
            ls_keys = cdpmod.list_localstorage_keys(port)
            profile_keys = list(profile.localstorage_token_keys)
            hit_key = next((k for k in profile_keys if k in ls_keys), None)
        except Exception as exc:  # noqa: BLE001
            ls_keys = []
            checks.append({"name": "localstorage read", "ok": False,
                           "error": str(exc)[:300]})
    known = [k for k in ls_keys if "current-info" in k]
    ok = bool(hit_key or known)
    checks.append({
        "name": "login-state key",
        "ok": ok,
        "detail": {"profile_candidates": list(profile.localstorage_token_keys),
                   "hit": hit_key,
                   "page_keys_with_current_info": known,
                   "page_key_count": len(ls_keys),
                   "note": "读不到时改用 `emcli token set <token>` 手动写入"},
    })

    # --- 5. 本地 sqlite 缓存(7.1.4 实测不存在,故列为可选能力) --- #
    try:
        db_exists = localdbmod.exists()
        db_path = str(localdbmod.db_path())
    except Exception as exc:  # noqa: BLE001
        db_exists, db_path = False, f"(无法确定:{exc})"
    checks.append({
        "name": "local em7.db",
        "ok": db_exists,
        "detail": {"path": db_path, "exists": db_exists,
                   "note": "缺它不影响收发消息,`emcli db *` 不可用"},
    })

    # --- 6. 登录态后端:CDP 端口 + 磁盘 LevelDB(只读诊断,只报长度与布尔) --- #
    #
    #     读类命令**不依赖调试端口**:CDP 连不上会自动回退磁盘 LevelDB。
    #     安全红线:这里只输出 数据目录 / 文件记录数 / origin / 键名 /
    #     token 是否存在与长度 / 选中的 origin,**绝不输出任何值**,也不输出
    #     `sensitive_fields` 这类敏感字段名清单。
    live_port = None
    try:
        live_port = appmod.find_live_port()
    except Exception:  # noqa: BLE001
        live_port = None
    disk_brief: Dict[str, Any] = {}
    try:
        disk_brief = _disk_diagnose_brief(localstoremod.diagnose())
    except Exception as exc:  # noqa: BLE001
        disk_brief = {"disk_error": f"{exc.__class__.__name__}: {exc}"[:200]}
    checks.append({
        "name": "login-state backend",
        "ok": bool(live_port) or bool(disk_brief.get("token_found")),
        "detail": {
            "cdp_port": live_port or "未开放",
            **disk_brief,
            "note": ("读类命令不依赖调试端口:CDP 不可用时自动回退磁盘 LevelDB;"
                     "发送类命令需要调试端口"),
        },
    })

    # --- 7. 凭证文件(自动登入用:只报路径与布尔,绝不回显账号/密码) --- #
    #
    #     查找顺序:--env-file → $EMCLI_LOGIN_ENV → 配置记录 → 当前目录 →
    #     各级父目录(最多 6 层)→ %LOCALAPPDATA%\emcli。这里把"找没找到 /
    #     用的哪一份 / 配置里记的那份还在不在"一次摆出来,省得靠试错排查。
    from .login import ENV_CRED_FILE, CRED_PARENT_LEVELS

    cred_brief = _cred_file_brief()
    checks.append({
        "name": "credentials file",
        "ok": bool(cred_brief.get("credentials_found")),
        "detail": {
            "path": cred_brief.get("credentials_file") or "(未找到)",
            # 来源=配置记录时,这里会带上"忽略后首个命中",父目录查找若回归可立刻发现
            "source": (cred_brief.get("credentials_source_display")
                       or "(未找到)"),
            "source_ignoring_config": (
                cred_brief.get("credentials_source_ignoring_config")
                or "(未命中)"),
            "recorded_path": cred_brief.get("credentials_config_file")
                             or "(未设置)",
            "recorded_exists": bool(cred_brief.get("credentials_config_exists")),
            "recorded_stale": bool(cred_brief.get("credentials_config_stale")),
            # 失效时给一句人话,人类输出会把它拼在末尾(空串会被过滤掉)
            "recorded_note": (
                "配置里记录的路径已失效(文件不存在),已回退到 "
                + (cred_brief.get("credentials_source") or "未找到")
                if cred_brief.get("credentials_config_stale") else ""),
            "candidates_tried": cred_brief.get("credentials_candidates", 0),
            "note": ("自动登入凭证文件查找顺序:--env-file → "
                     f"${ENV_CRED_FILE} → 配置记录 → 当前目录 → "
                     f"各级父目录(最多 {CRED_PARENT_LEVELS} 层)→ "
                     "本地配置目录;只报路径,不回显账号/密码"),
        },
    })

    # --- 8. 启动入口(让客户端以后默认带端口启动) --- #
    launcher_brief: Dict[str, Any] = {}
    try:
        lst = appmod.launcher_status()
        public = lst.get("public_lnk") or {}
        user = lst.get("user_launcher") or {}
        launcher_brief = {
            "configured": bool(lst.get("configured")),
            "port": lst.get("port"),
            "launcher": lst.get("launcher"),
            "public_lnk": public.get("path"),
            "public_lnk_exists": public.get("exists"),
            "public_lnk_writable": public.get("writable"),
            "user_launcher": user.get("path"),
            "user_launcher_exists": user.get("exists"),
            "note": ("让客户端默认带端口启动:`emcli launcher install`;"
                     "回滚:`emcli launcher remove`"),
        }
    except Exception as exc:  # noqa: BLE001
        launcher_brief = {"error": f"{exc.__class__.__name__}: {exc}"[:200],
                          "note": "让客户端默认带端口启动:`emcli launcher install`"}
    checks.append({
        "name": "launcher",
        "ok": bool(launcher_brief.get("configured")),
        "detail": launcher_brief,
    })

    # --- 9. GUI DOM(需要已连上的 CDP 页面) --- #
    dom = {}
    if cdp_ok:
        try:
            from .gui import GuiClient, GUIError

            try:
                with GuiClient(log=lambda m: None, auto_launch=False,
                               profile=profile) as gui:
                    dom = gui.dom_report()
            except GUIError as exc:
                dom = {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            dom = {"error": str(exc)[:300]}
    dom_ok = bool(dom) and all(
        isinstance(v, dict) and v.get("available") for v in dom.values())
    checks.append({
        "name": "gui dom selectors",
        "ok": dom_ok,
        "detail": dom or {"note": "需要 CDP 已连接;未连上则跳过"},
    })

    # --- 10. 可选:REST / XMPP 实际连通 --- #
    if probe:
        try:
            im = ctx.session.fetch_im_params()
            checks.append({"name": "rest gettoken", "ok": True,
                           "detail": {k: v for k, v in im.items()
                                      if k != "msg_token"}})
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": "rest gettoken", "ok": False,
                           "error": str(exc)[:300]})
        try:
            with ctx.session.xmpp() as client:
                convs = client.get_conversations(count=1)
                checks.append({
                    "name": "xmpp",
                    "ok": True,
                    "detail": {"jid": _mask_jid(ctx, client.negotiated_jid),
                               "conversations": len(
                                   convs.get("conversations") or [])}})
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": "xmpp", "ok": False,
                           "error": str(exc)[:300]})

    payload = {
        "ok": True,
        "user_agent": profile.user_agent,
        "checks": checks,
    }
    if out.json_mode:
        out.data(payload)
        return

    out.info("E-Mobile 版本兼容能力报告(只读)")
    out.info("=" * 52)
    for check in checks:
        mark = "✓" if check.get("ok") else "✗"
        detail = check.get("detail") or check.get("error") or ""
        if isinstance(detail, dict):
            note = detail.pop("note", "")
            sub = "; ".join(f"{k}={v}" for k, v in detail.items() if v not in
                            (None, "", [], {}))
            text = f"{sub}  {note}".strip()
        else:
            text = str(detail)
        out.info(f"  {mark} {check['name']}: {text}")
    out.info("=" * 52)
    failed = [c["name"] for c in checks if not c.get("ok")]
    if failed:
        out.info("缺:" + "、".join(failed))
        out.info("处理建议:")
        if "e-mobile exe" in failed:
            out.info("  · 设 $env:EMCLI_EMOBILE_EXE 指向 E-Mobile.exe 完整路径")
        if "client version" in failed:
            out.info("  · 版本未识别 → 用候选并集尽力运行;"
                     "可设 $env:EMCLI_CLIENT_VERSION 手动指定")
        if "login-state key" in failed:
            out.info("  · 用 `emcli token set <access_token>` 手动写入登录态")
        if "login-state backend" in failed:
            out.info("  · 两条登录态后端都读不到:确认 E-Mobile 已登录;"
                     "读类命令走磁盘 LevelDB(不需要调试端口),"
                     "发送类命令才需要端口")
        if "credentials file" in failed:
            out.info("  · 未找到凭证文件:用 `emcli login init` 生成模板,或用 "
                     "--env-file <路径> 指定(自动查找顺序见上面 note)")
        if "launcher" in failed:
            out.info("  · 启动入口未配置:`emcli launcher install`"
                     "(让客户端以后默认带调试端口启动)")
        if "gui dom selectors" in failed:
            out.info("  · GUI DOM 未达预期:确认 E-Mobile 已登录并停在主界面,"
                     "再看上面各选择器命中数")
    else:
        out.success("全部能力就绪")


def _force_utf8_output() -> None:
    """Windows 控制台默认 GBK,中文 JSON 会被写坏,这里强制 UTF-8。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main() -> None:
    _force_utf8_output()
    cli(prog_name=__prog__)


if __name__ == "__main__":
    main()
