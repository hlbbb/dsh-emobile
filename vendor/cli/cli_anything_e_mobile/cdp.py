"""从运行中的 E-Mobile 读取登录态(经 Chrome DevTools Protocol,只读)。

E-Mobile 把当前会话存在浏览器 localStorage 的 `nc-current-info`:
  { currentBaseUrl, currentUser: { access_token, base_user_id, base_user_name, ... } }

要求 E-Mobile 以 `--remote-debugging-port=9333` 启动。
本模块只读,不注入、不操作页面。

⚠ 版本差异(2026-09-24 实测):该**键名随客户端版本变化**
    7.2.6(开发机) → `nc-current-info`
    7.1.4(正式环境) → `gn-current-info` / `en-current-info`,
                      且 `currentUser`、`access_token`、`base_user_id`
                      字样在 7.1.4 的 leveldb 里均未命中
  ⇉ 不能写死键名。统一走 `read_current_info()`:候选键逐个试,
    最后按"值里能解析出 access_token"兜底 —— 与键名无关。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

try:
    import websocket  # type: ignore
    _HAS_WS = True
except ImportError:  # pragma: no cover
    websocket = None
    _HAS_WS = False

#: 默认端口(可被 EMCLI_CDP_PORT 覆盖)
DEFAULT_PORT = 9333

#: 各客户端版本已知的登录态键名候选(全量并集,兜底用)
KNOWN_TOKEN_KEYS: tuple = (
    "nc-current-info",    # 7.2.x(开发机实测)
    "gn-current-info",    # 7.1.x(正式环境实测)
    "en-current-info",    # 7.1.x(正式环境实测)
)

#: 已知的 "current-info" 键名后缀 —— 即使出现全新前缀,兜底也能认出来。
#: **唯一定义处**:``localstore.py`` 直接复用这里的常量,不要再抄一份。
TOKEN_KEY_SUFFIX = "current-info"

#: 在 JSON 值里定位 access_token 时允许出现的字段名(一旦变化仍有兜底)。
#: **唯一定义处**:``localstore.py`` 直接复用这里的常量,不要再抄一份。
ACCESS_TOKEN_FIELDS = ("access_token", "accessToken", "token")

#: 向后兼容别名(旧代码 / 旧文档里引用的是带下划线的私有名)
_TOKEN_KEY_SUFFIX = TOKEN_KEY_SUFFIX
_ACCESS_TOKEN_FIELDS = ACCESS_TOKEN_FIELDS


class CDPError(RuntimeError):
    """CDP 读取失败。"""


def cdp_available(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    try:
        _http_json(f"http://{host}:{port}/json/version", timeout)
        return True
    except (CDPError, OSError):
        return False


def read_localstorage(key: str = "nc-current-info", port: int = 9333,
                       host: str = "127.0.0.1") -> Dict[str, Any]:
    """在 E-Mobile 主窗口页面上执行 JS,读取 localStorage 指定键。

    保留原签名以兼容既有调用与旧测试。**新代码请用 `read_current_info()`。**
    """
    store = read_localstorage_dict(port=port, host=host)
    if key not in store:
        raise CDPError(
            f"localStorage 中没有 {key}(可能未登录或页面已切换)。"
            f"实际存在的键:{', '.join(store.keys()) or '(空)'}")
    try:
        return json.loads(store[key])
    except json.JSONDecodeError as exc:
        raise CDPError(f"{key} 不是合法 JSON: {exc}") from exc


def read_localstorage_dict(port: int = 9333,
                           host: str = "127.0.0.1") -> Dict[str, str]:
    """把整个 localStorage 取回来(键名 → 字符串原值)。"""
    if not _HAS_WS:
        raise CDPError("缺少依赖 websocket-client,请 `pip install websocket-client`")
    try:
        targets = _http_json(f"http://{host}:{port}/json/list")
    except CDPError as exc:
        raise CDPError(f"无法连到 E-Mobile 调试端口 {port}({exc});"
                       "请确认 E-Mobile 已用 --remote-debugging-port 启动") from exc

    page = _pick_page(targets)
    if page is None:
        raise CDPError("未找到 E-Mobile 页面目标。页面列表: "
                       + ", ".join(t.get("url", "?") for t in targets))

    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=8)
    try:
        expression = "JSON.stringify(localStorage)"
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True,
                       "awaitPromise": True},
        }))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") != 1:
                continue
            result = msg.get("result") or {}
            if "exceptionDetails" in result:
                raise CDPError("页面内求值异常: "
                               + json.dumps(result["exceptionDetails"],
                                            ensure_ascii=False)[:300])
            value = ((result.get("result") or {}).get("value"))
            if not isinstance(value, str):
                raise CDPError("页面未返回 localStorage 字符串")
            store = json.loads(value)
            break
    except json.JSONDecodeError as exc:
        raise CDPError(f"CDP 响应解析失败: {exc}") from exc
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
    return store


def read_current_info(port: int = 9333, host: str = "127.0.0.1",
                      keys: Optional[List[str]] = None) -> Dict[str, Any]:
    """读取登录态信息(currentBaseUrl / currentUser / ...)。

    版本兼容策略(**不依赖键名**):
      1. 先按传入 keys(通常是该版本的候选键)逐个试。
      2. 未命中则遍历全部键,挑"能解析成 JSON 且里面能取到 access_token"的那个。
      3. 再未命中则枚举所有键名报错,便于现场判断服务端是否又改了键名。

    返回解析后的 dict。失败抛 CDPError,信息里**带上实际存在的键名**。
    """
    store = read_localstorage_dict(port=port, host=host)

    candidates: List[str] = [str(k) for k in (keys or []) if k]
    # 追加全量已知键 + 任何以 current-info 结尾的键(兜住未来新前缀)
    for key in KNOWN_TOKEN_KEYS:
        if key not in candidates:
            candidates.append(key)
    for key in store:
        if key not in candidates and key.endswith(TOKEN_KEY_SUFFIX):
            candidates.append(key)

    tried = list(candidates)
    hit: Optional[str] = None
    for key in candidates:
        if key in store:
            hit = key
            break

    if hit is None:
        # 兜底:不看键名,只看值里是否有可用的 access_token
        for key, raw in store.items():
            payload = _try_json(raw)
            if payload and _extract_access_token(payload):
                hit = key
                break

    if hit is None:
        raise CDPError(
            "localStorage 里找不到登录态(未登录、页面已切换,"
            "或该客户端版本换了键名)。\n"
            f"  已尝试:{', '.join(tried) or '(无候选)'}\n"
            f"  实际存在:{', '.join(store.keys()) or '(空)'}")

    raw = store.get(hit)
    if raw is None or not isinstance(raw, str):
        raise CDPError(f"localStorage 中 {hit} 不是字符串(无法解析为 JSON)")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CDPError(f"{hit} 不是合法 JSON: {exc}") from exc


def list_localstorage_keys(port: int = 9333,
                           host: str = "127.0.0.1") -> List[str]:
    """列出 E-Mobile 页面当前的 localStorage 键名(只读诊断用)。"""
    return list(read_localstorage_dict(port=port, host=host).keys())


def _try_json(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, str):
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _extract_access_token(payload: Dict[str, Any]) -> Optional[str]:
    """从解析后的 JSON 里找 access_token。

    兼容两种形态:
      * { "currentUser": { "access_token": ... } }
      * { "access_token": ... }
    """
    containers = [payload]
    nested = payload.get("currentUser")
    if isinstance(nested, dict):
        containers.append(nested)
    for container in containers:
        if not isinstance(container, dict):
            continue
        for field in ACCESS_TOKEN_FIELDS:
            value = container.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _pick_page(targets: list) -> Optional[Dict[str, Any]]:
    pages = [t for t in targets
             if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    for t in pages:
        if "emweb" in t.get("url", "") or "url-input" in t.get("url", ""):
            return t
    return pages[0] if pages else None


def _http_json(url: str, timeout: float = 1.5) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise CDPError(str(exc)) from exc
