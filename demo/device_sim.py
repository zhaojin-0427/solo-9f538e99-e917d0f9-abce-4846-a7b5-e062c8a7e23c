#! /usr/bin/env python3
# -*- coding: utf-8 -*-
"""发酵罐 NDJSON 设备日志模拟器（演示用，仅依赖 Python 标准库）。

设备行为：持续向 active.ndjson 追加 NDJSON 记录；归档时把 active.ndjson
重命名为 active.ndjson.1（不删除、不覆盖），再新建 active.ndjson，目录
最多保留这两份。

子命令
======
  write <N>     追加 N 条完整合法记录（序号自动接续，基于 active 文件现状）
  half          追加半行（不带换行），用于演示“半行留待下次”
  finish-half   给当前半行补全换行（可选择内容，默认补成合法记录的剩余部分）
  rotate        模拟设备归档：active.ndjson -> active.ndjson.1，再新建 active.ndjson
  illegal       追加一条完整但非法的行（默认：温度为 NaN）
  reset         清空目录内两份日志（演示重置，不动数据库；数据库由 Web 端重置）
  auto          一键编排完整演示剧本（配合已启动的 Web 服务，自动调 /collect）

示例
====
  python3 -I demo/device_sim.py --data-dir ./data write 3
  python3 -I demo/device_sim.py --data-dir ./data half
  python3 -I demo/device_sim.py --data-dir ./data rotate
  python3 -I demo/device_sim.py --data-dir ./data auto --base-url http://localhost:8000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

ACTIVE = "active.ndjson"
ROTATED = "active.ndjson.1"
# 半行演示的辅助元数据（不属于设备日志，仅演示脚本使用，采集端会忽略它）
HALF_META = ".half.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _paths(data_dir: str):
    return os.path.join(data_dir, ACTIVE), os.path.join(data_dir, ROTATED)


def _next_seq(data_dir: str) -> int:
    """扫描两份文件，取最大 seq + 1；没有记录则从 1 开始。

    完整行直接解析；文件末尾不带换行的半行用正则提取其中的 seq，
    避免半行已“预约”的序号被后续写入重复使用。
    """
    import re

    active, rotated = _paths(data_dir)
    biggest = 0
    seq_re = re.compile(r'"seq"\s*:\s*(-?\d+)')
    for path in (rotated, active):
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        for line in content.splitlines():  # 完整行
            try:
                obj = json.loads(line)
                if isinstance(obj.get("seq"), int) and not isinstance(obj.get("seq"), bool):
                    biggest = max(biggest, obj["seq"])
            except json.JSONDecodeError:
                pass
        tail = content[content.rfind("\n") + 1 :] if content else ""
        m = seq_re.search(tail)  # 末尾半行里可能已写入 seq
        if m:
            biggest = max(biggest, int(m.group(1)))
    return biggest + 1


def cmd_write(data_dir: str, n: int) -> None:
    os.makedirs(data_dir, exist_ok=True)
    active, _ = _paths(data_dir)
    seq = _next_seq(data_dir)
    with open(active, "a", encoding="utf-8") as fh:
        for i in range(n):
            temp = round(25.0 + ((seq + i) % 7) * 0.42, 3)  # 25.0 ~ 27.52
            rec = {"seq": seq + i, "ts": _now_iso(), "temp": temp}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            time.sleep(0.01)
    print(f"[write] 向 {active} 追加 {n} 条合法记录，序号 {seq}..{seq + n - 1}")


def cmd_half(data_dir: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    active, _ = _paths(data_dir)
    seq = _next_seq(data_dir)
    full = json.dumps({"seq": seq, "ts": _now_iso(), "temp": 26.315}, ensure_ascii=False)
    piece = full[: len(full) // 2]  # 故意截断、不带换行
    with open(active, "a", encoding="utf-8") as fh:
        fh.write(piece)
    # 记录完整内容，finish-half 时可无损补全
    with open(os.path.join(data_dir, HALF_META), "w", encoding="utf-8") as fh:
        json.dump({"full": full, "written_bytes": len(piece.encode("utf-8"))}, fh,
                  ensure_ascii=False)
    print(f"[half] 向 {active} 写入半行（{len(piece.encode('utf-8'))} 字节，无换行）：{piece!r}")
    print("       采集时该半行不会入库；finish-half 补全后下一轮采集即可入库。")


def _find_half_line(data_dir: str):
    """在两份日志中定位末尾不带换行的半行，返回 (路径, 半行文本) 或 None。"""
    active, rotated = _paths(data_dir)
    for path in (rotated, active):
        if not os.path.exists(path):
            continue
        with open(path, "rb") as fh:
            raw = fh.read()
        if raw and not raw.endswith(b"\n"):
            tail = raw[raw.rfind(b"\n") + 1 :].decode("utf-8")
            return path, tail
    return None


def cmd_finish_half(data_dir: str) -> None:
    meta_path = os.path.join(data_dir, HALF_META)
    found = _find_half_line(data_dir)
    if found is None:
        print("[finish-half] 两份日志中都没有待补全的半行，无需操作。")
        return
    target, tail = found
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as fh:
            full = json.load(fh)["full"]
        if not full.startswith(tail):
            raise SystemExit("[finish-half] 半行与元数据不一致，无法安全补全。")
        rest = full[len(tail):]
    else:
        # 无元数据时无法复原原始 JSON，直接给出明确失败提示而非猜测
        raise SystemExit("[finish-half] 缺少半行元数据（.half.json），无法补全；"
                         "请用 reset 后重新 half。")
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(rest + "\n")
    os.remove(meta_path)
    print(f"[finish-half] 已在 {os.path.basename(target)} 补全半行并写入换行：{full}")


def cmd_rotate(data_dir: str) -> None:
    active, rotated = _paths(data_dir)
    if not os.path.exists(active):
        print("[rotate] active.ndjson 不存在，先创建空文件再轮转。")
        open(active, "a").close()
    if os.path.exists(rotated):
        print("[rotate] 警告：active.ndjson.1 已存在；真实设备不会覆盖，"
              "本次仍按设备语义用 rename 替换（旧 .1 已被设备更早轮转换走）。")
    os.rename(active, rotated)
    open(active, "a").close()
    print(f"[rotate] 完成归档：{active} -> {rotated}，并新建空 {ACTIVE}")


def cmd_illegal(data_dir: str, kind: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    active, _ = _paths(data_dir)
    seq = _next_seq(data_dir)
    if kind == "nan":
        line = json.dumps({"seq": seq, "ts": _now_iso(), "temp": float("nan")})
        # json 默认允许 NaN，但采集端 math.isfinite 会拒绝
    elif kind == "bad-json":
        line = '{"seq": %d, "ts": "%s", "temp": 26.1,}' % (seq, _now_iso())  # 尾随逗号
    elif kind == "bad-ts":
        line = json.dumps({"seq": seq, "ts": "2026-11-15 08:00:00", "temp": 26.1})
    elif kind == "dup-conflict":
        # 同序号异内容：序号取当前最大值（若没有记录则写 seq=1，再配合 write 制造冲突）
        seq = max(1, seq - 1)
        line = json.dumps({"seq": seq, "ts": _now_iso(), "temp": 999.0})
    else:
        raise SystemExit(f"未知非法类型：{kind}")
    with open(active, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(f"[illegal:{kind}] 写入一条完整但非法的行（seq={seq}），采集时应整轮拒绝：{line}")


def cmd_reset(data_dir: str) -> None:
    active, rotated = _paths(data_dir)
    for p in (active, rotated, os.path.join(data_dir, HALF_META)):
        if os.path.exists(p):
            os.remove(p)
            print(f"[reset] 已删除 {p}")
    print("[reset] 日志目录已清空（数据库请通过网页“演示重置”或重新建库清理）。")


def _http_collect(base_url: str) -> dict:
    req = urllib.request.Request(base_url.rstrip("/") + "/collect", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {"http": resp.status}
    except urllib.error.HTTPError as exc:  # 采集业务失败仍是 302/200，这里仅作兜底
        return {"http": exc.code}


def cmd_seed(data_dir: str) -> None:
    """预置演示样本：.1 中含完整历史记录 + 一个半行，active 中含两条新记录。

    序号安排（保证采集端“连续递增”规则不被半行打断）：
      .1 完整行 seq 1..3，半行 seq 6（预留，补全后再采集）；
      active 完整行 seq 4..5。
    第一次采集：5 条完整记录（1-5）入库，半行原样保留；
    finish-half 后再次采集：seq 6 补录，共 6 条。
    """
    cmd_reset(data_dir)
    active, rotated = _paths(data_dir)
    # 先在 active 写 3 条历史，再轮转到 .1
    cmd_write(data_dir, 3)
    cmd_rotate(data_dir)
    # 旧文件末尾追加半行（模拟轮转后旧文件仍被续写但只写了一半）。
    # 半行序号取“active 两条之后”的位置，避免在其未完成时制造序号断档。
    seq_half = 6
    full = json.dumps({"seq": seq_half, "ts": _now_iso(), "temp": 26.315},
                      ensure_ascii=False)
    piece = full[: len(full) // 2]
    with open(rotated, "a", encoding="utf-8") as fh:
        fh.write(piece)
    with open(os.path.join(data_dir, HALF_META), "w", encoding="utf-8") as fh:
        json.dump({"full": full, "written_bytes": len(piece.encode("utf-8"))}, fh,
                  ensure_ascii=False)
    # 新 active 写两条（_next_seq 会读到半行的 seq=6，故这里显式写 4、5）
    with open(active, "a", encoding="utf-8") as fh:
        for seq in (4, 5):
            fh.write(json.dumps(
                {"seq": seq, "ts": _now_iso(),
                 "temp": round(25.0 + (seq % 7) * 0.42, 3)},
                ensure_ascii=False) + "\n")
    print("[seed] 演示样本就绪：")
    print(f"  {ROTATED}：3 条完整历史（seq 1-3）+ 1 个未结束半行（seq {seq_half}）")
    print(f"  {ACTIVE}：2 条完整新记录（seq 4-5）")
    print("  操作建议：网页点“立即采集” -> 5 条入库且页面显示半行字节；")
    print("            执行 finish-half 后再采集 -> seq 6 补录，共 6 条。")


def cmd_auto(data_dir: str, base_url: str) -> None:
    """一键演示剧本：正常 -> 半行 -> 轮转续写 -> 重复点击 -> 非法整轮拒绝 -> 补采。"""
    active, rotated = _paths(data_dir)
    step = lambda t: print("\n" + "=" * 68 + f"\n步骤：{t}\n" + "=" * 68)

    step("1/8 重置环境（清空两份日志）")
    cmd_reset(data_dir)

    step("2/8 设备写入 3 条合法记录，网页采集")
    cmd_write(data_dir, 3)
    _http_collect(base_url)
    print("       -> 已调用 POST /collect，预期入库 3 条（序号 1-3）")

    step("3/8 再写 2 条，连续点击两次采集（验证不重记、不漏记）")
    cmd_write(data_dir, 2)
    _http_collect(base_url)
    _http_collect(base_url)
    print("       -> 第一次入库 2 条（序号 4-5），第二次无新增、空操作")

    step("4/8 写入半行后采集（半行不入库、进度不前移）")
    cmd_half(data_dir)
    _http_collect(base_url)
    print("       -> 完整行 0 条，半行字节保留在文件中，页面显示 half_line_bytes>0 的轮次")

    step("5/8 设备轮转：active -> active.ndjson.1，新 active 写 2 条；旧文件补全半行")
    cmd_rotate(data_dir)
    cmd_write(data_dir, 2)  # 写入新 active（序号接续）
    # 旧文件（.1）里的半行补全换行，模拟“轮转后旧文件仍被续写”
    meta_path = os.path.join(data_dir, HALF_META)
    with open(rotated, "rb") as rf:
        old = rf.read()
    half = old[old.rfind(b"\n") + 1 :].decode("utf-8")
    with open(meta_path, "r", encoding="utf-8") as mf:
        full = json.load(mf)["full"]
    if not full.startswith(half):
        raise SystemExit("演示异常：旧文件半行与 .half.json 元数据不一致。")
    with open(rotated, "a", encoding="utf-8") as fh:
        fh.write(full[len(half):] + "\n")
    os.remove(meta_path)
    print(f"       -> 旧文件 {ROTATED} 的半行已补全；新 {ACTIVE} 有 2 条完整记录")
    _http_collect(base_url)
    print("       -> 预期入库 3 条（旧文件补全的 1 条 + 新文件 2 条），无漏记")

    step("6/8 再轮转一次并写入，验证多次轮转后的接续")
    cmd_rotate(data_dir)
    cmd_write(data_dir, 2)
    _http_collect(base_url)
    print("       -> 预期再入库 2 条，旧 inode 进度不再重复读取")

    step("7/8 写入非法完整行（温度 NaN），采集应整轮拒绝，库与进度不变")
    cmd_illegal(data_dir, "nan")
    _http_collect(base_url)
    print("       -> 采集轮次状态应为 rejected；记录数不增加，file_state 偏移不前移")
    print("       （非法字节仍留在文件中，可在页面“最近采集轮次”看到拒绝原因）")

    step("8/8 移除非法行后续写合法记录，验证恢复采集")
    _strip_last_line(active)
    cmd_write(data_dir, 1)
    _http_collect(base_url)
    print("       -> 预期再入库 1 条，系统恢复正常")
    print("\n演示结束。打开网页查看记录、采集轮次与进度，并可下载 CSV：")
    print(f"  {base_url}/    （CSV：{base_url}/records.csv）")


def _strip_last_line(path: str) -> None:
    with open(path, "rb") as fh:
        raw = fh.read()
    cut = raw.rfind(b"\n", 0, -1)
    kept = raw[: cut + 1] if cut >= 0 else b""
    with open(path, "wb") as fh:
        fh.write(kept)
    print(f"       -> 已移除 {path} 末尾非法行，文件恢复为 {len(kept)} 字节")


def main() -> None:
    ap = argparse.ArgumentParser(description="发酵罐 NDJSON 日志设备模拟器")
    ap.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "./data"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("write", help="追加 N 条完整合法记录")
    p.add_argument("n", type=int)

    sub.add_parser("half", help="写入不带换行的半行")
    sub.add_parser("finish-half", help="补全当前半行并加换行")
    sub.add_parser("rotate", help="模拟设备归档轮转")

    p = sub.add_parser("illegal", help="写入一条完整但非法的行")
    p.add_argument("kind", nargs="?", default="nan",
                   choices=["nan", "bad-json", "bad-ts", "dup-conflict"])

    sub.add_parser("reset", help="清空目录内两份日志")
    sub.add_parser("seed", help="预置含半行与轮转文件的演示样本")
    p = sub.add_parser("auto", help="一键演示剧本（需 Web 服务在运行）")
    p.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://localhost:8000"))

    args = ap.parse_args()
    if args.cmd == "write":
        cmd_write(args.data_dir, args.n)
    elif args.cmd == "half":
        cmd_half(args.data_dir)
    elif args.cmd == "finish-half":
        cmd_finish_half(args.data_dir)
    elif args.cmd == "rotate":
        cmd_rotate(args.data_dir)
    elif args.cmd == "illegal":
        cmd_illegal(args.data_dir, args.kind)
    elif args.cmd == "reset":
        cmd_reset(args.data_dir)
    elif args.cmd == "seed":
        cmd_seed(args.data_dir)
    elif args.cmd == "auto":
        cmd_auto(args.data_dir, args.base_url)


if __name__ == "__main__":
    main()
