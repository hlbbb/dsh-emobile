"""GUI 驱动层:通过 CDP 操作官方 E-Mobile 客户端完成发送。

为什么需要它:
  * 自己拼 XMPP 发消息,服务端会静默丢弃(已实测:单聊 12+ 次、群 6 次全失败);
  * 文件上传的隐藏 input 是头像上传器,DOM.setFileInputFiles 无效;
  * Chrome 69 的 CDP 没有 Input.dispatchDragEvent。

可行的路子是利用 E-Mobile 主窗口的 nodeIntegration:
  1. 页面内 `require('fs').readFileSync()` 读本地文件(不用传 28MB 数据);
  2. 构造真实 `File` + `DataTransfer`;
  3. 向 `.oli-drop-area` 派发 DragEvent(拖放发送);
  4. 点「发送」按钮,等输入框清空;
  5. 轮询会话历史验证。

全程不弹原生对话框、不碰键盘焦点、不改文件名 —— 避免误触"重命名"。

⚠ 版本差异:DOM 类名(Quill 编辑器 / 拖放区 / 会话列表 / 发送按钮)
逆向自**开发机 7.2.6**,正式环境 7.1.4 尚未验证。故所有选择器都做成
**候选列表**,按序尝试;失败时报出每个候选命中几个元素 —— 让 7.1.4
第一次跑就能定位是哪个选择器失效,而不是猜。
候选表见 `compat.PROFILES[*].dom`;识别不出版本时用本模块的 `_DEFAULT_DOM`。
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

try:
    import websocket  # type: ignore
except ImportError:  # pragma: no cover
    websocket = None

from .errors import GUIError

MIME_BY_EXT = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf": "application/pdf",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".zip": "application/zip", ".txt": "text/plain", ".csv": "text/csv",
}

#: 只读 DOM 用:找当前会话标题
#: 注意:入参是第一项选择器候选(取到即为"输入框存在",可作为"已进会话"判据)
_FIND_TITLE_JS = r"""
(() => {
  const ed = document.querySelector(%s);
  if (!ed) return null;
  let p = ed, found = [];
  for (let i = 0; i < 10 && p; i++) { p = p.parentElement; if (!p) break;
    p.querySelectorAll('[class*="title"],[class*="name"]').forEach(c => {
      const t=(c.textContent||'').trim(); if (t && t.length<30) found.push(t); }); }
  return found.length ? found : null;
})()
"""

#: 兜底选择器候选表(客户端版本识别不出时用;识别出后以 profile.dom 为准)。
#: 每项都是**按优先级排列的候选**,第一个命中即用,便于跨版本。
_DEFAULT_DOM: Dict[str, Tuple[str, ...]] = {
    # 输入框(Quill 富文本编辑器)
    "editor": (".ql-editor", '[contenteditable="true"]'),
    # 左侧会话列表容器
    "chat_list": (".chat-list-wrapper",
                  '[class*="chat-list-wrapper"]',
                  '[class*="chat-list"]',
                  '[class*="session-list"]'),
    # 聊天区拖放区
    "drop_zone": (".oli-drop-area", '[class*="drop-area"]'),
    # 聊天工具栏「图片」按钮(点它才会创建可用的 input.ql-image)
    "image_button": ('.ql-toolbar .ql-picture',
                     '.ql-toolbar [class*="picture"]',
                     '.ql-toolbar .ql-image',
                     '.ql-toolbar .weaver-img'),
    # 发送按钮(中文文案 "发送")
    "send_button": ('button:text-is("发送")',
                    'button:has-text("发送")',
                    '[class*="send"]'),
}

#: 探针 JS:统计每个候选选择器命中几个元素(-1 = 选择器语法非法)
_COUNT_SELECTORS_JS = r"""
(() => {
  const out = {};
  const want = %s;
  for (const [name, list] of Object.entries(want)) {
    out[name] = list.map(sel => {
      let n = 0;
      try { n = document.querySelectorAll(sel).length; }
      catch (e) { n = -1; }
      return { selector: sel, count: n };
    });
  }
  return out;
})()
"""


def default_dom() -> Dict[str, Tuple[str, ...]]:
    """兜底选择器候选表的只读副本。"""
    return {k: tuple(v) for k, v in _DEFAULT_DOM.items()}


def _mime_for(path: str) -> str:
    return MIME_BY_EXT.get(os.path.splitext(path)[1].lower(),
                           "application/octet-stream")


def _pick_main_page(targets: list) -> Optional[Dict[str, Any]]:
    """挑 E-Mobile 主窗口(优先 OA 页面,其次任意 page target)。

    7.2.6 主窗 URL 含 `/emweb/` 或 `/client/main/`;7.1.4 的 URL 形态未验证,
    故放宽为"优先 emweb → 其次 url-input → 退回首个 page target",不再要求
    URL 必须含 emweb。
    """
    pages = [t for t in targets
             if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    for marker in ("emweb", "client/main", "url-input"):
        for t in pages:
            if marker in t.get("url", ""):
                return t
    return pages[0] if pages else None


#: 在左侧会话列表里定位一个会话项(精确匹配优先,目标不可见时先滚动)。
#: 容器选择器按候选列表逐个试(见 `_locate_js`),跨版本可用。
_LOCATE_ITEM_JS = r"""
(() => {
  const want = %s;
  const candidates = %s;
  let wrap = null;
  for (const s of candidates) { wrap = document.querySelector(s); if (wrap) break; }
  if (!wrap) return {ok: false, err: '会话列表容器未命中: ' + candidates.join(' | ')};

  const pick = (pred) => {
    let hit = null;
    wrap.querySelectorAll('*').forEach(e => {
      if (hit || e.children.length !== 0) return;
      const t = (e.textContent || '').trim();
      if (t && pred(t)) { hit = e; }
    });
    return hit;
  };

  let el = pick(t => t === want) || pick(t => t.startsWith(want));
  if (!el) return {ok: false, err: 'not found in list'};

  // 目标可能被滚出可视区:先滚进来,再取位置
  try { el.scrollIntoView({block: 'nearest', inline: 'nearest'}); } catch (e) {}
  const r = el.getBoundingClientRect();
  return {
    ok: true,
    text: (el.textContent || '').trim(),
    x: Math.round(r.x + r.width / 2),
    y: Math.round(r.y + r.height / 2)
  };
})()
"""


def _locate_js(name: str, container_candidates: Any) -> str:
    """生成定位会话项的 JS(容器候选 + 目标会话名均已注入)。"""
    candidates = list(container_candidates) if container_candidates \
        else list(_DEFAULT_DOM["chat_list"])
    return _LOCATE_ITEM_JS % (json.dumps(name), json.dumps(candidates))


class GuiClient:
    """CDP 操作 E-Mobile GUI。只做已明确的几件事,不盲目模拟输入。"""

    def __init__(self, port: int = 0, log: Optional[Any] = None,
                 auto_launch: bool = True,
                 profile: Optional[Any] = None) -> None:
        """
        port: 调试端口;0/None 表示自动发现(找不到就尝试自动启动 E-Mobile)。
        auto_launch: 端口不可用时是否自动启动 E-Mobile。
        profile: 版本档案(compat.VersionProfile),决定 DOM 选择器候选;
                 传 None 时用 `_DEFAULT_DOM`(未知版本的并集)。
        """
        if websocket is None:
            raise GUIError("需要 websocket-client:pip install websocket-client")
        self._log = log or (lambda *a, **k: None)
        self._ws = None
        self._next_id = 1
        self.auto_launch = auto_launch
        #: 图片消息大小上限(超过直接报错,不发给客户端碰运气;可用环境变量覆盖)
        try:
            self.max_image_bytes = int(
                os.environ.get("EMCLI_MAX_IMAGE_BYTES") or 8 * 1024 * 1024)
        except ValueError:
            self.max_image_bytes = 8 * 1024 * 1024
        self.port = self._resolve_port(port)
        self._launched_by_us = False
        # 选择器候选表:profile 优先,缺失项用兜底补齐
        self._dom_candidates: Dict[str, List[str]] = {}
        dom = dict(getattr(profile, "dom", {}) or {})
        for name, fallback in _DEFAULT_DOM.items():
            merged = list(dom.get(name) or []) + [s for s in fallback
                                                 if s not in dom.get(name, ())]
            self._dom_candidates[name] = merged
        self.profile_name = str(getattr(profile, "name", "unknown"))

    @property
    def dom_candidates(self) -> Dict[str, List[str]]:
        """选择器候选表(懒初始化 —— 兼容 `__new__` 构造的实例/测试替身)。"""
        cached = getattr(self, "_dom_candidates", None)
        if cached:
            return cached
        cached = {name: list(fallback) for name, fallback in _DEFAULT_DOM.items()}
        self._dom_candidates = cached
        return cached

    # ---- 选择器 ---- #
    def selector(self, feature: str, index: int = 0) -> str:
        """取某功能的选择器候选(默认第一个。通常直接用 `first_hit()`)。"""
        candidates = self.dom_candidates.get(feature) or \
            _DEFAULT_DOM.get(feature, ("",))
        return candidates[index] if index < len(candidates) else candidates[-1]

    def first_hit(self, feature: str) -> str:
        """在页面上探到该功能**第一个有命中**的选择器;一个都没有返回 ""。

        用于跨版本:7.2.6 认 `.ql-editor`,7.1.4 若是 `[contenteditable]`
        也能被认出,而不必事先知道对方用哪个。
        """
        counts = self.count_selectors(feature)
        for item in counts:
            if item.get("count", 0) > 0:
                return str(item.get("selector"))
        return ""

    def count_selectors(self, feature: Optional[str] = None) -> List[Dict[str, Any]]:
        """统计候选选择器各命中几个元素(-1 表示选择器语法非法)。

        feature 非空 → 扁平列表;否则按功能分组。页面返回异常(测试替身 /
        旧版 CDP 返回结构不同)时返回空列表,交由调用方按"无命中"处理。
        """
        if feature:
            want = {feature: self.dom_candidates.get(
                feature) or _DEFAULT_DOM.get(feature, ("",))}
        else:
            want = {name: tuple(sels) for name, sels
                    in self.dom_candidates.items()}
        try:
            result = self.eval_json(_COUNT_SELECTORS_JS % json.dumps(
                {k: list(v) for k, v in want.items()}))
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(result, dict):
            return []
        flat: List[Dict[str, Any]] = []
        for _name, items in result.items():
            if isinstance(items, list):
                flat.extend(i for i in items if isinstance(i, dict))
        return flat

    def dom_report(self) -> Dict[str, Any]:
        """完整 DOM 能力报告(兼容模式诊断用)。"""
        raw = self.eval_json(_COUNT_SELECTORS_JS % json.dumps(
            {k: list(v) for k, v in self.dom_candidates.items()}))
        report: Dict[str, Any] = {}
        if not isinstance(raw, dict):
            return report
        for name, items in raw.items():
            if not isinstance(items, list):
                continue
            hits = [i for i in items
                    if isinstance(i, dict) and (i.get("count") or 0) > 0]
            report[name] = {
                "available": bool(hits),
                "chosen": str(hits[0].get("selector")) if hits else None,
                "candidates": items,
            }
        return report

    def _resolve_port(self, port: Optional[int]) -> int:
        from . import app as appmod
        found = appmod.find_live_port() if port in (None, 0) else port
        if found:
            return found
        if not self.auto_launch:
            raise GUIError(
                f"调试端口不可用(试过 {port or '自动发现'})。"
                "请以 `E-Mobile.exe --remote-debugging-port=9333` 启动,"
                "或用 `emcli setup` 让本工具自动启动。")
        self._launched_by_us = True
        return appmod.ensure_port(preferred=port)

    # ---- 连接 ---- #
    def __enter__(self) -> "GuiClient":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def connect(self) -> None:
        try:
            targets = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/list", timeout=5).read())
        except Exception as exc:  # noqa: BLE001
            raise GUIError(
                f"连不上 E-Mobile 调试端口 {self.port}({exc})。"
                f"请先 `emcli setup`,或以 "
                f"`E-Mobile.exe --remote-debugging-port={self.port}` 启动") from exc
        page = _pick_main_page(targets)
        if page is None:
            raise GUIError("没找到 E-Mobile 页面目标(可能未登录)")
        self._ws = websocket.create_connection(page["webSocketDebuggerUrl"],
                                               timeout=60)

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    # ---- CDP ---- #
    def _call(self, method: str, params: Optional[dict] = None) -> dict:
        i = self._next_id
        self._next_id += 1
        body: Dict[str, Any] = {"id": i, "method": method}
        if params:
            body["params"] = params
        self._ws.send(json.dumps(body))
        while True:
            msg = json.loads(self._ws.recv())
            if msg.get("id") == i:
                return msg

    def eval(self, expression: str, timeout_s: int = 60) -> Any:
        res = self._call("Runtime.evaluate",
                         {"expression": expression, "returnByValue": True})
        result = res.get("result") or {}
        if "exceptionDetails" in result:
            raise GUIError("页面求值异常: "
                           + json.dumps(result["exceptionDetails"],
                                        ensure_ascii=False)[:300])
        return (result.get("result") or {}).get("value")

    def eval_json(self, expression: str) -> dict:
        raw = self.eval(expression)
        if isinstance(raw, str):
            return json.loads(raw)
        if isinstance(raw, dict):
            return raw
        raise GUIError(f"页面返回类型异常: {type(raw)}")

    # ---- 只读探测 ---- #
    def current_titles(self) -> List[str]:
        """当前会话标题候选(JS 返回的是数组,不是对象)。

        用解析出来的输入框选择器当锚点 —— 输入框找不到就说明不在会话里,
        返回空列表(比硬编码 `.ql-editor` 更能跨版本)。
        """
        anchor = self.first_hit("editor") or self.selector("editor")
        raw = self.eval(_FIND_TITLE_JS % json.dumps(anchor))
        if isinstance(raw, list):
            return [str(t) for t in raw if t]
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
                return [str(t) for t in parsed if t]
            except (json.JSONDecodeError, TypeError):
                return [raw]
        return []

    def assert_conversation(self, expect_substring: str) -> None:
        if expect_substring not in self.current_title():
            raise GUIError(
                f"GUI 当前会话是「{self.current_title() or '(未知)'}」,"
                f"不是「{expect_substring}」。可加 `--no-open` 跳过自动切换,"
                "或先在 E-Mobile 里打开目标会话。")

    def current_title(self) -> str:
        """当前会话标题 —— 取离输入框**最近**的那个标题候选。

        注意:消息气泡里也含 [class*=name] 元素(发送者名/群名提示),
        所以不能用"任一候选里包含目标名"来判断当前会话(会误判)。
        """
        titles = self.current_titles()
        return titles[0] if titles else ""

    def is_in_conversation(self, name: str) -> bool:
        return name in self.current_title()

    # ---- 发送 ---- #
    def _editor_selector(self) -> str:
        """解析出当前页面命中的输入框选择器。

        探不到命中时退回第一个候选(而不是直接报错):探测本身可能因
        eval 返回结构不同而失真,先让后续步骤用自己的方式报真实错误。
        只有当所有候选都确认无命中(探测可信)时才抛携带命中数的异常。
        """
        candidates = self.dom_candidates.get("editor") or _DEFAULT_DOM["editor"]
        sel = self.first_hit("editor")
        if sel:
            return sel
        counts = self.count_selectors("editor")
        # 探测可信(拿到了结构化结果)且确实一个都没命中 → 才算真的找不到
        probed = bool(counts) and all(
            isinstance(c, dict) and c.get("count") is not None
            for c in counts)
        if probed:
            detail = ", ".join(f"{c.get('selector')}→{c.get('count')}"
                               for c in counts)
            raise GUIError(
                "找不到聊天输入框。各候选选择器命中数:" + detail + "\n"
                "若客户端版本与预期不符,请用 `emcli compat` 查看能力报告。")
        return candidates[0]

    def clear_editor(self) -> None:
        """清空输入框(避免把草稿/上一个文件一起发出去)。"""
        sel = self._editor_selector()
        self.eval(r"""
(() => {
  const ed = document.querySelector(%s);
  if (!ed) return false;
  const cont = ed.closest('.ql-container') || ed.parentElement;
  const q = cont && cont.__quill;
  if (q && typeof q.setText === 'function') q.setText('');
  else { ed.innerHTML = ''; }
  return true;
})()
""" % json.dumps(sel))

    def editor_state(self) -> dict:
        sel = self._editor_selector()
        return self.eval_json(r"""
(() => {
  const ed = document.querySelector(%s);
  return {
    hasBlot: !!document.querySelector('.quill-file-blot, .file-blot-name'),
    textLen: (ed ? (ed.textContent || '').trim() : '').length,
    uploading: !!document.querySelector('[class*="uploading"],[class*="upload"][class*="progress"]')
  };
})()
""" % json.dumps(sel))

    def editor_html_len(self) -> int:
        sel = self._editor_selector()
        return int(self.eval_json(r"""
(() => { const ed = document.querySelector(%s);
         return {n: (ed ? ed.innerHTML : '').length}; })()
""" % json.dumps(sel))["n"])

    def click_send(self) -> None:
        counts = self.count_selectors("send_button")
        n = self.eval_json(r"""
(() => {
  const btns = [...document.querySelectorAll('button')]
    .filter(b => (b.textContent || '').trim() === '发送' && !b.disabled);
  if (!btns.length) return {n: 0};
  btns[btns.length - 1].click();
  return {n: btns.length};
})()
""")["n"]
        if not n:
            detail = ", ".join(f"{c.get('selector')}→{c.get('count')}"
                               for c in counts) or "(无候选)"
            raise GUIError(
                "没找到可用的「发送」按钮。候选选择器命中数:" + detail)
    def click_at(self, x: int, y: int) -> None:
        for mtype in ("mousePressed", "mouseReleased"):
            self._call("Input.dispatchMouseEvent",
                       {"type": mtype, "x": x, "y": y,
                        "button": "left", "clickCount": 1})
        time.sleep(0.12)

    # ---- 文本 ---- #
    def send_text(self, text: str, expect_title: Optional[str] = None) -> None:
        """发送文本到当前会话。

        expect_title:目标会话名。给了就会先自动打开并校验该会话
        —— 不校验的话,当前打开的是别的群时文本会发到那个群去。
        """
        if expect_title:
            self.open_conversation(expect_title)
        sel = self._editor_selector()
        self.clear_editor()
        # 真实点击输入框获得焦点/选区(quill 不吃 focus()+insertText)
        box = self.eval_json(r"""
(() => { const r = document.querySelector(%s).getBoundingClientRect();
         return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)}; })()
""" % json.dumps(sel))
        self.click_at(box["x"], box["y"])
        state = self.eval_json(r"""
(() => {
  const sel = %s;
  const text = %s;
  const ed = document.querySelector(sel);
  if (!ed) return {ok: false, via: 'none', err: 'no editor'};
  const cont = ed.closest('.ql-container') || ed.parentElement;
  const q = cont && cont.__quill;
  // 多行必须走 quill.setText：execCommand('insertText') 的换行会被编辑器吃掉，
  // 导致 textContent 与入参比对失败（实测报「输入框内容与预期不符」）。
  if (q && typeof q.setText === 'function') {
    q.setText(text);
    const got = String(q.getText ? q.getText() : '').replace(/\n+$/, '');
    if (got === text) return {ok: true, via: 'quill-setText'};
    if (!text.includes('\n') && String(ed.textContent || '').trim() === text) {
      return {ok: true, via: 'quill-setText'};
    }
    return {ok: false, via: 'quill-setText', err: 'echo mismatch',
            echoed: got.slice(0, 80), expected: text.slice(0, 80)};
  }
  ed.focus();
  const ok = document.execCommand('insertText', false, text);
  const echo = String(ed.textContent || '').trim();
  return {ok: !!(ok && echo === text), via: 'insertText',
          err: (ok && echo === text) ? null : 'echo mismatch',
          echoed: echo.slice(0, 80), expected: text.slice(0, 80)};
})()
""" % (json.dumps(sel), json.dumps(text)))
        if not state.get("ok"):
            raise GUIError(
                "输入框内容与预期不符"
                f"(写入方式={state.get('via')}): {state.get('echoed')!r}"
                f" != {state.get('expected')!r}")
        self.click_send()
        for _ in range(45):
            time.sleep(0.15)
            st = self.editor_state()
            if st["textLen"] == 0 and not st["hasBlot"]:
                return
        raise GUIError("点了发送但输入框未清空,发送可能未生效")

    # ---- 文件 ---- #
    def dropzone_info(self) -> dict:
        """聊天拖放区的位置与尺寸(动态取,不写死坐标)。"""
        candidates = self.dom_candidates.get("drop_zone") or \
            _DEFAULT_DOM["drop_zone"]
        return self.eval_json(r"""
(() => {
  const sel = %s;
  let z = null;
  for (const s of sel) { z = document.querySelector(s); if (z) break; }
  if (!z) return {ok: false};
  const r = z.getBoundingClientRect();
  return {ok: true, cls: String(z.className),
          x: Math.round(r.x), y: Math.round(r.y),
          w: Math.round(r.width), h: Math.round(r.height),
          cx: Math.round(r.x + r.width / 2),
          cy: Math.round(r.y + r.height / 2)};
})()
""" % json.dumps(list(candidates)))

    def send_file(self, file_path: str, expect_title: str) -> dict:
        """把文件拖进聊天区并发送。返回 {fileName, fileSize, fileId}。"""
        path = os.path.abspath(file_path)
        if not os.path.isfile(path):
            raise GUIError(f"文件不存在: {path}")
        size = os.path.getsize(path)
        self.assert_conversation(expect_title)
        self.clear_editor()

        zone = self.dropzone_info()
        if not zone.get("ok"):
            candidates = self.dom_candidates.get("drop_zone") or \
                _DEFAULT_DOM["drop_zone"]
            raise GUIError(
                "找不到聊天拖放区。候选选择器:" + " | ".join(candidates))

        self._log(f"拖入文件 {os.path.basename(path)} ({size/1024/1024:.1f} MB)"
                  f" → 拖放区中心 ({zone['cx']},{zone['cy']})")
        # 坐标用拖放区中心(不同分辨率/DPI 自适应),不写死
        result = self.eval_json(_DROP_JS % (json.dumps(path),
                                            json.dumps(_mime_for(path)),
                                            json.dumps(list(
                                                self.dom_candidates.get(
                                                    "drop_zone")
                                                or _DEFAULT_DOM["drop_zone"])),
                                            zone["cx"], zone["cy"]))
        if not result.get("dropped"):
            raise GUIError(f"拖放未被应用接受: {result}")

        st = self.editor_state()
        if not st.get("hasBlot"):
            raise GUIError("拖放后输入框里没有文件,应用可能未接受该文件")

        self.click_send()
        for _ in range(60):
            time.sleep(0.2)
            st = self.editor_state()
            if not st["hasBlot"] and st["textLen"] == 0:
                break
        else:
            raise GUIError("点了发送但输入框未清空")

        return {"fileName": os.path.basename(path), "fileSize": size,
                "dropped": True}

    # ---- 图片 ---- #
    def send_image(self, file_path: str, expect_title: str,
                   timeout_s: int = 60) -> dict:
        """把本地图片作为「图片消息」发送。

        为什么不走 `send_file` 的拖放:实测客户端**不接受 image 类型**的拖放
        (PNG/JPG,含 1x1/70B 最小图,报「拖放后输入框里没有文件」)。

        可行路子(2026-09-30 实测固化):
          1. CDP 真实点击聊天工具栏「图片」按钮(`.ql-toolbar .ql-picture`)——
             客户端此时才在**编辑器容器内**创建带 accept=image/* 的
             `input.ql-image` 并接好 change 消费逻辑;
             纯 JS `.click()` 触发不了(非可信手势,实测不创建),
             必须走 `Input.dispatchMouseEvent` 真实鼠标事件;
          2. 把图片塞进**编辑器容器内**的 `input.ql-image` 并触发 change,
             等图片在编辑器里真正就绪(`<img class="oli-image">`)再点发送。

        ⚠️ 绝不扫全页 `input[type=file]`:聊天页唯一的常驻文件输入框是
        `span.avatar-uploader` 里的**群头像上传器**,填了会把图片喂给
        群头像替换接口(2026-09-30 用户实测踩坑)。这里只认编辑器容器内、
        点击「图片」按钮后出现的 `input.ql-image`;没有它就直接报错,
        不静默降级成文件附件。

        返回 {fileName, fileSize, route, staged}。图片必须是 PNG/JPG 等
        客户端认识的图片类型。
        """
        path = os.path.abspath(file_path)
        if not os.path.isfile(path):
            raise GUIError(f"图片不存在: {path}")
        size = os.path.getsize(path)
        if size <= 0:
            raise GUIError(f"图片为空文件: {path}")
        if size > self.max_image_bytes:
            raise GUIError(
                f"图片 {size/1024/1024:.1f}MB 超过上限 "
                f"{self.max_image_bytes/1024/1024:.1f}MB(客户端可能拒绝)")

        self.assert_conversation(expect_title)
        self.clear_editor()

        with open(path, "rb") as fh:
            payload = base64.b64encode(fh.read()).decode("ascii")
        sel = self._editor_selector()
        basename = os.path.basename(path)

        # 前端拦截系统「打开」框(Chromium69 无 CDP 拦截能力),发送结束/异常都恢复原型
        suppress = bool(self.eval_json(_SUPPRESS_FILE_CLICK_JS).get("ok"))
        try:
            result = self.eval_json(_IMAGE_FILL_JS % (
                json.dumps(sel),
                json.dumps(payload),
                json.dumps(basename),
            ))
            if not result.get("ok"):
                # 常态:input.ql-image 不存在 → 先真实点击工具栏「图片」按钮创建它
                self._click_toolbar_image_button()
                self._wait_editor_image_input(sel)
                result = self.eval_json(_IMAGE_FILL_JS % (
                    json.dumps(sel),
                    json.dumps(payload),
                    json.dumps(basename),
                ))
            if not result.get("ok"):
                raise GUIError(
                    "图片未能放进输入框(只认编辑器容器内的 input.ql-image,"
                    "不会去填群头像上传器;客户端可能不支持图片消息): "
                    + json.dumps(result, ensure_ascii=False))

            self._log(f"图片已写入输入框({result.get('route')}),"
                      f"等待就绪 {basename} ({size/1024/1024:.2f} MB)")
            if not self._wait_image_staged(sel, timeout_s):
                raise GUIError(
                    "图片写入后未在输入框就绪,已取消发送(避免发出空消息)")

            self.click_send()
            # 图片的 hasBlot 不认 .oli-image,故额外确认编辑器里的 <img> 已消失,
            # 且先给客户端 0.5s 把图片 blot 移出编辑器,避免"刚清空就误报成功"。
            time.sleep(0.5)
            deadline = time.time() + max(5.0, float(timeout_s))
            while time.time() < deadline:
                st = self.editor_state()
                has_img = bool(self.eval_json(
                    "(() => { const ed = document.querySelector(%s);"
                    " return {img: !!(ed && ed.querySelector('img.oli-image, img'))}; })()"
                    % json.dumps(sel)).get("img"))
                if (st["textLen"] == 0 and not st["hasBlot"]
                        and not st["uploading"] and not has_img):
                    break
                time.sleep(0.2)
            else:
                raise GUIError("点了发送但输入框未清空")

            return {"fileName": basename, "fileSize": size,
                    "route": result.get("route"), "staged": True}
        finally:
            if suppress:
                try:
                    self.eval_json(_RESTORE_FILE_CLICK_JS)
                except Exception:  # noqa: BLE001
                    # 页面重载/ws 断开时恢复可能失败,不能掩盖原始异常
                    pass

    def _click_toolbar_image_button(self) -> None:
        """CDP 真实点击聊天工具栏「图片」按钮,让客户端创建 input.ql-image。

        必须用 `Input.dispatchMouseEvent`(可信手势):纯 JS `.click()` 实测
        不会触发客户端创建图片输入框。坐标取按钮中心,分辨率/DPI 自适应。
        """
        candidates = self.dom_candidates.get("image_button") or \
            _DEFAULT_DOM["image_button"]
        pos = self.eval_json(_IMAGE_BUTTON_JS % json.dumps(list(candidates)))
        if not pos.get("ok"):
            counts = self.count_selectors("image_button")
            detail = ", ".join(f"{c.get('selector')}→{c.get('count')}"
                               for c in counts) or "(无候选)"
            raise GUIError(
                "找不到聊天工具栏「图片」按钮(" + str(pos.get("err")) + ")。"
                "候选选择器命中数:" + detail + "。"
                "该客户端版本可能不支持图片消息,请用 `emcli compat` 诊断。")
        self._log(f"点击工具栏「图片」按钮 ({pos['x']},{pos['y']})")
        for mtype in ("mousePressed", "mouseReleased"):
            self._call("Input.dispatchMouseEvent",
                       {"type": mtype, "x": pos["x"], "y": pos["y"],
                        "button": "left", "clickCount": 1})

    def _wait_editor_image_input(self, sel: str, timeout_s: float = 5.0,
                                 interval_s: float = 0.15) -> None:
        """等「图片」按钮点击后在编辑器容器内创建出 input.ql-image。"""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.eval_json(_HAS_IMAGE_INPUT_JS % json.dumps(sel)).get("ok"):
                return
            time.sleep(interval_s)
        raise GUIError(
            f"等待图片输入框 input.ql-image 超时({timeout_s:g}s):"
            "点击「图片」按钮后客户端未创建可用的图片输入框")

    def _wait_image_staged(self, sel: str, timeout_s: int = 60) -> bool:
        """等图片真正进编辑器。

        实测就绪信号:编辑器里出现 `<img class="oli-image">`(data URL 预览);
        另有 file blot / 上传中状态也算就绪。
        """
        deadline = time.time() + max(1, timeout_s)
        while time.time() < deadline:
            st = self.editor_state()
            has_img = bool(self.eval_json(
                "(() => { const ed = document.querySelector(%s);"
                " return {img: !!(ed && ed.querySelector('img.oli-image, img'))}; })()"
                % json.dumps(sel)
            ).get("img"))
            if has_img or st.get("hasBlot") or st.get("uploading"):
                return True
            time.sleep(0.3)
        return False

    def open_conversation(self, name: str, timeout_s: int = 20) -> None:
        """在左侧会话列表里按名字点击打开会话(避免要求用户手动切)。

        通用性关键:定位只在会话列表容器内、目标不可见时先滚动,
        不同分辨率/DPI 都用元素实际位置,不写死坐标。
        会话列表容器本身也是候选选择器(见 `chat_list`)。
        """
        # 已经在目标会话就不用点
        if self.is_in_conversation(name):
            self._log(f"当前已在会话「{name}」")
            return

        pos = self.eval_json(_locate_js(name, self.dom_candidates.get(
            "chat_list") or _DEFAULT_DOM["chat_list"]))
        if not pos.get("ok"):
            counts = self.count_selectors("chat_list")
            detail = ", ".join(f"{c.get('selector')}→{c.get('count')}"
                               for c in counts) or "(无候选)"
            raise GUIError(
                f"在会话列表里找不到「{name}」({pos.get('err')})。"
                "会话列表容器候选命中数:" + detail + "。"
                "可先用 `emcli chat list` 查看确切名字;"
                "若会话被折叠在某个分组里,请在 GUI 里展开。")
        self._log(f"点击会话「{pos.get('text') or name}」"
                  f"({pos['x']},{pos['y']})")
        self.click_at(pos["x"], pos["y"])

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(0.2)
            if self.is_in_conversation(name):
                return
        raise GUIError(f"点击后 {timeout_s}s 内未能打开会话「{name}」"
                       f"(当前:「{self.current_title()}」)")

    def wait_for_file_message(self, file_name: str, probe,
                              since_time: Optional[str] = None,
                              attempts: int = 6, interval_s: int = 10,
                              first_delay_s: int = 10,
                              max_consecutive_errors: int = 3) -> dict:
        """发送后复核消息是否真的生效。

        节奏(默认):先等 10s 再查 → 之后每 10s 一次 → 最多 6 次,
        总窗口约 60s(1 分钟)。

        为什么要先等:一发完就查,消息往往还没落地,必然查不到,
        然后反复空转 —— 所以首次延迟是必须的。

        防"陷入循环":
          * 尝试次数有硬上限(attempts);
          * 连续查询失败 max_consecutive_errors 次即中止,不死磕;
          * 只认 sendTime 新于 since_time 的消息,避免把同名旧消息误判成功。

        注意:上传耗时与文件大小相关(21MB 实测约 4 分钟)。1 分钟窗口内
        没确认到,只代表"尚未落地",不代表发送失败 —— 调用方应把这点说清楚。
        """
        time.sleep(max(0, first_delay_s))
        errors = 0
        seen_old = False
        for attempt in range(1, max(1, attempts) + 1):
            try:
                items = probe()
                errors = 0
            except Exception as exc:  # noqa: BLE001
                errors += 1
                self._log(f"  第 {attempt}/{attempts} 次复核查询失败"
                          f"(连续 {errors}/{max_consecutive_errors}): {exc}")
                if errors >= max_consecutive_errors:
                    raise GUIError(
                        f"连续 {errors} 次查询失败,停止复核(避免死循环): {exc}")
                if attempt < attempts:
                    time.sleep(interval_s)
                continue

            for item in items:
                if file_name not in str(item.get("content", "")):
                    continue
                item_time = str(item.get("sendTime") or "")
                if since_time and item_time and item_time <= str(since_time):
                    seen_old = True        # 只看到同名旧消息,继续等
                    continue
                return item

            if attempt < attempts:
                self._log(f"  第 {attempt}/{attempts} 次复核:暂未确认,"
                          f"{interval_s}s 后再查"
                          + ("(目前只看到同名旧消息)" if seen_old else ""))
                time.sleep(interval_s)

        budget = first_delay_s + (attempts - 1) * interval_s
        raise GUIError(
            f"{attempts} 次复核(约 {budget}s)内未确认「{file_name}」已生效。"
            "消息可能仍在后台上传中,不代表发送失败;"
            "可用 `emcli chat history <会话>` 稍后手动确认,"
            "或调大 --attempts / --interval。")


_DROP_JS = r"""
(() => {
  try {
    const fs = require('fs');
    const path = require('path');
    const p = %s;
    const buf = fs.readFileSync(p);
    const file = new File([buf], path.basename(p), {type: %s});
    const dt = new DataTransfer();
    dt.items.add(file);
    // 拖放区按候选选择器逐个找(与 dropzone_info 保持一致)
    const zones = %s;
    let zone = null;
    for (const s of zones) { zone = document.querySelector(s); if (zone) break; }
    if (!zone) return {err: 'no drop zone: ' + zones.join(' | '),
                       zoneCandidates: zones};
    const r = zone.getBoundingClientRect();
    const cx = %s, cy = %s;          // 由调用方按拖放区实际位置给出
    const mk = (type) => {
      const ev = new DragEvent(type, {
        bubbles: true, cancelable: true, composed: true,
        dataTransfer: dt, clientX: cx, clientY: cy
      });
      zone.dispatchEvent(ev);
      return ev.defaultPrevented;
    };
    return {
      dropped: mk('drop'),
      dragOverPrevented: mk('dragover'),
      file: {name: file.name, size: file.size},
      zone: {cx: cx, cy: cy}
    };
  } catch (e) {
    return {err: String(e), stack: (e.stack || '').slice(0, 300)};
  }
})()
"""

#: 图片入口(2026-09-30 实测固化,7.2.6 订制 quill / weaver 主题):
#:   * 聊天页**没有**常驻 `input.ql-image` —— 只有点击工具栏「图片」按钮
#:     (`div.ql-picture`,内含 `i.weaver-img`)后,客户端才在**编辑器容器内**
#:     动态创建 `input.ql-image`(accept=image/*)并接好 change 消费逻辑;
#:   * 点击「图片」按钮必须走 CDP `Input.dispatchMouseEvent`(可信手势),
#:     纯 JS `.click()` 实测不会创建该输入框;
#:   * 页面固有 input[type=file](accept="") 是 `span.avatar-uploader` 里的
#:     **群头像上传器**,填了没人往聊天里放图、反而触发群头像替换接口
#:     (2026-09-30 用户实测踩坑)—— 因此这里绝不全页扫 `input[type=file]`,
#:     只认编辑器容器内的 `input.ql-image`;没有它就直接报错,
#:     也不静默降级成文件附件。
#: 填好后编辑器里出现 `<img class="oli-image" src="data:image/png;base64,...">`。
_IMAGE_BUTTON_JS = r"""
(() => {
  try {
    const candidates = %s;
    let btn = null, used = '';
    for (const s of candidates) {
      const el = document.querySelector(s);
      if (el) { btn = el; used = s; break; }
    }
    if (!btn) return {ok: false, err: 'no image button: '
                      + candidates.join(' | ')};
    const r = btn.getBoundingClientRect();
    if (r.width <= 0 && r.height <= 0) {
      return {ok: false, err: 'image button not visible: ' + used};
    }
    return {ok: true, x: Math.round(r.x + r.width / 2),
            y: Math.round(r.y + r.height / 2), used: used};
  } catch (e) {
    return {ok: false, err: String(e)};
  }
})()
"""

#: 编辑器容器内是否已存在 input.ql-image(只认容器内,不碰头像上传器)
_HAS_IMAGE_INPUT_JS = r"""
(() => {
  const ed = document.querySelector(%s);
  const cont = ed && (ed.closest('.ql-container') || ed.parentElement);
  if (!cont) return {ok: false, err: 'no editor container'};
  return {ok: !!cont.querySelector('input.ql-image'),
          count: cont.querySelectorAll('input.ql-image').length};
})()
"""

#: 发送图片前:吞掉 input.ql-image 的 .click(),阻止 Chromium 弹出系统「打开」框。
#: Chromium 69 / Electron 4.2 没有 Page.setInterceptFileChooserDialog(M77 才有),
#: 只能在前端拦截:覆盖原型 click 后,按钮 handler 仍会 append input.ql-image,
#: 只是它的 .click() 被吞,不会弹原生窗口;随后用 DataTransfer 注入图片。
_SUPPRESS_FILE_CLICK_JS = r"""
(() => {
  try {
    const proto = window.HTMLInputElement && window.HTMLInputElement.prototype;
    if (!proto || !proto.click) return {ok: false, err: 'no input.click'};
    if (window.__emcliSuppressQlImageClick) return {ok: true, already: true};
    const orig = proto.click;
    proto.click = function () {
      try {
        if (this && this.type === 'file' &&
            this.classList && this.classList.contains('ql-image')) {
          return;   // 吞掉原生文件框;元素仍在,后续用 DataTransfer 注入
        }
      } catch (e) { /* ignore */ }
      return orig.apply(this, arguments);
    };
    window.__emcliSuppressQlImageClick = { orig: orig, proto: proto };
    return {ok: true};
  } catch (e) {
    return {ok: false, err: String(e)};
  }
})()
"""

#: 恢复被 _SUPPRESS_FILE_CLICK_JS 覆盖的 HTMLInputElement.prototype.click。
_RESTORE_FILE_CLICK_JS = r"""
(() => {
  try {
    const s = window.__emcliSuppressQlImageClick;
    if (!s) return {ok: true, already: true};
    try { s.proto.click = s.orig; } catch (e) { /* ignore */ }
    try { delete window.__emcliSuppressQlImageClick; } catch (e) { /* ignore */ }
    return {ok: true};
  } catch (e) {
    return {ok: false, err: String(e)};
  }
})()
"""

#: 把图片塞进**编辑器容器内**的 input.ql-image 并触发 change。
#: 绝不回退到全页 input[type=file] —— 全页唯一的常驻文件输入框是
#: 群头像上传器(span.avatar-uploader),误填会把图片喂给群头像替换接口。
_IMAGE_FILL_JS = r"""
(() => {
  try {
    const sel = %s;
    const b64 = %s;
    const name = %s;
    const ed = document.querySelector(sel);
    if (!ed) return {ok: false, route: null, err: 'no editor: ' + sel};
    const cont = ed.closest('.ql-container') || ed.parentElement;
    if (!cont) return {ok: false, route: null,
                       err: 'no editor container for: ' + sel};
    const pick = cont.querySelector('input.ql-image');
    if (!pick) {
      return {ok: false, route: null,
              err: 'no input.ql-image inside editor container;'
                   + ' 需先真实点击工具栏「图片」按钮,'
                   + '不会去填头像上传器'};
    }
    const bin = atob(b64);
    const buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    const dt = new DataTransfer();
    dt.items.add(new File([buf], name, {type: 'image/png'}));
    pick.files = dt.files;
    pick.dispatchEvent(new Event('input', {bubbles: true}));
    pick.dispatchEvent(new Event('change', {bubbles: true}));
    return {ok: true, route: 'ql-image',
            accept: pick.getAttribute('accept') || '',
            multiple: !!pick.multiple, staged: pick.files.length};
  } catch (e) {
    return {ok: false, route: null, err: String(e),
            stack: String(e && e.stack || '').slice(0, 300)};
  }
})()
"""
