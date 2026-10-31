"""E-Mobile 消息通道:XMPP over TCP(可选 HTTP CONNECT 代理隧道)+ SCRAM-SHA-1。

协议事实(逆向自 E-Mobile 7.2.6 前端 bundle + 本机实测):
  * 服务器: ga-oamessage.<公司域名>:5222 (TCP XMPP;WS 7070 端点已禁用 → 503)
  * 消息域 domain: "weaver"        ← /emp/api/msg/gettoken 的 msg_domain
  * 用户 node: "{base_user_id}|{msg_udid}"
  * 认证: SASL SCRAM-SHA-1, username=node, password=msg_token
  * 单聊 to: "{target_id}|{udid}@{domain}"   群聊 to: "{target_id}@group.{domain}"
  * 发消息: <message type="chat"><body>{JSON}</body></message>
      body JSON = {"objectName":"RC:TxtMsg","content":"文本","extra":{...}}
  * 会话列表: <iq xmlns="http://weaver.com.cn/getConversation" type="get"><query>{JSON}</query></iq>
  * 历史消息: <iq xmlns="http://weaver.com.cn/history" type="get"><query>{JSON}</query></iq>
  * 读后回执: <message type="headline" to="{from}@confirm.{domain}" from="{my_jid}"/>

Windows 上 E-Mobile 自身流量经本地代理(Clash 混合端口),故默认自动探测系统代理。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import queue
import re
import socket
import time
import uuid
from typing import Any, Callable, Dict, List, Optional
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import Element

from ..config import mask_jid

XMLNS_STREAM = "http://etherx.jabber.org/streams"
XMLNS_CLIENT = "jabber:client"
XMLNS_SASL = "urn:ietf:params:xml:ns:xmpp-sasl"
XMLNS_BIND = "urn:ietf:params:xml:ns:xmpp-bind"
XMLNS_FRAMING = "urn:ietf:params:xml:ns:xmpp-framing"

NS_GET_CONVERSATION = "http://weaver.com.cn/getConversation"
NS_CONVERSATION_PAGE = "http://weaver.com.cn/getConversationPage"
NS_HISTORY = "http://weaver.com.cn/history"
NS_SEARCH_HISTORY = "http://weaver.com.cn/history/search"
NS_GLOBAL_SEARCH = "http://weaver.com.cn/chat/search"
NS_GROUP = "http://weaver.com.cn/group"
NS_GROUP_INVITE = "http://weaver.com.cn/group/inviter"
NS_DING = "http://weaver.com.cn/ding"
NS_HISTORYITEM = "http://weaver.com.cn/historyitem"

OBJECT_NAME_TEXT = "RC:TxtMsg"
OBJECT_NAME_IMG = "RC:ImgMsg"
OBJECT_NAME_FILE = "FW:attachmentMsg"
OBJECT_NAME_VOICE = "RC:VcMsg"
OBJECT_NAME_RICH = "FW:richTextMsg"

_DECL_RE = re.compile(r"^<\?xml[^>]*\?>\s*")
_STREAM_OPEN_RE = re.compile(r"<(?:[\w.:-]+:)?stream:stream\b[^>]*>", re.IGNORECASE)
_STREAM_CLOSE_RE = re.compile(r"</(?:[\w.:-]+:)?stream:stream\s*>", re.IGNORECASE)


class XMPPError(RuntimeError):
    """XMPP 连接/协议错误。"""


# --------------------------------------------------------------------------- #
# 代理探测
# --------------------------------------------------------------------------- #
def detect_proxy(explicit: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """按优先级解析代理:显式参数 > EMCLI_PROXY > 环境变量 > Windows 系统代理。"""
    candidates: List[str] = []
    for candidate in (explicit, os.environ.get("EMCLI_PROXY"),
                      os.environ.get("HTTPS_PROXY"), os.environ.get("https_proxy"),
                      os.environ.get("HTTP_PROXY"), os.environ.get("http_proxy")):
        if candidate:
            candidates.append(candidate)
    for candidate in candidates:
        parsed = _parse_proxy_url(candidate)
        if parsed:
            return parsed
    return _windows_system_proxy()


#: 本机常见混合代理端口(Clash / Clash Verge / v2ray / SSR 等)
COMMON_LOCAL_PROXY_PORTS: tuple = (7897, 7890, 10809, 10808, 1080)


def resolve_proxy(explicit: Optional[str] = None,
                  probe_host: Optional[str] = None,
                  probe_port: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """解析代理;若配置类来源都没给出,则探测本机常见代理端口能否 CONNECT 到目标。"""
    found = detect_proxy(explicit)
    if found:
        return found
    if probe_host and probe_port:
        for port in COMMON_LOCAL_PROXY_PORTS:
            candidate = {"host": "127.0.0.1", "port": port}
            if proxy_can_reach(candidate, probe_host, probe_port):
                return candidate
    return None


def proxy_can_reach(proxy: Dict[str, Any], host: str, port: int,
                    timeout: float = 3.0) -> bool:
    """探测该代理是否能 CONNECT 到指定目标。"""
    try:
        sock = _open_tunnel_via_proxy(proxy, host, port, timeout)
    except (OSError, XMPPError):
        return False
    try:
        sock.close()
    except OSError:
        pass
    return True


def _parse_proxy_url(value: str) -> Optional[Dict[str, Any]]:
    value = value.strip()
    if not value:
        return None
    if "://" not in value:
        if value.count(":") == 1:
            host, _, port = value.partition(":")
            return {"host": host, "port": int(port)} if port.isdigit() else None
        return None
    scheme, _, rest = value.partition("://")
    rest = rest.rstrip("/")
    if "@" in rest:
        rest = rest.split("@", 1)[1]
    if rest.startswith("["):
        host, _, tail = rest.partition("]")
        port = tail.lstrip(":")
    else:
        host, _, port = rest.partition(":")
    if not port:
        port = "7897" if scheme.lower().startswith("socks") else "8080"
    if not port.isdigit():
        return None
    return {"host": host, "port": int(port)}


def _windows_system_proxy() -> Optional[Dict[str, Any]]:
    """读取 Windows 系统代理(HKCU Internet Settings)。"""
    try:
        import winreg  # type: ignore
    except ImportError:
        return None
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\Internet Settings")
        try:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return None
        except FileNotFoundError:
            return None
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return None
    if not server:
        return None
    if "=" in server:
        for part in server.split(";"):
            if part.startswith("http="):
                return _parse_proxy_url(part[4:])
            if part.startswith("socks="):
                return _parse_proxy_url(part[6:])
    return _parse_proxy_url(server)


def _open_tunnel_via_proxy(proxy: Dict[str, Any], host: str, port: int,
                           timeout: float) -> socket.socket:
    """HTTP CONNECT 隧道(Clash/mixed 代理均支持)。"""
    sock = socket.create_connection((proxy["host"], proxy["port"]), timeout=timeout)
    sock.settimeout(timeout)
    sock.sendall(
        f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        f"Proxy-Connection: keep-alive\r\n\r\n".encode("ascii")
    )
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise XMPPError(f"代理 {proxy['host']}:{proxy['port']} 关闭了连接")
        buf += chunk
        if len(buf) > 8192:
            break
    head = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1", "replace")
    status_line = head.split("\r\n", 1)[0]
    if " 200" not in status_line:
        raise XMPPError(f"代理拒绝 CONNECT {host}:{port} -> {status_line}")
    return sock


class _WsTransport:
    """XMPP over WebSocket 的最小实现。

    关键点:**必须带 `Sec-WebSocket-Protocol: xmpp`**,否则服务端返回
    `503 Endpoint Creation Failed`(实测,Strophe 的 `new WebSocket(url, "xmpp")`
    就是在传这个子协议)。
    """

    def __init__(self, host: str, port: int, proxy: Optional[Dict[str, Any]],
                 timeout: float, path: str = "/ws/") -> None:
        self.host, self.port, self.path = host, port, path
        self.timeout = timeout
        if proxy:
            self._sock = _open_tunnel_via_proxy(proxy, host, port, timeout)
        else:
            self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: xmpp\r\n"
            "\r\n"
        )
        self._sock.sendall(request.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise XMPPError("WS 握手:服务器关闭了连接")
            buf += chunk
            if len(buf) > 8192:
                break
        head = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1", "replace")
        status_line = head.split("\r\n", 1)[0]
        if " 101" not in status_line:
            raise XMPPError(f"WS 握手被拒: {status_line}"
                            "  (提示:必须带 Sec-WebSocket-Protocol: xmpp)")
        if "xmpp" not in head.lower():
            self.close()
            raise XMPPError("WS 握手:服务端未确认 xmpp 子协议")

    # ---- 类 socket 接口 ---- #
    def sendall(self, data: bytes) -> None:
        self._sock.sendall(_ws_frame(data))

    def recv(self, bufsize: int = 65536) -> bytes:
        return self._read_frame()

    def settimeout(self, timeout: Optional[float]) -> None:
        self._sock.settimeout(timeout)

    def close(self) -> None:
        try:
            self._send_close()
        except OSError:
            pass
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()

    def _send_close(self) -> None:
        try:
            self._sock.sendall(_ws_frame(b"", opcode=0x8))
        except OSError:
            pass

    def _read_exact(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = self._sock.recv(n - len(out))
            if not chunk:
                raise XMPPError("WS 连接被服务器关闭")
            out += chunk
        return out

    def _read_frame(self) -> bytes:
        header = self._read_exact(2)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        if length == 126:
            length = int.from_bytes(self._read_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(self._read_exact(8), "big")
        if masked:
            self._read_exact(4)                     # 服务端不应掩码
        payload = self._read_exact(length)
        if opcode == 0x8:                           # close
            raise XMPPError("WS 收到关闭帧")
        if opcode in (0x9, 0xA):                    # ping/pong
            if opcode == 0x9:
                self._sock.sendall(_ws_frame(payload, opcode=0xA))
            return b""
        return payload


def _ws_frame(data: bytes, opcode: int = 0x1) -> bytes:
    """客户端帧必须掩码(RFC 6455)。"""
    import struct
    header = bytearray()
    header.append(0x80 | opcode)                     # FIN + opcode
    length = len(data)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    mask = os.urandom(4)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return bytes(header) + masked


def _open_websocket(host: str, port: int, proxy: Optional[Dict[str, Any]],
                    timeout: float):
    """XMPP over WebSocket(带 xmpp 子协议)。"""
    return _WsTransport(host, port, proxy, timeout)


# --------------------------------------------------------------------------- #
# SCRAM-SHA-1
# --------------------------------------------------------------------------- #
class ScramSha1:
    """RFC 5802 SCRAM-SHA-1 客户端侧实现(仅标准库)。"""

    def __init__(self, username: str, password: str,
                 client_nonce: Optional[str] = None) -> None:
        self.username = _scram_escape(username)
        self.password = password
        self.client_nonce = client_nonce or base64.b64encode(
            os.urandom(18)).decode("ascii")
        self.client_first_bare = f"n={self.username},r={self.client_nonce}"

    @staticmethod
    def client_first_message() -> str:
        raise NotImplementedError  # 占位,避免误用

    def first_message(self) -> str:
        return "n,," + self.client_first_bare

    def client_final(self, server_first: str) -> str:
        attrs = _parse_scram_attrs(server_first)
        nonce = attrs.get("r", "")
        if not nonce.startswith(self.client_nonce):
            raise XMPPError("服务端 nonce 不含客户端 nonce")
        salt_b64, iterations = attrs.get("s"), attrs.get("i")
        if not salt_b64 or not iterations:
            raise XMPPError("SCRAM 挑战缺少 salt/迭代次数")
        try:
            salt = base64.b64decode(salt_b64)
            iters = int(iterations)
        except (ValueError, TypeError) as exc:
            raise XMPPError(f"SCRAM 挑战解析失败: {exc}") from exc

        final_without_proof = f"c=biws,r={nonce}"
        salted = hashlib.pbkdf2_hmac("sha1", self.password.encode("utf-8"),
                                     salt, iters, dklen=20)
        client_key = hmac.new(salted, b"Client Key", hashlib.sha1).digest()
        stored_key = hashlib.sha1(client_key).digest()
        auth_message = ",".join([self.client_first_bare, server_first,
                                 final_without_proof]).encode("utf-8")
        client_sig = hmac.new(stored_key, auth_message, hashlib.sha1).digest()
        proof = bytes(a ^ b for a, b in zip(client_key, client_sig))
        return final_without_proof + ",p=" + base64.b64encode(proof).decode("ascii")


def _scram_escape(value: str) -> str:
    return value.replace("=", "=3D").replace(",", "=2C")


def _parse_scram_attrs(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in text.split(","):
        if "=" in item:
            k, _, v = item.partition("=")
            out[k.strip()] = v.strip()
    return out


# --------------------------------------------------------------------------- #
# XMPP 客户端
# --------------------------------------------------------------------------- #
def connected_log_text(jid: str) -> str:
    """`--verbose` 的「已连接 <jid>」日志行:**始终**掩码 node。

    连接日志经常被落盘、转发或贴进工单,而 JID 的 node 就是
    `{工号}|{设备标识}`。这一层拿不到 CLI 的选项,所以在**源头**就掩码:
    任何调用方(CLI / 脚本 / 测试)都不会把工号写进日志。domain / resource
    保留,排障仍能看出连的是哪台服务器、哪个资源。

    需要完整标识时由展示层显式还原 —— CLI 的 `--show-identity` 会调用
    `cli._restore_connected_identity()`。
    """
    return f"已连接 {mask_jid(jid)}"


class XMPPClient:
    """最小可用 XMPP 客户端:连接 / SASL / bind / IQ / 消息收发。"""

    def __init__(self, host: str, port: int, domain: str, node: str, token: str,
                 resource: str = "emcli", proxy: Optional[Dict[str, Any]] = None,
                 timeout: float = 20.0,
                 on_message: Optional[Callable[[Dict[str, Any]], None]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 transport: Optional[str] = "tcp") -> None:
        """
        transport:
          "tcp"      XMPP over TCP(SASL 之后是裸流),端口 5222
          "ws"       XMPP over WebSocket,端口 7070,路径 /ws/
                     **必须带 Sec-WebSocket-Protocol: xmpp**,否则服务端 503
                     (实测:不带头 → 503 Endpoint Creation Failed)
        """
        self.host = host
        self.port = port
        self.domain = domain
        self.node = node
        self.token = token
        self.resource = resource
        self.proxy = proxy
        self.timeout = timeout
        self.on_message = on_message
        self._log = log or (lambda msg: None)
        self.transport = transport

        self._sock: Optional[socket.socket] = None
        self._puller = ET.XMLPullParser(["end"])
        self._elements: "queue.Queue[Element]" = queue.Queue()
        self._writer_lock = __import__("threading").Lock()
        self.connected = False
        self.negotiated_jid: Optional[str] = None

    # ---- 地址 ---- #
    @property
    def bare_jid(self) -> str:
        return f"{self.node}@{self.domain}"

    @property
    def full_jid(self) -> str:
        return f"{self.bare_jid}/{self.resource}"

    @staticmethod
    def chat_jid(chat_id: str, udid: str, domain: str, is_group: bool) -> str:
        """拼接对端 JID。规则直接来自前端 buildToId():

            is_group : "{chat_id}@group.{domain}"
            单聊     : "{chat_id}|{udid}@{domain}"

        注意单聊的 chat_id 是**复合 id**("对方id,我的id"),不是单纯的对端 id
        —— 前端 this.messages 就是以它作 key,发消息时原样传给 sendMsg({to})。
        """
        if is_group:
            return f"{chat_id}@group.{domain}"
        return f"{chat_id}|{udid}@{domain}"

    # ---- 连接 ---- #
    def connect(self) -> str:
        if self.connected:
            return self.negotiated_jid or self.full_jid
        if self.transport == "ws":
            self._sock = _open_websocket(self.host, self.port, self.proxy,
                                         self.timeout)
            self._log(f"连接消息服务器 ws://{self.host}:{self.port}/ws/ (subprotocol=xmpp)"
                      + (f" 经代理 {self.proxy['host']}:{self.proxy['port']}" if self.proxy else ""))
        else:
            via = ""
            if self.proxy:
                via = f" (经代理 {self.proxy['host']}:{self.proxy['port']})"
            self._log(f"连接消息服务器 {self.host}:{self.port} (TCP){via}")
            try:
                if self.proxy:
                    self._sock = _open_tunnel_via_proxy(self.proxy, self.host,
                                                        self.port, self.timeout)
                else:
                    self._sock = socket.create_connection((self.host, self.port),
                                                          timeout=self.timeout)
            except OSError as exc:
                raise XMPPError(f"无法连接 {self.host}:{self.port}{via}: {exc}") from exc

        self._send_raw(self._stream_open_xml())

        scram = ScramSha1(self.node, self.token)
        self._negotiate_sasl(scram)
        self._log("SASL SCRAM-SHA-1 认证通过")

        # 重启流
        self._puller = ET.XMLPullParser(["end"])
        self._elements = queue.Queue()
        self._send_raw(self._stream_open_xml())
        self._bind_resource()

        self.connected = True
        self.negotiated_jid = self.full_jid
        # 源头即掩码(工号不外泄);`--show-identity` 由 CLI 展示层还原,
        # 见 `cli._restore_connected_identity`。
        self._log(connected_log_text(self.negotiated_jid))
        return self.negotiated_jid

    def _stream_open_xml(self) -> str:
        """开流报文。

        TCP(RFC 6120): <stream:stream xmlns='jabber:client' ...>
        WS (RFC 7395): <open xmlns='urn:ietf:params:xml:ns:xmpp-framing' .../>
        —— WS 上发 <stream:stream> 服务端不会回任何东西(实测卡住)。
        """
        if self.transport == "ws":
            return (f"<open xmlns='{XMLNS_FRAMING}' to='{self.domain}' "
                    f"version='1.0'/>")
        return (f"<stream:stream xmlns='jabber:client' "
                f"xmlns:stream='{XMLNS_STREAM}' to='{self.domain}' version='1.0'>")

    def _negotiate_sasl(self, scram: ScramSha1) -> None:
        mechanisms: List[str] = []
        while True:
            elem = self._recv_element(self.timeout)
            tag = _local(elem.tag)
            if tag == "error":
                raise XMPPError("流错误: " + ET.tostring(elem, encoding="unicode")[:300])
            if tag != "features":
                continue
            mechanisms = [m.text or "" for m in elem.findall(f".//{{{XMLNS_SASL}}}mechanism")]
            if not mechanisms:
                mechanisms = [m.text or "" for m in elem.iter()
                              if _local(m.tag) == "mechanism"]
            if "SCRAM-SHA-1" not in mechanisms:
                raise XMPPError(f"服务端不支持 SCRAM-SHA-1, 提供: {mechanisms}")
            break

        self._send_raw(f"<auth xmlns='{XMLNS_SASL}' mechanism='SCRAM-SHA-1'>"
                       f"{base64.b64encode(scram.first_message().encode()).decode()}</auth>")
        while True:
            elem = self._recv_element(self.timeout)
            tag = _local(elem.tag)
            if tag == "challenge":
                server_first = base64.b64decode("".join(elem.itertext())).decode("utf-8", "replace")
                final = scram.client_final(server_first)
                self._send_raw(f"<response xmlns='{XMLNS_SASL}'>"
                              f"{base64.b64encode(final.encode()).decode()}</response>")
            elif tag == "success":
                return
            elif tag == "failure":
                raise XMPPError("SASL 认证失败: " + (elem.text or "?").strip())
            elif tag == "error":
                raise XMPPError("SASL 流错误: " + ET.tostring(elem, encoding="unicode")[:300])

    def _bind_resource(self) -> None:
        bind_id = _new_id()
        self._send_raw(
            f"<iq type='set' id='{bind_id}'>"
            f"<bind xmlns='{XMLNS_BIND}'><resource>{_xml_escape(self.resource)}</resource></bind></iq>"
        )
        while True:
            elem = self._recv_element(self.timeout)
            if _local(elem.tag) != "iq" or elem.get("id") != bind_id:
                continue
            if elem.get("type") == "error":
                raise XMPPError("bind 失败: " + _error_text(elem))
            jid_el = elem.find(f".//{{{XMLNS_BIND}}}jid")
            if jid_el is not None and jid_el.text:
                self.negotiated_jid = jid_el.text.strip()
            return

    def send_initial_presence(self, device: str = "emcli") -> None:
        self._send_raw(
            f"<presence><device>{_xml_escape(device)}</device><priority>1</priority></presence>")

    def disconnect(self) -> None:
        sock, self._sock = self._sock, None
        self.connected = False
        if sock is None:
            return
        try:
            if self.transport == "ws":
                sock.close()
            else:
                self._send_raw("</stream:stream>")
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
        except (OSError, XMPPError):
            pass

    # ---- 底层读写 ---- #
    def _send_raw(self, xml: str) -> None:
        if self._sock is None:
            raise XMPPError("socket 未连接")
        with self._writer_lock:
            self._sock.sendall(xml.encode("utf-8"))

    def _pump(self, timeout: float) -> bool:
        """读一次 socket 并喂解析器;返回是否收到数据。"""
        if self._sock is None:
            return False
        try:
            self._sock.settimeout(max(0.02, timeout))
            chunk = self._sock.recv(65536)
        except socket.timeout:
            return False
        except OSError:
            self.connected = False
            return False
        if not chunk:
            self.connected = False
            return False
        self._feed(chunk)
        return True

    def _feed(self, chunk: bytes) -> None:
        """喂数据给增量解析器。

        TCP:整段就是流,`<stream:stream>` 当根即可。
        WS :每一帧是一个独立 XML 文档(`<open/>`、`<stream:features>`、stanza…),
             直接喂会把第二帧当成"文档后垃圾"。Strophe 的解法就是用
             `<wrapper>…</wrapper>` 把每帧包起来再解析(_streamWrap)。
        """
        text = _DECL_RE.sub("", chunk.decode("utf-8", "replace"))
        if self.transport == "ws":
            # WS:每帧是一个完整 XML 文档 → 每帧一个解析器,并用 <wrapper> 包住
            # (Strophe 的 _streamWrap 就是干这个的)
            parser = ET.XMLPullParser(["end"])
            try:
                parser.feed(f"<wrapper>{text}</wrapper>")
            except ET.ParseError as exc:
                self._log(f"XML 解析警告: {exc}")
                return
            for _event, elem in parser.read_events():
                self._elements.put(elem)
            return
        try:
            self._puller.feed(text)
        except ET.ParseError as exc:
            self._log(f"XML 解析警告: {exc}")
        for _event, elem in self._puller.read_events():
            self._elements.put(elem)

    def _recv_element(self, timeout: Optional[float] = None) -> Element:
        deadline = time.time() + (timeout or self.timeout)
        while True:
            self._pump(min(0.4, max(0.05, deadline - time.time())))
            try:
                return self._elements.get_nowait()
            except queue.Empty:
                if time.time() >= deadline:
                    raise XMPPError("等待 XMPP 响应超时")
                continue

    # ---- 高层操作 ---- #
    def iq(self, namespace: str, params: Dict[str, Any], *,
           method: str = "get", timeout: Optional[float] = None) -> Dict[str, Any]:
        """自定义命名空间 IQ,返回 <query> 内的 JSON 对象。"""
        if not self.connected:
            raise XMPPError("未连接")
        rid = _new_id()
        payload = json.dumps(params, ensure_ascii=False)
        self._send_raw(
            f"<iq xmlns='{namespace}' type='{method}' id='{rid}' to='{self.domain}'>"
            f"<query>{_xml_escape(payload)}</query></iq>"
        )
        deadline = time.time() + (timeout or self.timeout)
        while True:
            elem = self._recv_element(max(1.0, deadline - time.time()))
            if _local(elem.tag) != "iq" or elem.get("id") != rid:
                self._handle_unsolicited(elem)
                continue
            if elem.get("type") == "error":
                raise XMPPError(f"{namespace} 错误: {_error_text(elem)}")
            # 响应结构: <iq type="result" id="..."><query>{JSON}</query></iq>
            # query 可能带命名空间,故按本地名匹配(与前端 getElementsByTagName 一致)
            query = None
            for child in elem.iter():
                if _local(child.tag) == "query":
                    query = child
                    break
            if query is None or not (query.text or "").strip():
                return {}
            try:
                return json.loads(query.text)
            except json.JSONDecodeError as exc:
                raise XMPPError(f"{namespace} 响应非 JSON: {(query.text or '')[:200]}") from exc

    def _handle_unsolicited(self, elem: Element) -> None:
        tag = _local(elem.tag)
        if tag == "message" and self.on_message:
            try:
                self.on_message(self.parse_message(elem))
            except Exception as exc:  # noqa: BLE001
                self._log(f"消息回调异常: {exc}")
        elif tag == "iq" and elem.get("type") == "get" and elem.get("id"):
            self._send_raw(f"<iq type='result' id='{elem.get('id')}'/>")

    # ---- 业务 IQ ---- #
    def get_conversations(self, count: int = 50, **extra: Any) -> Dict[str, Any]:
        params: Dict[str, Any] = {"count": count}
        params.update(extra)
        return self.iq(NS_GET_CONVERSATION, params, timeout=25)

    def get_conversations_page(self, count: int = 50, page: int = 1, **extra: Any) -> Dict[str, Any]:
        params: Dict[str, Any] = {"count": count, "pageNumber": page}
        params.update(extra)
        return self.iq(NS_CONVERSATION_PAGE, params, timeout=25)

    def get_history(self, target_id: str, target_type: Optional[str] = None,
                    count: int = 20, last_time: Optional[int] = None,
                    **extra: Any) -> Dict[str, Any]:
        """拉历史消息。参数为前端同款 camelCase:
        {targetId, targetType, fromUserId, lastTime, pageSize, isEM7}
        注意 lastTime 是"毫秒×2"的时间游标,首次传当前时间×2 取最近消息。
        """
        if target_type is None:
            target_type = infer_target_type(target_id)
        params: Dict[str, Any] = {
            "targetId": target_id,
            "targetType": str(target_type),
            "pageSize": count,
            "isEM7": True,
        }
        params["lastTime"] = last_time if last_time else 2 * int(time.time() * 1000)
        params["fromUserId"] = self.node.split("|", 1)[0]
        params.update(extra)
        return self.iq(NS_HISTORY, params, timeout=30)

    def search_history(self, target_id: str, keyword: str,
                       target_type: Optional[str] = None, count: int = 20,
                       page_number: int = 1, **extra: Any) -> Dict[str, Any]:
        if target_type is None:
            target_type = infer_target_type(target_id)
        params: Dict[str, Any] = {
            "targetId": target_id,
            "targetType": str(target_type),
            "keyword": keyword,
            "pageSize": count,
            "pageNumber": page_number,
            "fromUserId": self.node.split("|", 1)[0],
        }
        params.update(extra)
        return self.iq(NS_SEARCH_HISTORY, params, timeout=30)

    def get_msg_by_ids(self, msg_ids: List[str]) -> Dict[str, Any]:
        return self.iq(NS_HISTORYITEM, {"msgIds": msg_ids}, timeout=30)

    def group_call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.iq(NS_GROUP, dict(params or {}), method=method, timeout=25)

    def ding_list(self, send_or_receive: str = "all", count: int = 20) -> Dict[str, Any]:
        return self.iq(NS_DING, {"method": "getDingList",
                                 "sendOrReceive": send_or_receive,
                                 "lastTime": 2 * int(time.time() * 1000),
                                 "count": count}, timeout=25)

    # ---- 消息 ---- #
    def send_text(self, chat_id: str, text: str, udid: str, *,
                  is_group: bool = False,
                  extra: Optional[Dict[str, Any]] = None,
                  to_jid: Optional[str] = None) -> str:
        """发送文本消息,返回本地生成的 msgid。

        chat_id: 会话 id —— 群是 UUID,单聊是 "对方id,我的id" 复合形。
        to_jid : 只要想完全自定义目标 JID 时传(排障用)。
        """
        if not self.connected:
            raise XMPPError("未连接")
        msg_id = str(uuid.uuid4())
        to = to_jid or self.chat_jid(chat_id, udid, self.domain, is_group)
        body = json.dumps({"objectName": OBJECT_NAME_TEXT, "content": text,
                           "extra": extra or {}}, ensure_ascii=False)
        self._send_raw(
            f"<message id='{msg_id}' to='{_xml_escape(to)}' "
            f"from='{_xml_escape(self.full_jid)}' type='chat'>"
            f"<body>{_xml_escape(body)}</body></message>"
        )
        return msg_id

    @staticmethod
    def parse_message(elem: Element) -> Dict[str, Any]:
        body_el = elem.find("body")
        raw = (body_el.text or "") if body_el is not None else ""
        if raw.strip().startswith("{"):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"content": raw}
        else:
            payload = {"content": raw}
        frm = elem.get("from") or ""
        sender = frm.split("@", 1)[0]
        if "|" in sender:
            sender = sender.split("|", 1)[0]
        content = payload.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False) if content is not None else ""
        return {
            "msgid": elem.get("id") or "",
            "from": frm,
            "sender": sender,
            "to": elem.get("to") or "",
            "objectName": payload.get("objectName") or "",
            "content": content,
            "extra": payload.get("extra") or {},
            "received_at": time.time(),
        }

    def drain_messages(self, seconds: float) -> List[Dict[str, Any]]:
        """持续收消息 N 秒并自动回执。"""
        out: List[Dict[str, Any]] = []
        deadline = time.time() + seconds
        while time.time() < deadline:
            remaining = max(0.05, deadline - time.time())
            self._pump(min(0.3, remaining))
            while True:
                try:
                    elem = self._elements.get_nowait()
                except queue.Empty:
                    break
                if _local(elem.tag) == "message":
                    msg = self.parse_message(elem)
                    self._send_receipt(msg)
                    out.append(msg)
        return out

    def _send_receipt(self, msg: Dict[str, Any]) -> None:
        frm = msg.get("from") or ""
        if not frm or not msg.get("msgid"):
            return
        peer = frm.split("@", 1)[0] + f"@confirm.{self.domain}"
        try:
            self._send_raw(f"<message type='headline' id='{msg['msgid']}' "
                          f"to='{_xml_escape(peer)}' "
                          f"from='{_xml_escape(self.full_jid)}'/>")
        except (OSError, XMPPError):
            pass

    # ---- 上下文 ---- #
    def __enter__(self) -> "XMPPClient":
        self.connect()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.disconnect()


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def infer_target_type(target_id: str) -> str:
    """按会话 id 推断 targetType。

    会话列表里 targettype: 0 = 单聊 / 1 = 群 / 111 = 应用(流程、推送)
    单聊 id 形如 "<对方id>,<我的id>"(双方 id 升序拼接);群 id 是 UUID;
    应用 id 形如 "<我的id>,a_-2686000000000000000_1"。
    """
    tid = (target_id or "").strip()
    if not tid:
        raise XMPPError("会话 id 不能为空")
    # 应用/系统会话:形如 "<我的id>,a_-2668..._1",含下划线且带 a_/s_ 标记
    if "_" in tid and ("a_" in tid or "s_" in tid or "group_" in tid
                       or tid in ("ding",)):
        return "111"
    # 单聊:两个 userid 用逗号拼接(升序)
    if "," in tid:
        return "0"
    # 群:UUID
    return "1"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _new_id() -> str:
    return "em-" + uuid.uuid4().hex[:24]


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("'", "&apos;").replace('"', "&quot;"))


def _error_text(elem: Element) -> str:
    parts: List[str] = []
    for child in elem.iter():
        if _local(child.tag) == "text" and (child.text or "").strip():
            parts.append(child.text.strip())
    if parts:
        return " / ".join(parts)
    return ET.tostring(elem, encoding="unicode")[:200]
