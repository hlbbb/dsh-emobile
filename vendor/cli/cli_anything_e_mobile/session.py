"""会话管理:token 供给、IM 参数获取、XMPP 连接、会话解析。"""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from . import cdp, config as cfgmod, localstore, rest as restmod
from .im.xmpp import (XMPPClient, XMPPError, infer_target_type,
                      resolve_proxy)

_TS_RE = re.compile(r"^\d{10,13}$")

#: 在登录态 JSON 里找 access_token 的字段名。
#: **唯一定义在 `cdp.ACCESS_TOKEN_FIELDS`**(本文件不再抄一份,避免三处漂移);
#: 这里刻意**不认**泛化的 `token` 字段:CDP / 磁盘后端已经用它做过"这条记录像不像
#: 登录态"的兜底判据,而本函数是从**已通过校验**的 payload 里再取一次值,少认一个
#: 泛化名可以避免把同名但含义不同的 token 当成 access_token 用。
_TOKEN_FIELDS = tuple(name for name in cdp.ACCESS_TOKEN_FIELDS if name != "token")


def _extract_token(*containers: Any) -> str:
    """从若干层容器里挑第一个非空字符串 token。"""
    for container in containers:
        if not isinstance(container, dict):
            continue
        for field in _TOKEN_FIELDS:
            value = container.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _detect_client_version_safe() -> Any:
    """探测版本失败/不可用一律回 None(不让诊断性调用整条链路崩掉)。"""
    try:
        from . import compat as compatmod
        return compatmod.detect_client_version()
    except Exception:  # noqa: BLE001
        return None


def _take(payload: Dict[str, Any], field: str) -> Any:
    """从登录态里取字段:优先 `currentUser.<field>`,回退顶层 `<field>`。

    7.2.x 的 user 信息在 `currentUser` 对象里;7.1.4 实测是平铺在顶层的。
    """
    nested = payload.get("currentUser")
    if isinstance(nested, dict) and nested.get(field):
        return nested.get(field)
    return payload.get(field)


def _login_state_unavailable(cdp_exc: BaseException,
                             disk_exc: BaseException) -> cdp.CDPError:
    """把 CDP 与磁盘两条后端的失败合成一条**可区分**的错误。

    返回 `CDPError` 是**刻意**的:既有调用方(`_reread_token`)只在收到
    CDPError 时才退回本地缓存 token,换成别的异常类型会破坏该降级行为。
    两条原因都写进消息,并按类型挂到 `cdp_error` / `localstore_error` 上,
    调用方要分流时可以直接看类型,不必解析文案。
    """
    error = cdp.CDPError(
        "无法从 E-Mobile 读取登录态(CDP 与本地 LevelDB 两条后端都失败):\n"
        f"  [CDP] {cdp_exc}\n"
        f"  [本地 LevelDB] {type(disk_exc).__name__}: {disk_exc}")
    error.cdp_error = cdp_exc              # type: ignore[attr-defined]
    error.localstore_error = disk_exc      # type: ignore[attr-defined]
    return error


class EMSession:
    """把配置、REST、XMPP 串起来的门面。"""

    def __init__(self, config: Optional[cfgmod.Config] = None,
                 log: Optional[Any] = None) -> None:
        self.config = config or cfgmod.Config.load()
        self._log = log or (lambda *a, **k: None)
        self._rest: Optional[restmod.EMRestClient] = None
        self._conv_cache: Optional[List[Dict[str, Any]]] = None
        self._conv_cache_at: float = 0.0
        self._name_cache: Dict[str, str] = {}
        self._profile: Optional[Any] = None
        self._detected_version: Optional[Any] = None

    # ---- 版本档案 ---- #
    @property
    def profile(self) -> Any:
        """当前客户端版本档案(懒探测 + 缓存);识别不出回 unknown 档案。"""
        if self._profile is None:
            from . import compat as compatmod
            self._detected_version = compatmod.detect_client_version()
            self._profile = compatmod.select_profile(
                self._detected_version or "unknown")
        return self._profile

    @property
    def client_version(self) -> str:
        if self._detected_version is None:
            self._detected_version = _detect_client_version_safe()
        return str(self._detected_version) if self._detected_version else "unknown"

    # ---- token ---- #
    def refresh_token_from_app(self, cdp_port: Optional[int] = None,
                               use_localstore: bool = True,
                               localstore_retries: int = 10,
                               localstore_delay: float = 0.2) -> Dict[str, Any]:
        """从运行中的 E-Mobile 读取 currentUser,更新 access_token。

        两条后端,先 CDP 后磁盘:
          1) **CDP**:要求客户端以 --remote-debugging-port 启动(只读、不操作页面);
          2) **磁盘回退**:直接解析客户端的 Chromium localStorage LevelDB ——
             用户**正常登录、没开调试端口**时也能读到(见 `localstore`)。
             localStorage 是内存缓存 + 延迟批量提交,故磁盘侧自带重试。

        **回退判据是"CDP 后端拿不到可用登录态",不只是"CDP 抛异常"**:
        CDP 连得上、localStorage 里却没有可用 access_token 时(客户端刚重启、
        停在登录页,或该版本换了键名)同样回退磁盘 —— 否则会出现"端口是通的,
        却读不到 token"的死角。

        版本兼容:按当前档案的候选键读;读不到就按"值里有 access_token"兜底,
        与 E-Mobile 客户端换了 localStorage 键名无关。

        两条后端都失败时抛 `cdp.CDPError`,消息里分别带上「CDP 侧原因」
        (连不上 / 连上了但没有 token)与「磁盘侧原因」(没有登录态 / 解析失败),
        并按类型挂在 `exc.cdp_error` / `exc.localstore_error` 上便于分流。
        `use_localstore=False` 时保持旧行为:CDP 抛什么就原样抛什么。
        """
        port = cdp_port or cfgmod.CDP_PORT
        keys = list(self.profile.localstorage_token_keys)
        source = "cdp"
        source_text = "E-Mobile 当前登录会话 (CDP 只读)"

        cdp_raised = False
        cdp_exc: Optional[BaseException] = None
        data: Dict[str, Any] = {}
        try:
            data = cdp.read_current_info(port=port, keys=keys)
        except cdp.CDPError as exc:
            cdp_raised = True
            cdp_exc = exc

        if cdp_exc is None and not _extract_token(data.get("currentUser") or {},
                                                  data):
            # CDP 连得上,但 localStorage 里没有可用的 access_token —— 同样算
            # "CDP 后端拿不到登录态",必须继续尝试磁盘后端,不能在这里就放弃。
            cdp_exc = cdp.CDPError(
                "CDP 读到了 E-Mobile 的 localStorage,但里面没有可用的 "
                "access_token(客户端可能刚重启、停在登录页,"
                "或该版本换了键名)。\n"
                f"  已探测候选键:{', '.join(keys) or '(无候选)'}")

        if cdp_exc is not None:
            if not use_localstore:
                if cdp_raised:
                    raise cdp_exc          # 与改动前一致:原样抛 CDP 错误
                data = {}                  # 与改动前一致:下面报"会话里没有 token"
            else:
                try:
                    data = localstore.read_current_info(
                        keys=keys, retries=localstore_retries,
                        delay=localstore_delay)
                except localstore.LocalStoreError as disk_exc:
                    raise _login_state_unavailable(cdp_exc, disk_exc) from disk_exc
                source = "localstore"
                source_text = "E-Mobile 当前登录会话 (本地 LevelDB 只读)"

        user = data.get("currentUser") or {}
        token = _extract_token(user, data)
        if not token:
            raise XMPPError(
                "E-Mobile 会话里没有 access_token(可能未登录)。\n"
                f"  已探测候选键:{', '.join(keys)}\n"
                "  兜底扫描 localStorage 全键也未找到 token;"
                "请确认 E-Mobile 已登录,或用 `emcli token set <token>` 手动写入。")
        c = self.config
        c.base_url = _take(data, "currentBaseUrl") or c.base_url
        c.access_token = token
        c.access_token_from = source
        c.access_token_at = time.time()
        c.access_token_expire = int(_take(data, "expire")
                                    or cfgmod.TOKEN_TTL_SECONDS)
        # 字段嵌套形态版本间会变:7.2.x 在 currentUser 下,
        # 7.1.4 实测在顶层。两级都取,前者优先。
        c.base_user_id = str(_take(data, "base_user_id") or c.base_user_id)
        c.base_user_name = str(_take(data, "base_user_name") or c.base_user_name)
        c.save()
        return {
            "base_url": c.base_url,
            "user": f"{c.base_user_name}({c.base_user_id})",
            "access_token": cfgmod.mask_secret(c.access_token),
            "expire_seconds": c.access_token_expire,
            "client_version": self.client_version,
            "source": source_text,
        }

    def ensure_token(self, force: bool = False) -> str:
        """确保有**可用**的 access_token:应用客户端(CDP)才是登录态源头。

        踩过的坑(2026-09-29 实测):OA 在登录成功后还会**再次轮换 token**,
        `emcli login` 校验并写进本地配置的那个 token,几秒后就已被服务端
        换成新的 —— 表现为"刚登录成功,`login status` 却报 42001"。
        因此**不能只看本地缓存的 TTL**,必须先拿它问一次服务端
        (`gettoken`),不可用就回客户端重读。

        顺序:本地缓存 → 服务端实测 → 不可用则从 E-Mobile 重读 → 仍不行才用缓存兜底。
        """
        from .login import session_status

        c = self.config
        if force or not c.has_access_token():
            return self._reread_token(c)

        probe = session_status(c.base_url, c.access_token)
        if probe.get("ok") and not probe.get("needs_login"):
            return c.access_token

        self._log(f"本地 token 已不可用({probe.get('reason')}),"
                  f"改从运行中的 E-Mobile 重读")
        return self._reread_token(c)

    def _reread_token(self, c: "cfgmod.Config") -> str:
        """从 E-Mobile 重读登录态;CDP 不可用时退回本地缓存。"""
        try:
            self.refresh_token_from_app()
        except cdp.CDPError as exc:
            if c.has_access_token():
                self._log(f"CDP 不可用({exc}),继续用本地 token")
                return c.access_token
            raise cfgmod.ConfigError(str(exc)) from exc
        # token 换了,IM 参数(msg_token/xmpp_host)跟着失效,别让 1 小时缓存继续喂旧值
        if c.im_fetched_at:
            c.im_fetched_at = 0.0
        self._log("已从运行中的 E-Mobile 刷新 access_token")
        return c.access_token

    def ensure_login(self, auto: bool = True,
                     env_file: Optional[str] = None,
                     manual_code: str = "",
                     max_attempts: int = 5,
                     verify_timeout_s: float = 30.0,
                     log: Optional[Any] = None) -> Dict[str, Any]:
        """确保 OA 会话可用:失效就自动重新登录一次。

        自动化流程(如 xrm-04 的 OA 投递)在 42001/被顶掉时可以直接调它,
        不用人工点客户端。

        返回 `{needed, logged_in, attempts, reason, user}`;不抛异常,
        由调用方按 `logged_in` 决定是否继续。

        登录需要账号密码,缺凭证文件时返回 `logged_in=False` 并说明原因 ——
        绝不猜、也不静默跳过。
        """
        logger = log or self._log
        from .login import (LoginError, LoginPage, OcrEngine, load_credentials,
                            perform_login, session_status)

        result: Dict[str, Any] = {"needed": False, "logged_in": False,
                                  "attempts": 0, "reason": ""}

        # 1) 现状探测
        try:
            token = self.ensure_token()
        except Exception as exc:  # noqa: BLE001
            token = ""
            result["reason"] = f"读不到本机登录态:{exc}"
        if token:
            status = session_status(self.config.base_url, token)
            if status.get("ok") and not status.get("needs_login"):
                result.update({"logged_in": True,
                               "reason": status.get("reason")
                               or "会话有效",
                               "user": self.config.base_user_name})
                return result
            result["reason"] = status.get("reason") or "会话失效"
        result["needed"] = True
        if not auto:
            return result

        # 2) 读凭证并登录
        try:
            creds = load_credentials(path=env_file)
        except Exception as exc:  # noqa: BLE001
            result["reason"] = f"{result['reason']};自动登录未执行:{exc}"
            return result

        try:
            with LoginPage(log=logger,
                           auto_launch=True) as page:
                outcome = perform_login(page, creds,
                                        ocr=OcrEngine(log=logger),
                                        manual_code=manual_code,
                                        max_attempts=max_attempts,
                                        log=logger)
                result["attempts"] = outcome.attempts
                if not outcome.ok:
                    result["reason"] = outcome.reason
                    return result
                port = page.port
        except Exception as exc:  # noqa: BLE001
            result["reason"] = f"{result['reason']};自动登录失败:{exc}"
            return result

        # 3) 重读登录态,并以 gettoken **实测**会话可用性
        #
        #    登录页跳走后 localStorage 会先写入一个过渡态 token,gettoken 对它
        #    仍返回 42001;真正的 token 要再等一两秒。只看"登录表单消失"会把
        #    过渡态当成功(2026-09-29 实测踩过),故以 errcode 为准。
        deadline = time.time() + max(1.0, verify_timeout_s)
        while True:
            try:
                refreshed = self.refresh_token_from_app(cdp_port=port)
            except Exception as exc:  # noqa: BLE001
                result["reason"] = f"登录后读取登录态失败:{exc}"
            else:
                probe = session_status(self.config.base_url,
                                       self.config.access_token)
                if probe.get("ok") and not probe.get("needs_login"):
                    result["logged_in"] = True
                    result["user"] = refreshed.get("user")
                    result["reason"] = "自动登录成功(已实测可用)"
                    try:
                        self.fetch_im_params(force=True)
                    except Exception as exc:  # noqa: BLE001
                        logger(f"IM 参数获取失败(不影响登录):{exc}")
                    return result
                result["reason"] = str(probe.get("reason") or "会话仍不可用")
            if time.time() >= deadline:
                result["reason"] = (result["reason"]
                                    + f";登录后 {verify_timeout_s:.0f}s 内会话仍不可用")
                return result
            time.sleep(2.0)

    def fetch_im_params(self, force: bool = False) -> Dict[str, Any]:
        """调 /emp/api/msg/gettoken 取 IM 参数。"""
        c = self.config
        if c.has_im_params() and not force and c.im_fetched_at:
            if time.time() - c.im_fetched_at < 3600:
                return {
                    "xmpp_host": c.xmpp_host, "tcp_port": c.xmpp_port,
                    "ws_port": c.ws_port, "msg_domain": c.msg_domain,
                    "msg_udid": c.msg_udid, "transport": c.transport,
                    "source": "本地缓存",
                }
        self.ensure_token()
        data = self.rest.get_im_params()
        msg_token = data.get("msg_token") or ""
        if not msg_token:
            raise XMPPError("gettoken 未返回 msg_token")
        c.msg_token = msg_token
        c.msg_udid = str(data.get("msg_udid") or "")
        c.msg_domain = str(data.get("msg_domain") or cfgmod.DEFAULT_MSG_DOMAIN)
        c.xmpp_host = str(data.get("pc_api_ip") or "")
        # 两个端口不能混:
        #   app_port(5222) = TCP XMPP
        #   pc_port (7070) = XMPP over WebSocket,必须带 xmpp 子协议,
        #                    不带会被服务端 503 拒掉(实测)
        c.xmpp_port = int(data.get("app_port") or cfgmod.DEFAULT_XMPP_PORT)
        c.ws_port = int(data.get("pc_port") or cfgmod.DEFAULT_WS_PORT)
        c.im_fetched_at = time.time()
        c.save()
        return {
            "xmpp_host": c.xmpp_host, "tcp_port": c.xmpp_port, "ws_port": c.ws_port,
            "msg_domain": c.msg_domain, "msg_udid": c.msg_udid,
            "msg_token": cfgmod.mask_secret(c.msg_token),
            "source": "/emp/api/msg/gettoken",
            "server_version": data.get("msg_server_version"),
            "transport": c.transport,
            "note": ("WS(/ws/,子协议 xmpp)" if c.transport == "ws"
                     else "TCP 5222"),
        }

    @property
    def rest(self) -> restmod.EMRestClient:
        if self._rest is None:
            self.ensure_token()
            self._rest = restmod.EMRestClient(
                self.config.base_url,
                self.config.access_token,
                user_agent=getattr(self.profile, "user_agent", None),
            )
        return self._rest

    # ---- XMPP ---- #
    @contextmanager
    def xmpp(self, on_message: Optional[Any] = None,
             resource: Optional[str] = None,
             transport: Optional[str] = None):
        """连上消息通道,退出时自动断开。"""
        c = self.config
        self.fetch_im_params()
        if not c.has_im_params():
            raise cfgmod.ConfigError("IM 参数不完整,请先 `emcli token refresh`")
        use_transport = transport or c.transport or "ws"
        port = c.ws_port if use_transport == "ws" else c.xmpp_port
        proxy = resolve_proxy(c.proxy or None, c.xmpp_host, port)
        client = XMPPClient(
            host=c.xmpp_host, port=port, domain=c.msg_domain,
            node=f"{c.base_user_id}|{c.msg_udid}", token=c.msg_token,
            resource=resource or c.resource or "emcli",
            proxy=proxy, on_message=on_message, transport=use_transport,
            log=lambda m: self._log(m),
        )
        client.connect()
        try:
            yield client
        finally:
            client.disconnect()

    # ---- 会话工具 ---- #
    def resolve_names(self, ids) -> Dict[str, str]:
        """把 userid / groupid 批量解析成姓名(带进程内缓存,失败降级为原 id)。"""
        wanted = [str(i) for i in ids if i]
        missing = list(dict.fromkeys(i for i in wanted if i not in self._name_cache))
        if missing:
            try:
                data = self.rest.post("/api/msg/userinfo/simplelist", body={
                    "userlist": missing, "grouplist": missing,
                    "agentlist": [], "tenantlist": [],
                })
            except Exception as exc:  # noqa: BLE001
                self._log(f"名字解析失败: {exc}")
                data = {}
            for user in (data.get("userlist") or []):
                key = str(user.get("base_user_id") or user.get("id") or "")
                if key:
                    self._name_cache[key] = (user.get("name")
                                             or user.get("base_user_name") or key)
            for grp in (data.get("grouplist") or []):
                key = str(grp.get("id") or grp.get("groupid") or "")
                if key:
                    self._name_cache[key] = (grp.get("name")
                                             or grp.get("groupname") or key)
        return {i: self._name_cache.get(i, i) for i in wanted}

    def conversations(self, force: bool = False) -> List[Dict[str, Any]]:
        """会话列表(带 30 秒内存缓存)。"""
        if self._conv_cache is not None and not force and \
                time.time() - self._conv_cache_at < 30:
            return self._conv_cache
        with self.xmpp() as client:
            data = client.get_conversations(count=100)
        convs = data.get("conversations") or []
        self._conv_cache = convs
        self._conv_cache_at = time.time()
        return convs

    def resolve_conversation(self, query: str) -> Dict[str, Any]:
        """把 名字/群里的人名/id 解析成会话对象。

        支持:
          * 完整 id (UUID 或 "对方id,我的id" 或 "我的id,a_...")
          * 群名(模糊、忽略空格)
          * 单聊对象姓名 -> 用 "对方姓名" 或 "我和<姓名>"
        """
        convs = self.conversations(force=True)
        q = (query or "").strip()
        if not q:
            raise XMPPError("请提供会话 id 或会话名称")
        for conv in convs:
            if str(conv.get("targetid", "")) == q:
                return conv
        # 名称匹配(去空格、忽略大小写)
        def norm(s: Any) -> str:
            return re.sub(r"\s+", "", str(s or "")).lower()
        target = norm(q)
        exact = [c for c in convs if norm(c.get("targetname")) == target]
        if exact:
            return exact[0]
        partial = [c for c in convs if target in norm(c.get("targetname"))]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            names = "、".join(f"{c.get('targetname')}" for c in partial[:8])
            raise XMPPError(f"“{q}” 匹配到多个会话:{names}。请用更完整的名字或会话 id。")
        raise XMPPError(f"找不到会话 “{q}”。可先用 `emcli chat list` 查看会话列表。")

    @staticmethod
    def peer_id_of(conv: Dict[str, Any], my_id: str) -> str:
        """从会话对象推出对端 id(单聊)或群 id。"""
        target_id = str(conv.get("targetid") or "")
        target_type = str(conv.get("targettype") or "")
        if target_type == "111" or "_" in target_id:
            return target_id            # 应用/系统会话,直接用
        if target_type == "0" and "," in target_id:
            a, _, b = target_id.partition(",")
            return b if a == str(my_id) else a
        return target_id

    @staticmethod
    def is_group(conv: Dict[str, Any]) -> bool:
        return str(conv.get("targettype") or "") == "1"

    @staticmethod
    def ts(value: Any) -> str:
        """把服务端时间(毫秒或秒)格式化为本地时间。"""
        try:
            num = int(float(value))
        except (TypeError, ValueError):
            return "-"
        if num > 10_000_000_000:      # 毫秒
            num //= 1000
        elif num > 100_000_000_000:   # 双倍毫秒(history 游标)
            num //= 2000
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(num))


def looks_like_timestamp(value: str) -> bool:
    return bool(_TS_RE.match(value or ""))
