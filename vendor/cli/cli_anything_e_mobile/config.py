"""本地配置存储(仅本机,可随时删除)。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

APP_DIR = Path(os.environ.get("EMCLI_HOME") or
               (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "emcli"))
CONFIG_FILE = APP_DIR / "config.json"

#: 抓 token 用的 CDP 端口(需以 --remote-debugging-port 启动 E-Mobile)
CDP_PORT = int(os.environ.get("EMCLI_CDP_PORT", "9333"))
#: XMPP 消息服务器默认值(实际由 /emp/api/msg/gettoken 返回覆盖)
DEFAULT_XMPP_PORT = 5222   # TCP XMPP(响应里的 app_port)
DEFAULT_WS_PORT = 7070     # XMPP over WebSocket(响应里的 pc_port)
DEFAULT_MSG_DOMAIN = "weaver"
#: 传输方式:tcp = 裸 TCP XMPP(5222);ws = XMPP over WebSocket(7070)
#: 默认 tcp:实测**只有 TCP 通道能把消息真正发到群**(WS 上 IQ/读取正常,
#: 但发送被服务端静默丢弃)。WebSocket 通道留给只读场景或将来排查。
DEFAULT_TRANSPORT = os.environ.get("EMCLI_TRANSPORT", "tcp")
#: token 有效期(服务端 expire 通常 604800 秒,这里留余量提前刷新)
TOKEN_TTL_SECONDS = 6 * 24 * 3600


class ConfigError(RuntimeError):
    """配置缺失或不完整。"""


@dataclass
class Config:
    # 来自 E-Mobile 登录态
    base_url: str = ""                 # http://<OA 主机>:8999
    base_user_id: str = ""             # 你的 E-Mobile 用户 id
    base_user_name: str = ""           # 张三(占位示例)
    access_token: str = ""             # REST 令牌
    access_token_from: str = ""        # 来源: cdp / localstore / manual / cache
    access_token_at: float = 0.0       # 取得时间(epoch)
    access_token_expire: int = TOKEN_TTL_SECONDS

    # 来自 /emp/api/msg/gettoken
    msg_token: str = ""
    msg_udid: str = ""                 # 形如 a1b2c3d4 的设备标识
    msg_domain: str = DEFAULT_MSG_DOMAIN
    xmpp_host: str = ""                # ga-oamessage.<OA 主机>
    xmpp_port: int = DEFAULT_XMPP_PORT  # 5222,TCP XMPP 用
    ws_port: int = DEFAULT_WS_PORT      # 7070,XMPP over WebSocket 用(必须带 xmpp 子协议)
    im_fetched_at: float = 0.0

    # 行为
    resource: str = "emcli"            # XMPP 资源名,避免与 GUI 的 /pc 冲突
    transport: str = DEFAULT_TRANSPORT  # ws / tcp
    proxy: str = ""                    # "host:port",空则自动探测
    # 发送白名单:只有列在这里的会话 id 才允许 `chat send`。
    # 默认空 = 一律禁止发送(零信任)。用 `emcli policy add <会话>` 显式授权。
    # 这条护栏是为了避免"误发到真实群/真实同事"——消息发出去收不回来。
    send_allowlist: List[str] = field(default_factory=list)

    # 自动登入:上次成功解析到的凭证文件**绝对路径**。
    # 只记路径 —— 账号 / 密码**绝不**落进配置文件;记下来的作用是:
    # 下次在任何目录下跑都能先用这份文件,不必依赖 cwd 或环境变量。
    # 若该路径已不存在,自动登入会静默跳过它继续按顺序往后找(见 login.py)。
    login_env_file: str = ""

    # 其他
    notes: Dict[str, Any] = field(default_factory=dict)

    # ---- 持久化 ---- #
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        path = path or CONFIG_FILE
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Optional[Path] = None) -> Path:
        path = path or CONFIG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return path

    # ---- 有效性判断 ---- #
    def has_access_token(self) -> bool:
        if not self.access_token:
            return False
        if not self.access_token_at:
            return False
        return (time.time() - self.access_token_at) < self.access_token_expire * 0.9

    def has_im_params(self) -> bool:
        return bool(self.msg_token and self.xmpp_host)

    def token_age_hours(self) -> float:
        if not self.access_token_at:
            return -1.0
        return (time.time() - self.access_token_at) / 3600.0

    def require_access_token(self) -> None:
        if not self.access_token:
            raise ConfigError(
                "尚未取得 access_token。请先运行:\n"
                "  1) 确保 E-Mobile 已登录并保持运行;\n"
                "  2) `emcli token refresh` —— 会先尝试客户端调试端口(CDP),\n"
                "     连不上或读不到 token 时自动改读本机 E-Mobile 本地登录态\n"
                "     (磁盘 LevelDB),**无需**为了读 token 去开调试端口;\n"
                "  或 `emcli token set <access_token>` 手动填写。"
            )

    def require_im_params(self) -> None:
        if not self.has_im_params():
            raise ConfigError(
                "尚未取得 IM 参数(msg_token / 消息服务器)。请先 `emcli token refresh`。"
            )

    # ---- 展示 ---- #
    def summary(self, with_secrets: bool = False) -> Dict[str, Any]:
        data = {
            "base_url": self.base_url,
            # 姓名与工号都是个人敏感值:登录态展示面**默认**不出现完整值。
            # 组合逻辑只在这里实现一次,展示层一律读这些字段(见 mask_user_label)。
            # 例外:显式原始数据命令(`db users` / `db items --value` /
            # `contact search`)与 `--show-identity` 排障开关,见 SKILL.md。
            "user": mask_user_label(self.base_user_name, self.base_user_id),
            "user_masked": mask_person_name(self.base_user_name),
            "user_len": len(str(self.base_user_name or "").strip()),
            "user_id_masked": mask_user_id(self.base_user_id),
            "user_id_len": len(str(self.base_user_id or "").strip()),
            "access_token": mask_secret(self.access_token),
            "token_source": self.access_token_from or "-",
            "token_age_hours": round(self.token_age_hours(), 2)
            if self.access_token_at else None,
            "msg_token": mask_secret(self.msg_token),
            "msg_domain": self.msg_domain,
            "xmpp_host": self.xmpp_host or "-",
            "tcp_port": self.xmpp_port,
            "ws_port": self.ws_port,
            "transport": self.transport,
            "udid": self.msg_udid or "-",
            "resource": self.resource,
            "proxy": self.proxy or "(自动探测)",
            "send_allowlist": list(self.send_allowlist),
            "login_env_file": self.login_env_file or "-",
            "config_file": str(CONFIG_FILE),
        }
        if with_secrets:
            data["access_token_plain"] = self.access_token
            data["msg_token_plain"] = self.msg_token
        return data


def mask_secret(value: str) -> str:
    """凭证/令牌/手机号等敏感值的**唯一**掩码函数:可见字符 ≤ 4 个,且必带长度。

    格式:`ab…yz(32)`(值 + 括号里的字符数)。设计取舍:
      * 旧的"前 6 + … + 后 4"对 32 位 token 会暴露 11/32 ≈ 34% 的字符,
        足够被拿去做部分匹配,故收紧到 4 个字符;
      * 长度必须保留 —— 排查"token 是不是被截断了"时它是唯一有用的信号;
      * 空值仍返回 `-`;长度 ≤ 2 时一个字符都不显示(只给长度)。

    **全仓唯一实现**:任何要展示 token / msg_token / 密钥 / 手机号的地方都必须
    调这里,不要就地手写 `value[:6] + "…" + value[-4:]` 这类切片 —— 历史事故
    (2026-10-01)就是漏改两处手写切片,导致 `--json` 里仍能看见 10 个字符。

    注意:`summary(with_secrets=True)` 的明文输出**不经过**这里,那是调用方
    显式要明文时才给的(见 `Config.summary`)。
    """
    if not value:
        return "-"
    text = str(value)
    length = len(text)
    if length <= 2:
        return f"…({length})"
    if length <= 4:
        return f"{text[0]}…{text[-1]}({length})"
    return f"{text[:2]}…{text[-2:]}({length})"


#: 向后兼容别名:既有引用(`from .config import _mask`)继续可用。
_mask = mask_secret


def _short_fingerprint(value: str) -> str:
    """稳定的 4 位十六进制指纹:用于**区分不同人**,不可逆推原值。"""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:4]


def mask_person_name(value: str) -> str:
    """**姓名**的脱敏形态:首字 + 占位 + 字数 + 稳定指纹,例如 `张**(3字·a1b2)`。

    设计取舍:
      * 只露**首字**,绝不露尾字 —— 两字姓名若"首+尾"就等于把名字全给了;
      * 必须带**字数**,排查"是不是登错账号"时它是最直接的信号;
      * 必须带**稳定指纹**:同姓同字数的人(张三 / 张四)靠它区分,
        既满足"能确认登的是自己",又不足以还原姓名;
      * 单字姓名连首字也不露(否则等于明文)。

    **全仓唯一实现**:任何展示"当前登录用户姓名"的地方都必须走这里
    (组合形态见 `mask_user_label`),不要在展示层就地切片。
    """
    text = str(value or "").strip()
    if not text:
        return "-"
    length = len(text)
    fingerprint = _short_fingerprint(text)
    if length == 1:
        return f"**(1字·{fingerprint})"
    return f"{text[0]}**({length}字·{fingerprint})"


def mask_user_id(value: str) -> str:
    """**工号 / 用户 id** 的脱敏形态:前缀 + 占位 + 长度 + 稳定指纹。

    形态与规则:
      * 长度 ≥ 5:`12**(5位·ab12)` —— 只露前 2 位;
      * 长度 ≤ 4:`**(4位·ab12)` —— **一位都不露**。3~4 位的短工号若也露
        前 2 位,暴露比例高达 50%~67%,等于没脱敏,故短号一律完全掩码;
      * 空值:`-`。

    **稳定指纹是必需的**:没有它,`12345` 与 `12346` 都会得到 `12**(5位)`,
    既无法确认"登的是不是自己",也无法区分两个账号。指纹取 sha256 前 4 位
    (与 `mask_person_name` 同机制),不可逆推原值。

    工号是可直接定位到个人的标识,按项目"个人敏感值不得回显"的原则一并脱敏;
    保留前 2 位只是为了让排查时能对上"大概是哪个号段",不足以还原完整工号。

    刻意**不用** `…` 作占位:全仓 `…` 专属于 `mask_secret` 的凭证掩码,
    这样"凡含 … 的串必带 (数字长度)"的审计断言仍然成立。
    """
    text = str(value or "").strip()
    if not text:
        return "-"
    length = len(text)
    fingerprint = _short_fingerprint(text)
    if length < 5:
        return f"**({length}位·{fingerprint})"
    return f"{text[:2]}**({length}位·{fingerprint})"


def mask_jid(jid: str, *, reveal: bool = False) -> str:
    """JID 脱敏:只掩码 **node**(`@` 之前),domain / resource 原样保留。

    E-Mobile 的 node 形如 `{工号}|{设备标识}`,所以按 `|` 拆段、每段走
    `mask_user_id()`;`@domain/resource` 是服务器与资源名,不含个人信息,
    排障时反而必须看清,故保留。例如::

        12345|ab12cd34@msg.example.com/pc
        → 12**(5位·1d84)|ab**(8位·9c3e)@msg.example.com/pc

    没有 `@` 时把整串当 node 处理(仍逐段掩码);空值给 `-`。
    `reveal=True` 原样返回 —— **只**给 CLI 的 `--show-identity`(排障且确认
    输出不会落盘/转发/外泄)使用,不要在其他地方默认打开。

    **全仓唯一实现**:`status --probe` / `compat --probe` / `chat send
    --dry-run` / `--verbose` 连接日志都必须走这里,不要就地拼字符串。
    """
    text = str(jid or "").strip()
    if not text:
        return "-"
    if reveal:
        return text
    node, sep, rest = text.partition("@")
    masked = "|".join(mask_user_id(part) for part in node.split("|"))
    return f"{masked}@{rest}" if sep else masked


def mask_user_label(name: str, user_id: str = "") -> str:
    """把"姓名 + 工号"组合成**脱敏**的用户标签,例如 `张**(3字·a1b2)(12**(5位·c3d4))`。

    `Config.summary()` 的 `user` 字段与所有展示层共用这一处组合逻辑。
    空值语义与历史一致:只有姓名给姓名,只有工号给工号,都没有给 `-`。
    """
    masked_name = mask_person_name(name)
    masked_id = mask_user_id(user_id)
    if masked_name == "-" and masked_id == "-":
        return "-"
    if masked_id == "-":
        return masked_name
    if masked_name == "-":
        return masked_id
    return f"{masked_name}({masked_id})"


def mask_user_label_text(text: str) -> str:
    """把**已经拼好**的 `姓名(工号)` 文本整体脱敏。

    `session.refresh_token_from_app()` 返回的 `user` 就是这种组合串
    (例如 `张三(900001)`),它不经过 `Config.summary()`;展示层拿到这种
    字符串时统一走这里,不要直接回显。无法识别组合形态时按姓名处理
    (仍然不露完整值)。
    """
    value = str(text or "").strip()
    if not value:
        return "-"
    name, sep, ident = value.rpartition("(")
    if sep and ident.endswith(")") and name:
        return mask_user_label(name, ident[:-1])
    return mask_person_name(value)

