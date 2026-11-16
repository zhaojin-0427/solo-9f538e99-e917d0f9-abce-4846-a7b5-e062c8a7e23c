# -*- coding: utf-8 -*-
"""发酵罐 NDJSON 温度日志采集核心。

设计要点
========
1. 设备把 ``active.ndjson`` 重命名为 ``active.ndjson.1`` 后新建同名文件，
   目录最多保留这两份，归档不删除、不覆盖。
2. 采集进度按 **inode + 已观测前缀哈希** 绑定（而不是只按 inode 或路径）：
   - 轮转后旧文件换了路径但 inode 不变，前缀哈希校验一致，仍从原偏移续读；
   - inode 在一轮成功采集时缺席（文件已消失 / 被移走），该 inode 的进度立即
     作废；号码被文件系统复用到新文件时，必然从头重新校验，不会沿用旧偏移；
   - 即使 inode 在两次采集之间被删除又复用（没有观察到缺席的空轮次），
     也会先核对已观测前缀（含半行）的 SHA-256；不一致即从文件头重读，
     并逐行与库内记录比对。
3. 每次采集只处理“上次成功后新增的字节”，且只消费到最后一个换行符；
   换行符之后的半行保留在文件里，偏移不前移，下一轮再读。
4. 一轮采集 = 一个事务：任何一条完整记录非法（JSON 非法 / 字段不合法 /
   序号不连续递增 / 同序号内容冲突），整轮拒绝，已入库记录与采集进度都不变。
   无法确认文件连续性（前缀被改写、截断、重读后序号断档等）同样整轮拒绝，
   进度不推进，可修复文件后重试；库内已有的序号按原文比对，绝不重复入库。
   采集期间文件被并发替换/截断时状态记为 retry，同样不推进、可重试。
5. 重复点击 / 服务重启：进度已持久化在 SQLite，没有新字节时即为空操作，
   不重记、不漏记。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

ACTIVE_NAME = "active.ndjson"
ROTATED_NAME = "active.ndjson.1"
# 固定的采集顺序：先旧文件后当前文件，便于排查；最终仍按序号归位校验。
LOG_FILES = (ROTATED_NAME, ACTIVE_NAME)


class RejectRound(Exception):
    """整轮采集被拒绝的业务异常，message 为可直接展示的中文原因。"""


class RetryRound(RejectRound):
    """文件连续性暂时无法确认（采集期间被并发改写等），整轮不推进、可重试。"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #
def get_db(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> None:
    with get_db(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                seq          INTEGER NOT NULL UNIQUE,
                ts           TEXT    NOT NULL,
                temp         REAL    NOT NULL,
                raw          TEXT    NOT NULL,
                source_path  TEXT    NOT NULL,
                source_inode INTEGER NOT NULL,
                collected_at TEXT    NOT NULL
            );

            -- 采集进度按 inode 记录，path 仅作展示（轮转后路径会变）。
            -- offset      = 已确认消费到的位置（最后一个换行之后）；
            -- anchor_offset = 上次观测到的 EOF（含半行）；
            -- prefix_hash = [0, anchor_offset) 的 SHA-256，用于确认文件连续。
            -- prefix_hash / anchor_offset 为 NULL 表示旧版本进度，
            -- 无法确认连续性，须从文件头重新校验。
            CREATE TABLE IF NOT EXISTS file_state (
                inode         INTEGER PRIMARY KEY,
                path          TEXT    NOT NULL,
                offset        INTEGER NOT NULL,
                file_size     INTEGER NOT NULL,
                anchor_offset INTEGER,
                prefix_hash   TEXT,
                updated_at    TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS collect_runs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at       TEXT    NOT NULL,
                finished_at      TEXT    NOT NULL,
                status           TEXT    NOT NULL,           -- ok / rejected / retry / error
                files_scanned    INTEGER NOT NULL DEFAULT 0,
                lines_seen       INTEGER NOT NULL DEFAULT 0, -- 本轮读到的完整行数
                records_inserted INTEGER NOT NULL DEFAULT 0,
                duplicates       INTEGER NOT NULL DEFAULT 0, -- 已存在且内容一致
                half_line_bytes  INTEGER NOT NULL DEFAULT 0, -- 留在文件里的半行字节
                detail           TEXT    NOT NULL DEFAULT '',
                error            TEXT    NOT NULL DEFAULT ''
            );
            """
        )
        # 兼容旧库：为已有 file_state 补上连续性指纹列（旧进度按“不可确认”处理）。
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(file_state)")}
        if "prefix_hash" not in columns:
            conn.execute("ALTER TABLE file_state ADD COLUMN prefix_hash TEXT")
        if "anchor_offset" not in columns:
            conn.execute("ALTER TABLE file_state ADD COLUMN anchor_offset INTEGER")


def reset_all(db_path: str, data_dir: str) -> None:
    """演示用：清空数据库与挂载目录中的两份日志。"""
    with get_db(db_path) as conn:
        conn.executescript(
            "DELETE FROM records; DELETE FROM file_state; DELETE FROM collect_runs;"
        )
    for name in LOG_FILES:
        p = os.path.join(data_dir, name)
        if os.path.exists(p):
            os.remove(p)


# --------------------------------------------------------------------------- #
# 单行校验
# --------------------------------------------------------------------------- #
def _parse_utc_ts(value) -> Optional[datetime]:
    """要求带时区且 UTC 偏移为 0 的 ISO-8601 字符串（如 2026-11-15T08:30:00Z）。"""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        return None
    return dt


def validate_line(raw_line: str) -> Tuple[int, str, float]:
    """校验一条完整记录，返回 (seq, ts 原文, temp)；非法则抛 RejectRound。"""
    try:
        obj = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        raise RejectRound(f"非法 JSON 行：{exc.msg}；内容：{raw_line[:120]!r}")
    if not isinstance(obj, dict):
        raise RejectRound(f"记录必须是 JSON 对象：{raw_line[:120]!r}")

    seq = obj.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise RejectRound(f"序号 seq 必须是整数：{raw_line[:120]!r}")

    ts = obj.get("ts")
    if _parse_utc_ts(ts) is None:
        raise RejectRound(f"时刻 ts 必须是 UTC 的 ISO-8601 字符串（结尾 Z）：{raw_line[:120]!r}")

    temp = obj.get("temp")
    if isinstance(temp, bool) or not isinstance(temp, (int, float)) or not math.isfinite(temp):
        raise RejectRound(f"温度 temp 必须是有限数值：{raw_line[:120]!r}")

    return seq, ts, float(temp)


# --------------------------------------------------------------------------- #
# 文件扫描与增量读取
# --------------------------------------------------------------------------- #
@dataclass
class FileChunk:
    path: str
    name: str
    inode: int
    size: int
    final_size: int            # 读取完成后再次观测的大小（检测并发追加/改写）
    start_offset: int          # 本轮读取前的偏移（0 表示从头校验）
    complete_text: str         # 本轮新增且以换行结束的文本
    new_offset: int            # 本轮成功后应持久化的偏移
    anchor_offset: int         # 本轮观测到的 EOF，指纹覆盖 [0, anchor_offset)
    prefix_hash: str           # [0, anchor_offset) 的 SHA-256
    half_line_bytes: int       # 未以换行结束、留给下轮的字节数
    resumed: bool              # 是否沿用了持久化偏移（False = 从头校验）
    complete_lines: List[str] = field(default_factory=list)


def _scan_files(data_dir: str) -> List[dict]:
    found = []
    for name in LOG_FILES:
        path = os.path.join(data_dir, name)
        try:
            st = os.stat(path)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        found.append({"path": path, "name": name, "inode": st.st_ino, "size": st.st_size})
    return found


def _read_file_chunk(data_dir: str, info: dict, states: Dict[int, sqlite3.Row]) -> FileChunk:
    """按 inode 进度读取某文件的新增字节，只切出完整行。

    连续性判定（任一条件不满足都必须从文件头重新校验）：
      * 该 inode 有持久化进度，且上次观测到的 EOF（anchor_offset）不超过当前文件；
      * 已观测前缀 [0, anchor_offset) 的 SHA-256 与持久化指纹一致。
        inode 在两次采集之间被删除后又被文件系统复用（即使大小恰好相同）、
        旧版本无指纹、文件被重写或截断都会在此失败。
      * 轮转改名不改 inode 也不改内容，指纹一致，因此旧文件仍可续读新增字节。

    确认连续后从 ``offset``（最后一个换行之后）继续；证伪则从文件头重读，
    已入库序号按原文比对，不会重复入库，序号断档则整轮拒绝。
    """
    path, inode = info["path"], info["inode"]
    state = states.get(inode)
    state_offset = int(state["offset"]) if state is not None else 0
    anchor = int(state["anchor_offset"]) if state is not None and state["anchor_offset"] is not None else None
    stored_hash = state["prefix_hash"] if state is not None else None

    try:
        fh = open(path, "rb")
    except FileNotFoundError:
        # 扫描后被轮转/删除：本轮不推进，下一轮重新扫描即可。
        raise RetryRound(f"{info['name']} 在扫描后消失，本轮不推进，请重试。")
    with fh:
        # 读取期间若文件被轮转/替换，句柄仍指向打开时的 inode；fstat 比对可发现
        # “扫描到打开之间文件被换掉”，避免把新文件的数据安到旧 inode 上。
        opened = os.fstat(fh.fileno())
        if opened.st_ino != inode:
            raise RetryRound(f"{info['name']} 在扫描后被替换，本轮不推进，请重试。")
        # 单次读入快照：既用于校验前缀连续性，也用于切出新增完整行。
        data = fh.read()
        after = os.fstat(fh.fileno())
    if after.st_size < len(data) or after.st_size < opened.st_size:
        # 读取期间被截断，快照不可信 -> 本轮不推进、可重试。
        raise RetryRound(f"{info['name']} 在读取期间被截断，本轮不推进，请重试。")
    final_size = after.st_size

    prefix_ok = (
        state is not None
        and stored_hash is not None
        and anchor is not None
        and 0 <= state_offset <= anchor <= len(data)
        and hashlib.sha256(data[:anchor]).hexdigest() == stored_hash
    )
    start = state_offset if prefix_ok else 0
    raw = data[start:]

    last_nl = raw.rfind(b"\n")
    if last_nl < 0:
        complete_bytes, tail = b"", raw
    else:
        complete_bytes, tail = raw[: last_nl + 1], raw[last_nl + 1 :]

    new_offset = start + len(complete_bytes)
    try:
        text = complete_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RejectRound(f"日志包含非 UTF-8 字节，整轮拒绝：{exc}") from exc
    # text 保证以 \n 结尾；空行（含 \n\n）会保留为空串，
    # 后续 validate_line 解析空串时按非法 JSON 处理 -> 整轮拒绝。
    lines = text.split("\n")[:-1] if text else []
    return FileChunk(
        path=path,
        name=info["name"],
        inode=inode,
        size=info["size"],
        final_size=final_size,
        start_offset=start,
        complete_text=text,
        new_offset=new_offset,
        anchor_offset=len(data),
        prefix_hash=hashlib.sha256(data).hexdigest(),
        half_line_bytes=len(tail),
        resumed=prefix_ok,
        complete_lines=lines,
    )


# --------------------------------------------------------------------------- #
# 一轮采集
# --------------------------------------------------------------------------- #
def collect_once(data_dir: str, db_path: str) -> dict:
    started = utc_now_iso()
    result = {
        "status": "error",
        "files": [],
        "lines_seen": 0,
        "records_inserted": 0,
        "duplicates": 0,
        "half_line_bytes": 0,
        "error": "",
        "started_at": started,
        "finished_at": None,
    }
    try:
        init_db(db_path)
        conn = get_db(db_path)
        try:
            states = {row["inode"]: row for row in conn.execute("SELECT * FROM file_state")}
            files = _scan_files(data_dir)
            chunks: List[FileChunk] = [_read_file_chunk(data_dir, info, states) for info in files]
            if len({c.inode for c in chunks}) != len(chunks):
                # 同一 inode 同时出现在两个路径（如硬链接）：无法确定哪份是当前
                # 活动日志，进度归属不唯一 -> 不推进、可重试。
                raise RetryRound(
                    "active.ndjson 与 active.ndjson.1 指向同一个 inode，"
                    "无法确认文件身份，本轮不推进，请重试。"
                )
            result["files"] = [
                {
                    "name": c.name,
                    "path": c.path,
                    "inode": c.inode,
                    "size": c.size,
                    "start_offset": c.start_offset,
                    "new_offset": c.new_offset,
                    "resumed": c.resumed,
                    "complete_lines": len(c.complete_lines),
                    "half_line_bytes": c.half_line_bytes,
                }
                for c in chunks
            ]
            result["half_line_bytes"] = sum(c.half_line_bytes for c in chunks)

            # 1) 逐行解析校验（任何一行非法 -> 整轮拒绝）
            parsed: List[dict] = []
            for chunk in chunks:
                for raw_line in chunk.complete_lines:
                    seq, ts, temp = validate_line(raw_line)
                    parsed.append(
                        {
                            "seq": seq,
                            "ts": ts,
                            "temp": temp,
                            "raw": raw_line,
                            "source_path": chunk.name,
                            "source_inode": chunk.inode,
                        }
                    )
            result["lines_seen"] = len(parsed)

            # 2) 批内同序号冲突检测
            by_seq: Dict[int, dict] = {}
            for rec in parsed:
                old = by_seq.get(rec["seq"])
                if old is not None and old["raw"] != rec["raw"]:
                    raise RejectRound(
                        f"同一序号 {rec['seq']} 出现不同内容，整轮拒绝："
                        f"{old['raw'][:80]!r} != {rec['raw'][:80]!r}"
                    )
                by_seq.setdefault(rec["seq"], rec)

            # 3) 与已入库记录比对 + 序号连续性校验
            existing_rows = conn.execute("SELECT seq, raw FROM records").fetchall()
            existing = {row["seq"]: row["raw"] for row in existing_rows}
            for seq, rec in by_seq.items():
                if seq in existing and existing[seq] != rec["raw"]:
                    raise RejectRound(
                        f"序号 {seq} 已入库但内容不同，整轮拒绝："
                        f"库内={existing[seq][:80]!r} 新行={rec['raw'][:80]!r}"
                    )

            last_row = conn.execute("SELECT MAX(seq) AS m FROM records").fetchone()
            last_seq = last_row["m"] if last_row is not None else None
            new_seqs = sorted(s for s in by_seq if s not in existing)
            if new_seqs:
                # 严格按库内最大序号 +1 递增。从头重读产出的已入库记录按原文
                # 比对后跳过；若重读暴露了“缺失序号”，说明文件不连续，
                # 同样整轮拒绝（进度不推进），修复后可重试，不会静默跳过。
                expected_start = (last_seq + 1) if last_seq is not None else new_seqs[0]
                expected = list(range(expected_start, expected_start + len(new_seqs)))
                if new_seqs != expected:
                    raise RejectRound(
                        "序号未按 +1 连续递增，整轮拒绝："
                        f"库内最大序号={last_seq}，本轮新序号={new_seqs}，期望={expected}"
                    )

            # 4) 全部通过 -> 单事务写记录 + 推进进度
            duplicates = 0
            with conn:  # BEGIN/COMMIT，异常自动 ROLLBACK
                for seq in new_seqs:
                    rec = by_seq[seq]
                    conn.execute(
                        "INSERT INTO records(seq, ts, temp, raw, source_path, source_inode, collected_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            seq,
                            rec["ts"],
                            rec["temp"],
                            rec["raw"],
                            rec["source_path"],
                            rec["source_inode"],
                            started,
                        ),
                    )
                duplicates = len(by_seq) - len(new_seqs)
                for chunk in chunks:
                    conn.execute(
                        "INSERT INTO file_state"
                        "(inode, path, offset, file_size, anchor_offset, prefix_hash, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT(inode) DO UPDATE SET"
                        " path=excluded.path, offset=excluded.offset,"
                        " file_size=excluded.file_size,"
                        " anchor_offset=excluded.anchor_offset,"
                        " prefix_hash=excluded.prefix_hash,"
                        " updated_at=excluded.updated_at",
                        (chunk.inode, chunk.path, chunk.new_offset, chunk.final_size,
                         chunk.anchor_offset, chunk.prefix_hash, started),
                    )
                # 本轮缺席的 inode 立即作废：文件消失（或已不在两份日志中）后，
                # 其 inode 号码可能被新文件复用，绝不能让新文件继承旧偏移。
                # 设备只会保留两份日志，仍存在但改名为 .1 的旧文件在 chunks 中，
                # 因此其进度得以保留并继续追加。
                # 本轮两份日志都不存在时同样清空进度：新文件出现后必须从文件头校验。
                live_inodes = [c.inode for c in chunks]
                if live_inodes:
                    placeholders = ",".join("?" for _ in live_inodes)
                    conn.execute(
                        f"DELETE FROM file_state WHERE inode NOT IN ({placeholders})",
                        live_inodes,
                    )
                else:
                    conn.execute("DELETE FROM file_state")
            result.update(
                status="ok",
                records_inserted=len(new_seqs),
                duplicates=duplicates,
            )
        finally:
            conn.close()
    except RetryRound as exc:
        result["status"] = "retry"
        result["error"] = str(exc)
    except RejectRound as exc:
        result["status"] = "rejected"
        result["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - 任何意外都不能破坏进度
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["finished_at"] = utc_now_iso()
        finished = result["finished_at"]
        with get_db(db_path) as log_conn:
            log_conn.execute(
                "INSERT INTO collect_runs(started_at, finished_at, status, files_scanned,"
                " lines_seen, records_inserted, duplicates, half_line_bytes, detail, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    started,
                    finished,
                    result["status"],
                    len(result["files"]),
                    result["lines_seen"],
                    result["records_inserted"],
                    result["duplicates"],
                    result["half_line_bytes"],
                    json.dumps(result["files"], ensure_ascii=False),
                    result["error"],
                ),
            )
    return result


def list_records(db_path: str, limit: int = 200, offset: int = 0) -> Tuple[List[sqlite3.Row], int]:
    with get_db(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM records").fetchone()["c"]
        rows = conn.execute(
            "SELECT * FROM records ORDER BY seq DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
    return rows, total


def list_runs(db_path: str, limit: int = 50) -> List[sqlite3.Row]:
    with get_db(db_path) as conn:
        return conn.execute(
            "SELECT * FROM collect_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def list_file_states(db_path: str) -> List[sqlite3.Row]:
    with get_db(db_path) as conn:
        return conn.execute("SELECT * FROM file_state ORDER BY path").fetchall()
