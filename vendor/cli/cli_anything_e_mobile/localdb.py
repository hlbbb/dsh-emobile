"""本地 em7.db 只读查询(E-Mobile 自带的 SQLite 缓存)。

路径:%APPDATA%\\E-Mobile\\sqlite3\\em7.db
表结构(见 bundle: resources/app.asar → sqlite3/tables/index.js):
  urls      url, lastLoginTime            ← 用户配置过的服务器地址
  users     urlId, loginId, key, password, baseUserId, lastLoginTime
  items     key, value, type, url         ← KV 缓存
            type: 1 file / 2 image / 3 search / 4 draft / 5 system / 6 chatlist
  versions  version, value

安全约束:
  * 只读。任何路径都不打开写连接(immutable=1,失败回退 mode=ro)。
  * 不导出 password 明文,只标记是否已保存凭据。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

ITEM_TYPES: Dict[int, str] = {
    1: "file", 2: "image", 3: "search", 4: "draft", 5: "system", 6: "chatlist",
}


def db_path() -> Path:
    appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
    return Path(appdata) / "E-Mobile" / "sqlite3" / "em7.db"


def exists() -> bool:
    return db_path().exists()


def connect_ro(path: Optional[Path] = None) -> sqlite3.Connection:
    """只读打开(不与运行中的 E-Mobile 抢锁)。"""
    target = Path(path or db_path())
    if not target.exists():
        raise FileNotFoundError(
            f"未找到 {target}。请在 E-Mobile 中登录一次(登录后本机才会生成该库)。")
    for uri in (f"file:{target}?mode=ro&immutable=1", f"file:{target}?mode=ro"):
        try:
            return sqlite3.connect(uri, uri=True, timeout=3)
        except sqlite3.OperationalError:
            continue
    raise sqlite3.OperationalError(f"无法以只读方式打开 {target}")


def tables(conn: sqlite3.Connection) -> List[str]:
    """业务表(排除 sqlite_sequence 等内部表)。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [r[0] for r in rows]


def _query(conn: sqlite3.Connection, sql: str,
           params: tuple = ()) -> List[Dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ---- urls ---- #
def q_urls(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    return _query(conn, "SELECT url, lastLoginTime FROM urls "
                        "ORDER BY lastLoginTime DESC")


# ---- users ---- #
def q_users(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """不导出 password / key 明文,只说明保存了哪些账号。"""
    return _query(conn, """
        SELECT id, urlId, loginId,
               (password IS NOT NULL AND password != '') AS password_saved,
               baseUserId, lastLoginTime
        FROM users ORDER BY lastLoginTime DESC
    """)


# ---- versions ---- #
def q_versions(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    return _query(conn, "SELECT version, value FROM versions")


# ---- items ---- #
def q_items_summary(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """按 type 统计 KV 缓存条数。"""
    rows = _query(conn, """
        SELECT type, COUNT(*) AS count, SUM(LENGTH(value)) AS bytes
        FROM items GROUP BY type ORDER BY type
    """)
    for r in rows:
        r["type_name"] = ITEM_TYPES.get(int(r.get("type") or 0), "unknown")
    return rows


def q_items(conn: sqlite3.Connection, type_: Optional[int] = None,
            key_like: str = "", limit: int = 100) -> List[Dict[str, Any]]:
    sql = "SELECT id, key, type, url, LENGTH(value) AS size FROM items WHERE 1=1"
    params: List[Any] = []
    if type_ is not None:
        sql += " AND type = ?"
        params.append(type_)
    if key_like:
        sql += " AND key LIKE ?"
        params.append(f"%{key_like}%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = _query(conn, sql, tuple(params))
    for r in rows:
        r["type_name"] = ITEM_TYPES.get(int(r.get("type") or 0), "unknown")
    return rows


def q_item_value(conn: sqlite3.Connection, key: str) -> Any:
    """取单个 KV 并尽量解析成结构化对象。"""
    rows = _query(conn, "SELECT key, value, type, url FROM items WHERE key = ?",
                  (key,))
    if not rows:
        return None
    row = rows[0]
    row["type_name"] = ITEM_TYPES.get(int(row.get("type") or 0), "unknown")
    raw = row.get("value")
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        row["parsed"] = None
        return row
    row["parsed"] = parsed
    return row


def q_chatlist(conn: sqlite3.Connection) -> Optional[Any]:
    """尝试取 chatlist(type=6)缓存内容。"""
    rows = _query(conn, "SELECT key, value FROM items WHERE type = 6 "
                        "ORDER BY id DESC LIMIT 1")
    for row in rows:
        try:
            return {"key": row["key"], "data": json.loads(row["value"])}
        except (TypeError, json.JSONDecodeError, KeyError):
            continue
    return None
