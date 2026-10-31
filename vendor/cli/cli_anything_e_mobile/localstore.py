r"""不依赖 CDP:直接解析 E-Mobile 的 Chromium localStorage LevelDB 读登录态。

E-Mobile(Electron)把登录态存在 Chromium 的 localStorage 里,落盘位置:

    %APPDATA%\E-Mobile\Local Storage\leveldb\

可用环境变量 ``EMCLI_EMOBILE_USERDATA`` 覆盖 userData 根目录(测试用)。

这样用户**正常登录客户端**(没开 ``--remote-debugging-port``)时,读类命令也能
直接拿到 ``access_token``,不必为了读 token 去重启客户端开调试端口。

磁盘格式要点(2026-09-24 侦察实测,见本文件顶部常量):
  * 键布局 ``_<origin>\x00\x01<key>``;``\x01`` = localStorage,
    ``\x02`` = sessionStorage(**不读**),``META:<origin>`` 记录(**不读**)。
  * 值首字节 ``\x00`` = UTF-16LE 正文,``\x01`` = Latin-1 正文。
  * ``*.ldb``(SSTable)的 block 值经 **Snappy** 压缩,压缩类型字节在 ``raw[-5]``;
    ``*.log``(WAL)不压缩、UTF-16LE 明文。**两者都要读**,按 internal sequence
    取最新值 —— 只读 ``.ldb`` 会漏掉刚写入、还没 compact 的登录态。
  * 该 build 的 ``BlockHandle.size`` **不含** 5 字节 block trailer(实测:真机
    ``.ldb`` 的 data/index block 都只有 ``size+5`` 才解析得通)。这里仍保留
    ``size+0`` 的退回解释,并用 block 解析校验,以兼容 size 含 trailer 的其它
    leveldb build。
  * Snappy 用纯 stdlib 实现,不引入任何 pip 依赖。
  * **已知限制**:block trailer 里的 **CRC32C 不校验**(Python stdlib 没有现成的
    CRC32C 实现,本项目也不引入第三方依赖)。损坏的压缩块只能靠后续结构解析
    (restart 数组 / 条目长度)间接发现,因此每个 ``.ldb`` 的解析结果都会带**一条**
    "未校验 block CRC32C" 的 warning —— 只标注限制,不代表该文件有问题。

安全红线(务必保持):
  * 对外只暴露登录必需字段(``currentBaseUrl`` / ``access_token`` /
    ``base_user_id`` / ``base_user_name``)。客户端 localStorage 里还明文存着
    ``currentUser.password`` 等敏感字段 —— **绝不读取、返回、打印或写日志**。
  * 所有异常与诊断信息只允许出现键名、origin、长度、布尔,**绝不出现值**。
"""

from __future__ import annotations

import json
import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .cdp import (ACCESS_TOKEN_FIELDS as _CDP_ACCESS_TOKEN_FIELDS,
                  KNOWN_TOKEN_KEYS as _CDP_TOKEN_KEYS,
                  TOKEN_KEY_SUFFIX as _CDP_TOKEN_KEY_SUFFIX)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: LevelDB table footer 魔数(文件末尾 8 字节,小端 u64)
FOOTER_MAGIC = 0xDB4775248B80FB57
#: footer 固定长度:metaindex handle + index handle + padding + magic(8)
FOOTER_SIZE = 48
#: WAL 逻辑块大小(leveldb 默认)
LOG_BLOCK_SIZE = 32768
#: 单文件读取上限:超过直接跳过,避免误读巨型文件把内存吃满
MAX_FILE_BYTES = 512 * 1024 * 1024

#: 登录态键名候选(与 cdp.py 共用同一份来源,避免两处漂移)
KNOWN_TOKEN_KEYS: Tuple[str, ...] = tuple(_CDP_TOKEN_KEYS)

#: 任意以该后缀结尾的键也当候选 —— 兜住将来出现的新前缀。
#: **复用 `cdp.TOKEN_KEY_SUFFIX`**,本文件不再自己写一份(避免漂移)。
TOKEN_KEY_SUFFIX: str = _CDP_TOKEN_KEY_SUFFIX

#: 在 JSON 值里定位 access_token 的字段名。
#: **复用 `cdp.ACCESS_TOKEN_FIELDS`**,本文件不再自己写一份(避免漂移)。
ACCESS_TOKEN_FIELDS: Tuple[str, ...] = tuple(_CDP_ACCESS_TOKEN_FIELDS)

#: 对外白名单:顶层字段
ALLOWED_TOP_FIELDS: Tuple[str, ...] = ("currentBaseUrl", "expire")
#: 对外白名单:currentUser 内的字段
ALLOWED_USER_FIELDS: Tuple[str, ...] = ("access_token", "base_user_id",
                                        "base_user_name")
#: 已知敏感字段名 —— 只用于诊断"是否存在",**绝不取值**
SENSITIVE_FIELD_NAMES: Tuple[str, ...] = ("password", "pwd", "passwd",
                                          "cookie", "refresh_token", "secret")

#: 一条合并后的 LevelDB 记录:(user_key, sequence, value_type, value)
RawEntry = Tuple[bytes, int, int, bytes]


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class LocalStoreError(RuntimeError):
    """磁盘后端读取失败(基类)。消息里只允许出现键名 / origin / 长度。"""


class LocalStoreUnavailableError(LocalStoreError):
    """数据目录不存在、不可读,或所有文件都解析不出记录 —— 后端不可用。"""


class NotLoggedInError(LocalStoreError):
    """读到了数据目录,但磁盘里确实没有可用登录态(客户端处于未登录状态)。"""


class PersistencePendingError(NotLoggedInError):
    """重试耗尽仍未见任何登录态键 —— 可能客户端还没把内存缓存落盘。

    与 :class:`NotLoggedInError` 的区别:这条**不能断言"没登录"**,
    只是"现在读不到",稍后重试可能就有了。
    """


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class LocalRecord:
    """一条已解码的 localStorage 记录。

    ``text`` 是**解码后的正文**,可能含敏感值 —— 调用方**不得**打印或外传;
    ``__repr__`` 已刻意屏蔽 ``text``,避免回溯/日志意外泄露。
    """

    origin: str
    key: str
    text: str
    seq: int
    value_bytes: int
    encoding: str

    def __repr__(self) -> str:  # 安全:绝不把 text 放进 repr
        return (f"LocalRecord(origin={self.origin!r}, key={self.key!r}, "
                f"seq={self.seq}, value_bytes={self.value_bytes}, "
                f"encoding={self.encoding!r}, text=<redacted>)")


@dataclass
class FileReport:
    """单个 leveldb 文件的解析结果(只含计数与错误,不含任何值)。"""

    name: str
    kind: str                      # "ldb" / "log"
    entries: int = 0
    error: str = ""
    warnings: List[str] = field(default_factory=list)


@dataclass
class StoreSnapshot:
    """一次磁盘扫描的合并结果(每个 user_key 取 internal sequence 最大的那条)。"""

    directory: str
    exists: bool
    records: List[LocalRecord] = field(default_factory=list)
    files: List[FileReport] = field(default_factory=list)
    #: 文件级失败(整个文件读不出来)—— 用于判断"后端不可用"
    errors: List[str] = field(default_factory=list)
    #: 文件内局部问题(坏块、截断尾巴)—— 不影响其它文件,只作提示
    warnings: List[str] = field(default_factory=list)

    @property
    def keys(self) -> List[str]:
        """出现过的 localStorage 键名(去重、排序)。"""
        return sorted({r.key for r in self.records})

    @property
    def origins(self) -> List[str]:
        """出现过的 origin(去重、排序)。"""
        return sorted({r.origin for r in self.records})

    def find(self, key: Optional[str] = None,
             origin: Optional[str] = None) -> List[LocalRecord]:
        """按键名 / origin 过滤记录(两者都可省略 = 不过滤)。"""
        return [r for r in self.records
                if (key is None or r.key == key)
                and _origin_matches(r.origin, origin)]

    def __repr__(self) -> str:  # 安全:只报计数
        return (f"StoreSnapshot(directory={self.directory!r}, "
                f"exists={self.exists}, records={len(self.records)}, "
                f"files={len(self.files)}, errors={len(self.errors)})")


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def userdata_dir(userdata: Optional[Any] = None) -> Path:
    """E-Mobile 的 userData 根目录。

    优先级:显式参数 > ``EMCLI_EMOBILE_USERDATA`` > ``%APPDATA%\\E-Mobile``。
    """
    if userdata:
        return Path(userdata)
    override = os.environ.get("EMCLI_EMOBILE_USERDATA")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "E-Mobile"
    return Path.home() / "AppData" / "Roaming" / "E-Mobile"


def leveldb_dir(userdata: Optional[Any] = None) -> Path:
    """localStorage 的 leveldb 目录(``<userData>/Local Storage/leveldb``)。"""
    return userdata_dir(userdata) / "Local Storage" / "leveldb"


# --------------------------------------------------------------------------- #
# Snappy(纯 stdlib)
# --------------------------------------------------------------------------- #
def _read_varint(buf: bytes, pos: int, context: str = "") -> Tuple[int, int]:
    """读一个 LEB128 varint,返回 ``(值, 新位置)``;越界 / 过长抛异常。"""
    result = 0
    shift = 0
    size = len(buf)
    while True:
        if pos >= size:
            raise LocalStoreError(f"varint 越界({context or '未命名'})")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise LocalStoreError(f"varint 过长({context or '未命名'})")


def snappy_decompress(data: bytes) -> bytes:
    """解压 Snappy **raw** 格式(纯 Python,无第三方依赖)。

    格式:前导 varint = 未压缩长度,随后是一串元素;
    元素 tag 低 2 位决定类型:``0`` = literal,``1``/``2``/``3`` = copy
    (1 / 2 / 4 字节偏移)。copy 的偏移可能小于长度(**重叠复制**),
    因此逐字节复制,不能切片。

    解压失败(截断、偏移越界、长度不符)一律抛 :class:`LocalStoreError`,
    由调用方跳过该 block / 该文件。
    """
    total = len(data)
    pos = 0
    unpacked_len, pos = _read_varint(data, pos, "snappy 前导长度")
    out = bytearray()
    while pos < total:
        tag = data[pos]
        pos += 1
        kind = tag & 0x03

        if kind == 0:                                   # literal
            length = tag >> 2
            if length < 60:
                length += 1
            else:
                extra = length - 59                     # 60→1 字节 … 63→4 字节
                if pos + extra > total:
                    raise LocalStoreError("snappy literal 长度字段被截断")
                length = int.from_bytes(data[pos:pos + extra], "little") + 1
                pos += extra
            if pos + length > total:
                raise LocalStoreError("snappy literal 内容被截断")
            out += data[pos:pos + length]
            pos += length
            if len(out) > unpacked_len:      # 早退:损坏流不许把内存撑爆
                raise LocalStoreError("snappy 输出超过声明长度")
            continue

        if kind == 1:                                   # copy,1 字节偏移
            if pos + 1 > total:
                raise LocalStoreError("snappy copy1 偏移被截断")
            length = ((tag >> 2) & 0x07) + 4
            offset = ((tag >> 5) << 8) | data[pos]
            pos += 1
        elif kind == 2:                                 # copy,2 字节偏移
            if pos + 2 > total:
                raise LocalStoreError("snappy copy2 偏移被截断")
            length = (tag >> 2) + 1
            offset = int.from_bytes(data[pos:pos + 2], "little")
            pos += 2
        else:                                           # copy,4 字节偏移
            if pos + 4 > total:
                raise LocalStoreError("snappy copy4 偏移被截断")
            length = (tag >> 2) + 1
            offset = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4

        if offset == 0 or offset > len(out):
            raise LocalStoreError("snappy copy 偏移越界")
        start = len(out) - offset
        for index in range(length):                     # 重叠复制,必须逐字节
            out.append(out[start + index])
        if len(out) > unpacked_len:
            raise LocalStoreError("snappy 输出超过声明长度")

    if len(out) != unpacked_len:
        raise LocalStoreError(
            f"snappy 解压长度不符(声明 {unpacked_len},实得 {len(out)})")
    return bytes(out)


# --------------------------------------------------------------------------- #
# LevelDB 原语
# --------------------------------------------------------------------------- #
def _parse_block(block: bytes) -> List[Tuple[bytes, bytes]]:
    """解析一个 LevelDB block body → ``[(key, value)]``。

    条目格式:``shared_len(varint) unshared_len(varint) value_len(varint)
    key_delta(unshared_len 字节) value(value_len 字节)``,末尾是 restart 数组。
    """
    if len(block) < 4:
        raise LocalStoreError("block 过短")
    restarts = struct.unpack_from("<I", block, len(block) - 4)[0]
    if restarts == 0 or restarts > len(block) // 4:
        raise LocalStoreError(f"block restart 数组非法(count={restarts})")
    data_end = len(block) - 4 - restarts * 4
    if data_end < 0:
        raise LocalStoreError("block restart 数组越界")

    entries: List[Tuple[bytes, bytes]] = []
    pos = 0
    key = b""
    while pos < data_end:
        shared, pos = _read_varint(block, pos, "block.shared")
        unshared, pos = _read_varint(block, pos, "block.unshared")
        value_len, pos = _read_varint(block, pos, "block.value_len")
        if shared > len(key) or pos + unshared + value_len > data_end:
            raise LocalStoreError("block 条目损坏")
        key = key[:shared] + block[pos:pos + unshared]
        pos += unshared
        entries.append((key, block[pos:pos + value_len]))
        pos += value_len
    return entries


def _decode_block(raw: bytes) -> Tuple[bytes, int]:
    """``raw`` = block body + 5 字节 trailer(压缩类型 1B + 掩码 crc32c 4B)。

    返回 ``(body, compression)``,``compression`` 为 0(未压缩)或 1(Snappy)。

    ⚠ **限制**:trailer 里的 CRC32C **不校验**(Python stdlib 没有现成的
    CRC32C 实现,也不引入第三方依赖)。损坏的 block 只能靠后续结构解析
    (restart 数组 / 条目长度)间接发现 —— 调用方(``read_table``)会在
    warnings 里显式标注这一点。
    """
    if len(raw) < 5:
        raise LocalStoreError("block 过短(无 trailer)")
    compression = raw[-5]
    body = raw[:-5]
    if compression == 0:
        return body, compression
    if compression == 1:
        return snappy_decompress(body), compression
    raise LocalStoreError(f"未知 block 压缩类型 {compression}")


def _grab_block_entries(blob: bytes, offset: int,
                        size: int) -> Tuple[List[Tuple[bytes, bytes]], bool]:
    """按 BlockHandle 取出一个 block 并解析成 ``(entries, compressed)``。

    ``compressed`` = 该 block 是否用了 Snappy(供调用方统计,不影响解析)。

    真机实测该 build 的 ``size`` **不含** trailer,故先按 ``size+5`` 读;
    失败再退回 ``size+0``(兼容 size 含 trailer 的 build)。两种解释都必须
    通过 ``_parse_block`` 校验才被采纳 —— 宁可失败,不可返回错位数据。
    """
    errors: List[str] = []
    for extra in (5, 0):
        end = offset + size + extra
        if offset < 0 or end > len(blob):
            continue
        try:
            body, compression = _decode_block(blob[offset:end])
            return _parse_block(body), compression != 0
        except LocalStoreError as exc:
            errors.append(f"size+{extra}:{exc}")
    raise LocalStoreError("block 解码失败(" + "; ".join(errors) + ")")


def _read_file(path: Path) -> bytes:
    """只读整个文件;被占用 / 无权限 / 超限一律抛 :class:`LocalStoreError`。"""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise LocalStoreError(f"{path.name} 无法访问({exc.__class__.__name__})") from exc
    if size > MAX_FILE_BYTES:
        raise LocalStoreError(f"{path.name} 超过 {MAX_FILE_BYTES} 字节上限,已跳过")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise LocalStoreError(
            f"{path.name} 读取失败({exc.__class__.__name__}: {exc})") from exc


def read_table(path: Path) -> Tuple[List[RawEntry], List[str]]:
    """解析一个 ``.ldb``(SSTable)→ ``(entries, warnings)``。

    单条记录形如 ``(user_key, sequence, value_type, value)``。
    index block 读不出来才抛异常;单个 data block 坏掉只记 warning 并继续。

    ⚠ **已知限制**:block trailer 里的 CRC32C **不校验**(见 :func:`_decode_block`)。
    只要解析到过 block,就会在 warnings 里带**一条** "未校验 block CRC32C"
    (每个文件一次,不刷屏)。
    """
    blob = _read_file(path)
    if len(blob) < FOOTER_SIZE:
        raise LocalStoreError("文件过短,无 footer")
    if struct.unpack_from("<Q", blob, len(blob) - 8)[0] != FOOTER_MAGIC:
        raise LocalStoreError("footer 魔数不符(不是 LevelDB table)")

    pos = len(blob) - FOOTER_SIZE
    _, pos = _read_varint(blob, pos, "footer.metaindex.offset")
    _, pos = _read_varint(blob, pos, "footer.metaindex.size")
    index_offset, pos = _read_varint(blob, pos, "footer.index.offset")
    index_size, pos = _read_varint(blob, pos, "footer.index.size")

    entries: List[RawEntry] = []
    warnings: List[str] = []
    blocks_seen = 0
    index_entries, _index_compressed = _grab_block_entries(blob, index_offset,
                                                           index_size)
    blocks_seen += 1
    for _, handle in index_entries:
        point = 0
        data_offset, point = _read_varint(handle, point, "blockhandle.offset")
        data_size, point = _read_varint(handle, point, "blockhandle.size")
        try:
            parsed, _compressed = _grab_block_entries(blob, data_offset,
                                                      data_size)
        except LocalStoreError as exc:
            warnings.append(f"data block @{data_offset} 跳过:{exc}")
            continue
        blocks_seen += 1
        for internal_key, value in parsed:
            if len(internal_key) < 8:
                warnings.append(f"data block @{data_offset} 有超短 internal key")
                continue
            tag = struct.unpack_from("<Q", internal_key, len(internal_key) - 8)[0]
            entries.append((internal_key[:-8], tag >> 8, tag & 0xFF, value))
    if blocks_seen:
        # 每个文件只标注一次:这是实现层面的限制,不是"这个文件有问题"
        warnings.append(
            "未校验 block CRC32C(Python stdlib 无 CRC32C 实现,"
            "不引入第三方依赖;Snappy block 的校验和未验证,"
            "损坏数据只靠结构解析间接发现)")
    return entries, warnings


def _parse_write_batch(record: bytes) -> List[RawEntry]:
    """解析 leveldb write_batch 正文 → 条目列表。

    布局:``sequence(8B 小端) + count(4B 小端)``,随后 ``count`` 条
    ``kType(1B) key(varint 长度 + 字节) [value(varint 长度 + 字节)]``。
    """
    if len(record) < 12:
        raise LocalStoreError("write_batch 过短")
    seq0, count = struct.unpack_from("<QI", record, 0)
    out: List[RawEntry] = []
    pos = 12
    for index in range(count):
        if pos >= len(record):
            raise LocalStoreError(f"write_batch 第 {index} 条越界")
        value_type = record[pos]
        pos += 1
        key_len, pos = _read_varint(record, pos, "batch.key_len")
        if pos + key_len > len(record):
            raise LocalStoreError(f"write_batch 第 {index} 条 key 越界")
        key = record[pos:pos + key_len]
        pos += key_len
        if value_type == 1:
            value_len, pos = _read_varint(record, pos, "batch.value_len")
            if pos + value_len > len(record):
                raise LocalStoreError(f"write_batch 第 {index} 条 value 越界")
            out.append((key, seq0 + index, 1, record[pos:pos + value_len]))
            pos += value_len
        else:
            out.append((key, seq0 + index, 0, b""))   # 删除标记
    return out


def read_log(path: Path) -> Tuple[List[RawEntry], List[str]]:
    """解析一个 ``.log``(WAL)→ ``(entries, warnings)``。

    记录 = ``crc(4) length(2) type(1) data(length)``;
    type 1=FULL / 2=FIRST / 3=LAST / 4=MIDDLE(跨块拼接,按 32768 分块)。
    截断的尾巴只记 warning,不抛异常 —— 文件可能正在被客户端写入。
    """
    blob = _read_file(path)
    out: List[RawEntry] = []
    warnings: List[str] = []
    pos = 0
    pending = b""
    total = len(blob)

    while pos + 7 <= total:
        block_left = LOG_BLOCK_SIZE - (pos % LOG_BLOCK_SIZE)
        if block_left < 7:
            pos += block_left                      # 块尾 padding
            continue
        _crc, length, record_type = struct.unpack_from("<IHB", blob, pos)
        header_at = pos
        pos += 7
        if length == 0 and record_type == 0:
            continue                               # 预分配 / 零填充
        if pos + length > total:
            warnings.append(f"WAL 记录被截断(偏移 {header_at})")
            break
        fragment = blob[pos:pos + length]
        pos += length

        if record_type == 1:                       # FULL
            record = fragment
        elif record_type == 2:                     # FIRST
            pending = fragment
            continue
        elif record_type == 3:                     # MIDDLE
            if not pending:
                warnings.append("WAL MIDDLE 片段缺少 FIRST,已跳过")
                continue
            pending += fragment
            continue
        elif record_type == 4:                     # LAST
            record = pending + fragment
            pending = b""
        else:
            warnings.append(f"WAL 未知记录类型 {record_type}")
            continue

        try:
            out.extend(_parse_write_batch(record))
        except LocalStoreError as exc:
            warnings.append(f"WAL write_batch 解析失败:{exc}")
    return out, warnings


# --------------------------------------------------------------------------- #
# 值解码 / 键拆分
# --------------------------------------------------------------------------- #
def decode_value(value: bytes) -> Tuple[str, str]:
    """解码 localStorage 值 → ``(encoding, text)``。

    首字节 ``\\x00`` = UTF-16LE 正文,``\\x01`` = Latin-1 正文;
    其余(异常形态)按 UTF-8 试一次。解不出来抛 :class:`LocalStoreError`。
    """
    if not value:
        raise LocalStoreError("值为空")
    prefix, body = value[:1], value[1:]
    if prefix == b"\x00":
        try:
            return "utf-16le", body.decode("utf-16-le")
        except UnicodeDecodeError as exc:
            raise LocalStoreError("UTF-16LE 值解码失败") from exc
    if prefix == b"\x01":
        return "latin-1", body.decode("latin-1")
    try:
        return "utf-8", value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LocalStoreError("UTF-8 值解码失败") from exc


def _split_localstorage_key(user_key: bytes) -> Optional[Tuple[str, str]]:
    """把 ``_<origin>\\x00\\x01<key>`` 拆成 ``(origin, key)``。

    返回 ``None`` 表示不是 localStorage 记录:``\\x02`` 是 sessionStorage,
    不以 ``_`` 开头的是 ``META:<origin>`` 之类的元数据 —— 都忽略。
    """
    if not user_key.startswith(b"_"):
        return None
    nul = user_key.find(b"\x00")
    if nul < 0:
        return None
    if user_key[nul + 1:nul + 2] != b"\x01":
        return None
    origin = user_key[1:nul].decode("utf-8", "replace")
    key = user_key[nul + 2:].decode("utf-8", "replace")
    return origin, key


# --------------------------------------------------------------------------- #
# 扫描 / 合并
# --------------------------------------------------------------------------- #
def read_all(directory: Optional[Any] = None, *,
             userdata: Optional[Any] = None) -> StoreSnapshot:
    """扫描 leveldb 目录下全部 ``*.ldb`` / ``*.log`` 并合并。

    合并规则:同一个 user_key 取 internal sequence **最大**的那条(删除标记
    value_type=0 也参与比较,因此能正确遮蔽更早的值)。

    任何单文件失败都不会影响其它文件;文件不存在时返回 ``exists=False``
    的空快照,不抛异常。
    """
    path = Path(directory) if directory else leveldb_dir(userdata)
    snapshot = StoreSnapshot(directory=str(path), exists=path.is_dir())
    if not snapshot.exists:
        return snapshot

    try:
        candidates = sorted(
            item for item in path.iterdir()
            if item.is_file() and item.suffix.lower() in (".ldb", ".log"))
    except OSError as exc:
        snapshot.errors.append(f"目录列举失败({exc.__class__.__name__})")
        return snapshot

    best: Dict[bytes, RawEntry] = {}
    for candidate in candidates:
        kind = candidate.suffix.lower().lstrip(".")
        try:
            entries, warnings = (read_table(candidate) if kind == "ldb"
                                 else read_log(candidate))
        except LocalStoreError as exc:
            snapshot.files.append(FileReport(name=candidate.name, kind=kind,
                                             entries=0, error=str(exc)))
            snapshot.errors.append(f"{candidate.name}: {exc}")
            continue
        report = FileReport(name=candidate.name, kind=kind,
                            entries=len(entries), warnings=warnings[:5])
        if warnings:
            snapshot.warnings.extend(f"{candidate.name}: {w}"
                                     for w in warnings[:5])
        snapshot.files.append(report)
        for user_key, seq, value_type, value in entries:
            current = best.get(user_key)
            if current is None or seq > current[1]:
                best[user_key] = (user_key, seq, value_type, value)

    for user_key, seq, value_type, value in best.values():
        if value_type != 1:                        # 删除标记
            continue
        split = _split_localstorage_key(user_key)
        if split is None:
            continue
        origin, key = split
        try:
            encoding, text = decode_value(value)
        except LocalStoreError:
            continue
        snapshot.records.append(LocalRecord(origin=origin, key=key, text=text,
                                            seq=seq, value_bytes=len(value),
                                            encoding=encoding))
    snapshot.records.sort(key=lambda r: (r.origin, r.key, r.seq))
    return snapshot


# --------------------------------------------------------------------------- #
# 登录态挑选(与 cdp.py 同一套自适应策略)
# --------------------------------------------------------------------------- #
def _origin_matches(origin: str, wanted: Optional[str]) -> bool:
    """origin 过滤:精确优先,其次大小写不敏感的子串;空值 = 不过滤。"""
    if not wanted:
        return True
    if origin == wanted:
        return True
    return wanted.lower() in origin.lower()


def _origin_rank(origin: str) -> int:
    """挑选用排序:非 ``file://`` 的 origin 优先。

    登录态真正写在网络 origin(``https://oa.<domain>:4433`` /
    ``http://oam.<domain>:8999``)上;``file://`` 那份实测**不含 token**,
    但万一是残留的旧值,也不该压过网络 origin。
    """
    return 1 if origin.startswith("file:") else 0


def _payload_of(record: LocalRecord) -> Optional[Dict[str, Any]]:
    """把记录正文解析成 JSON dict;不是 JSON / 不是对象则返回 None。"""
    text = record.text.strip()
    if text[:1] not in ("{", "["):
        return None
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _extract_access_token(payload: Dict[str, Any]) -> str:
    """从解析后的 JSON 里找 access_token。

    两级容器策略(**与 ``cdp._extract_access_token`` 同构**):
      * ``{"currentUser": {"access_token": ...}}``
      * ``{"access_token": ...}``(7.1.4 实测是平铺的)
    """
    containers: List[Any] = [payload]
    nested = payload.get("currentUser")
    if isinstance(nested, dict):
        containers.append(nested)
    for container in containers:
        if not isinstance(container, dict):
            continue
        for name in ACCESS_TOKEN_FIELDS:
            value = container.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _candidate_key_order(snapshot: StoreSnapshot,
                         keys: Optional[Sequence[str]]) -> List[str]:
    """候选键名顺序:显式 keys → 已知键名 → 任何 ``*-current-info`` 键。"""
    order: List[str] = []
    for key in list(keys or []) + list(KNOWN_TOKEN_KEYS):
        if key and str(key) not in order:
            order.append(str(key))
    for record in snapshot.records:                # 兜住将来的新前缀
        if record.key.endswith(TOKEN_KEY_SUFFIX) and record.key not in order:
            order.append(record.key)
    return order


def _pick_record(snapshot: StoreSnapshot,
                 keys: Optional[Sequence[str]] = None,
                 origin: Optional[str] = None) -> Optional[LocalRecord]:
    """挑出"能取到 access_token"的那份登录态记录。

    **不能只看第一个同名键**:本机 ``file://``、``https://oa.<domain>:4433``、
    ``http://oam.<domain>:8999`` 都可能存在同名键,而 ``file://`` 那份不含
    token —— 必须逐个 origin 试,选中真正能取到 token 的那份。

    顺序:
      1. 按候选键名逐个试(同一键名跨 origin,非 file:// 优先);
      2. 兜底:不看键名,只挑"值里能解析出 access_token"的记录。
    """
    records = [r for r in snapshot.records if _origin_matches(r.origin, origin)]
    records.sort(key=lambda r: (_origin_rank(r.origin), r.origin, r.key, r.seq))

    for key in _candidate_key_order(snapshot, keys):
        for record in records:
            if record.key != key:
                continue
            payload = _payload_of(record)
            if payload and _extract_access_token(payload):
                return record

    for record in records:
        payload = _payload_of(record)
        if payload and _extract_access_token(payload):
            return record
    return None


def _first_text(*containers: Any, name: str) -> str:
    """从若干容器里取第一个非空字符串字段。"""
    for container in containers:
        if not isinstance(container, dict):
            continue
        value = container.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_scalar(*containers: Any, name: str) -> Optional[Any]:
    """从若干容器里取第一个标量字段(int / float / 非空字符串)。"""
    for container in containers:
        if not isinstance(container, dict):
            continue
        value = container.get(name)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _sanitize(payload: Dict[str, Any]) -> Dict[str, Any]:
    """按**白名单**裁剪登录态,只保留登录必需字段。

    安全红线:客户端 localStorage 里还明文存着 ``currentUser.password`` 等
    敏感字段 —— 这里**绝不**把它们带出去(返回的 dict 只可能包含
    ``ALLOWED_TOP_FIELDS`` 与 ``ALLOWED_USER_FIELDS`` 里的字段)。

    形态统一归一到 ``{currentBaseUrl, expire, currentUser: {...}}``:
    7.1.4 的平铺形态也归一到 ``currentUser`` 下,与 ``session._take`` 的
    两级取值逻辑兼容。
    """
    nested = payload.get("currentUser")
    nested = nested if isinstance(nested, dict) else {}

    result: Dict[str, Any] = {}
    for name in ALLOWED_TOP_FIELDS:
        value = (_first_text(payload, nested, name=name)
                 if name == "currentBaseUrl"
                 else _first_scalar(payload, nested, name=name))
        if value is None or value == "":
            continue
        result[name] = value

    user: Dict[str, Any] = {}
    for name in ALLOWED_USER_FIELDS:
        value = (_extract_access_token(payload) if name == "access_token"
                 else _first_text(payload, nested, name=name))
        if value is None or value == "":
            continue
        user[name] = value
    if user:
        result["currentUser"] = user
    return result


def _sensitive_fields(payload: Dict[str, Any]) -> List[str]:
    """列出 payload 里出现的已知敏感字段**名** —— 只报名字,**绝不取值**。"""
    found: List[str] = []
    for container in (payload, payload.get("currentUser")):
        if not isinstance(container, dict):
            continue
        for name in SENSITIVE_FIELD_NAMES:
            if name in container and name not in found:
                found.append(name)
    return found


def _no_token_error(snapshot: Optional[StoreSnapshot], attempts: int,
                    pause: float) -> LocalStoreError:
    """重试耗尽后区分三种失败:解析失败 / 确实未登录 / 可能尚未落盘。"""
    if snapshot is None:
        return LocalStoreUnavailableError("未能读取 E-Mobile 本地 LevelDB")

    parsed_files = [f for f in snapshot.files if not f.error]
    if snapshot.files and not parsed_files:
        return LocalStoreUnavailableError(
            "E-Mobile 本地 LevelDB 解析失败(所有文件都读不出记录):\n  "
            + "\n  ".join(snapshot.errors[:5]))

    current_info_keys = sorted({r.key for r in snapshot.records
                                if r.key.endswith(TOKEN_KEY_SUFFIX)})
    if current_info_keys:
        # 有登录态键但取不到 token → 客户端确实处于未登录 / 已退出状态
        return NotLoggedInError(
            f"E-Mobile 本地 LevelDB 里没有可用的登录态(已重试 {attempts} 次)。\n"
            f"  数据目录:{snapshot.directory}\n"
            f"  已解析记录:{len(snapshot.records)} 条;"
            f"存在的登录态键:{', '.join(current_info_keys)}\n"
            "  这些键里都没有 access_token,客户端很可能处于未登录状态:"
            "请先在 E-Mobile 里登录,再执行 `emcli token refresh`"
            "(或用 `emcli token set <token>` 手动写入)。")

    return PersistencePendingError(
        f"E-Mobile 本地 LevelDB 里还没有任何登录态记录"
        f"(已重试 {attempts} 次 × {pause:g}s)。\n"
        f"  数据目录:{snapshot.directory}\n"
        f"  已解析记录:{len(snapshot.records)} 条,"
        f"未发现任何 *-{TOKEN_KEY_SUFFIX} 键。\n"
        "  两种可能:(a) 客户端还没把内存里的 localStorage 落盘"
        "(刚登录完请稍后重试);(b) 客户端从未登录过。\n"
        "  也可临时以 --remote-debugging-port=9333 启动 E-Mobile 后用 CDP 读取。")


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
def read_current_info(origin: Optional[str] = None,
                      keys: Optional[Sequence[str]] = None, *,
                      directory: Optional[Any] = None,
                      userdata: Optional[Any] = None,
                      retries: int = 3, delay: float = 0.2,
                      with_meta: bool = False) -> Dict[str, Any]:
    """从磁盘上的 localStorage LevelDB 读登录态(不依赖 CDP)。

    参数:
      origin:   只在该 origin 里找(精确优先,其次大小写不敏感子串);省略 = 全部。
      keys:     该客户端版本的候选键名(通常来自 ``profile.localstorage_token_keys``);
                省略也会自动补上已知键名与 ``*-current-info`` 后缀兜底。
      directory / userdata: 显式指定 leveldb 目录 / userData 根(测试用)。
      retries / delay: 总尝试次数(>=1)与每次之间的间隔秒数。Chromium 的
                localStorage 是内存缓存 + 延迟批量提交,"刚登录完立刻读盘"可能
                还没落盘,所以提供重试。
      with_meta: 为 True 时额外带上 ``_origin`` / ``_key`` / ``_value_bytes``
                (只含 origin、键名、字节长度,供诊断;默认关闭)。

    返回**白名单裁剪后**的登录态 dict(``currentBaseUrl`` / ``expire`` /
    ``currentUser.{access_token, base_user_id, base_user_name}``)—— 与
    ``cdp.read_current_info`` 同形,但**绝不**包含 password 等敏感字段。

    失败抛 :class:`LocalStoreError` 子类,三种情况可区分:
      * :class:`LocalStoreUnavailableError` —— 目录不存在 / 全部解析失败;
      * :class:`NotLoggedInError` —— 磁盘里确实没有可用登录态;
      * :class:`PersistencePendingError` —— 可能尚未落盘(可稍后重试)。
    """
    attempts = max(1, int(retries))
    pause = max(0.0, float(delay))
    snapshot: Optional[StoreSnapshot] = None

    for attempt in range(attempts):
        snapshot = read_all(directory, userdata=userdata)
        if not snapshot.exists:
            raise LocalStoreUnavailableError(
                f"E-Mobile 数据目录不存在:{snapshot.directory}\n"
                "  请确认已安装并至少启动过一次 E-Mobile;"
                "或用 EMCLI_EMOBILE_USERDATA 指定 userData 根目录。")
        record = _pick_record(snapshot, keys=keys, origin=origin)
        if record is not None:
            data = _sanitize(_payload_of(record) or {})
            if with_meta:
                data["_origin"] = record.origin
                data["_key"] = record.key
                data["_value_bytes"] = record.value_bytes
            return data
        if attempt + 1 < attempts:
            time.sleep(pause)

    raise _no_token_error(snapshot, attempts, pause)


def try_read_current_info(origin: Optional[str] = None,
                          keys: Optional[Sequence[str]] = None, *,
                          directory: Optional[Any] = None,
                          userdata: Optional[Any] = None,
                          retries: int = 1,
                          delay: float = 0.0) -> Optional[Dict[str, Any]]:
    """``read_current_info`` 的"不抛异常"版本:读不到就回 ``None``。"""
    try:
        return read_current_info(origin=origin, keys=keys, directory=directory,
                                 userdata=userdata, retries=retries,
                                 delay=delay)
    except LocalStoreError:
        return None


def diagnose(directory: Optional[Any] = None, *,
             userdata: Optional[Any] = None) -> Dict[str, Any]:
    """只读诊断:数据目录、各文件记录数、origin 与键名、是否找到 token。

    **只报 origin / 键名 / 长度 / 布尔,绝不返回值**。供 `emcli compat`
    之类的自检入口接入。此函数不抛异常。
    """
    path = Path(directory) if directory else leveldb_dir(userdata)
    info: Dict[str, Any] = {
        "userdata": str(userdata_dir(userdata)),
        "directory": str(path),
        "exists": path.is_dir(),
        "files": [],
        "records": 0,
        "origins": [],
        "keys": [],
        "current_info": [],
        "token_found": False,
        "token_len": 0,
        "origin": None,
        "key": None,
        "errors": [],
        "warnings": [],
    }
    try:
        snapshot = read_all(path)
    except Exception as exc:  # noqa: BLE001 — 诊断入口绝不向上抛
        info["errors"].append(f"{exc.__class__.__name__}: {exc}")
        return info

    info["exists"] = snapshot.exists
    info["files"] = [
        {"name": f.name, "kind": f.kind, "entries": f.entries,
         "error": f.error or None, "warnings": len(f.warnings)}
        for f in snapshot.files
    ]
    info["records"] = len(snapshot.records)
    info["origins"] = snapshot.origins
    info["keys"] = snapshot.keys
    info["errors"] = list(snapshot.errors)
    info["warnings"] = list(snapshot.warnings)

    for record in snapshot.records:
        if not record.key.endswith(TOKEN_KEY_SUFFIX):
            continue
        payload = _payload_of(record) or {}
        token = _extract_access_token(payload)
        info["current_info"].append({
            "origin": record.origin,
            "key": record.key,
            "seq": record.seq,
            "value_bytes": record.value_bytes,
            "encoding": record.encoding,
            "json": bool(payload),
            "has_token": bool(token),
            "token_len": len(token),
            "sensitive_fields": _sensitive_fields(payload),
        })

    picked = _pick_record(snapshot)
    if picked is not None:
        token = _extract_access_token(_payload_of(picked) or {})
        info["token_found"] = bool(token)
        info["token_len"] = len(token)
        info["origin"] = picked.origin
        info["key"] = picked.key
    return info
