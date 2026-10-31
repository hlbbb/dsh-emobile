"""CLI-Anything 风格输出:人类可读文本 + `--json` 结构化输出。"""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable, List, Optional


class Output:
    """统一输出。json_mode=True 时只输出 JSON,便于 agent 消费。"""

    def __init__(self, json_mode: bool = False, quiet: bool = False,
                 stream=None) -> None:
        self.json_mode = json_mode
        self.quiet = quiet
        self.stream = stream or (sys.stderr if json_mode else sys.stdout)

    # ---- 数据 ---- #
    def data(self, payload: Any) -> None:
        if self.json_mode:
            self._print(json.dumps(payload, ensure_ascii=False, indent=2))
        elif not self.quiet:
            self._print(str(payload))

    def table(self, rows: Iterable[Iterable[Any]], headers: Optional[List[str]] = None,
              title: str = "") -> str:
        rows = [list(r) for r in rows]
        if self.json_mode:
            payload: Any
            if headers is not None:
                payload = [dict(zip(headers, r)) for r in rows]
            else:
                payload = rows
            self._print(json.dumps(payload, ensure_ascii=False, indent=2))
            return ""
        text = _text_table(rows, headers, title)
        if not self.quiet:
            self._print(text)
        return text

    # ---- 日志 ---- #
    def info(self, message: str) -> None:
        if not self.json_mode and not self.quiet:
            self._print(message)

    def error(self, message: str) -> None:
        if self.json_mode:
            self._print(json.dumps({"ok": False, "error": message},
                                   ensure_ascii=False))
        else:
            print("✗ " + message, file=sys.stderr)

    def success(self, message: str) -> None:
        if not self.json_mode and not self.quiet:
            self._print("✓ " + message)

    def _print(self, text: str) -> None:
        stream = self.stream if not self.json_mode else sys.stdout
        print(text, file=stream)


def _text_table(rows: List[List[Any]], headers: Optional[List[str]],
                title: str) -> str:
    """等宽对齐表格(中文按显示宽度折算)。"""
    all_rows = ([list(headers)] if headers else []) + rows
    if not all_rows:
        return title or "(无数据)"
    widths = [0] * max(len(r) for r in all_rows)
    for r in all_rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], _display_width(str(cell)))

    def fmt(cells: List[Any]) -> str:
        out = []
        for i, cell in enumerate(cells):
            text = str(cell)
            pad = widths[i] - _display_width(text)
            out.append(text + " " * max(0, pad))
        return "  ".join(out).rstrip()

    lines = []
    if title:
        lines.append(title)
    if headers:
        lines.append(fmt(headers))
        lines.append("  ".join("-" * w for w in widths))
    lines.extend(fmt(r) for r in rows)
    return "\n".join(lines)


def _display_width(text: str) -> int:
    """CJK 全角字符占 2 列。"""
    width = 0
    for ch in text:
        width += 2 if _is_wide(ch) else 1
    return width


def _is_wide(ch: str) -> bool:
    code = ord(ch)
    return (0x1100 <= code <= 0x115F or 0x2E80 <= code <= 0xA4CF
            or 0xAC00 <= code <= 0xD7A3 or 0xF900 <= code <= 0xFAFF
            or 0xFE30 <= code <= 0xFE4F or 0xFF00 <= code <= 0xFF60
            or 0xFFE0 <= code <= 0xFFE6 or 0x20000 <= code <= 0x3FFFD)
