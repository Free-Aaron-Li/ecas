#!/usr/bin/env python3
"""rtu-live.jsonl → flow_reading（电磁流量计入库）。

背景（2026-11-03）：线上 `ecas-rtu-master` 每秒向 `/capture/rtu-live.jsonl` 追加一帧，
flow 帧（station=flow，站号 8，regs 14 个）**目前只落 JSONL、不进数据库**
⇒ 看板上的流量计区块没有数据可读。

本脚本只做一件事：把 JSONL 里**成功**的 flow 帧写进 `flow_reading`（原始寄存器块），
采集端代码一行不改（rtu-master 容器是只读挂载，动它的风险远大于在宿主机加一个读取器）。

设计要点
--------
* **幂等**：`ON CONFLICT (model, ts) DO NOTHING` ⇒ 重复跑、断电后重跑、从历史回填都不会写重；
* **断点续读**：把「处理到文件的哪个偏移」存进 state 文件，重启后接着读（不重扫 11 MB）；
* **只吃完整行**：文件末尾可能正写到一半（没有换行符），不完整的那行留到下一轮；
* **不碰采集链路**：只读 JSONL、只写 `flow_reading` 一张表，失败也不影响 rtu-master；
* **psql 走 stdin**：口令/DSN 不进命令行（`docker exec -i ecas-db psql -U ecas -d ecas`，
  容器内是 socket trust）。宿主机没有 psql 也能用（全在容器里执行）。

用法
----
    # 常驻（systemd 单元 ecas-flow-ingest.service 用这个）
    flow_ingest.py --follow

    # 一次性：把当前 JSONL 从头回填（第一次上线时把今天已经采到的流量帧补进库）
    flow_ingest.py --once --from-start

    # 演练：只统计会写多少行，不连数据库
    flow_ingest.py --once --from-start --dry-run

    # 本机验证：psql 换成临时库
    flow_ingest.py --once --from-start \
        --jsonl /tmp/rtu-live.jsonl \
        --psql "/opt/homebrew/opt/postgresql@18/bin/psql -h /tmp -p 55432 -U ecas -d ecas_dev -v ON_ERROR_STOP=1 -q -t -A -f -"
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator

#: 与后端 `app/flow.py` 的 `MODEL` / `REG_COUNT` 必须一致（库里的 model 值就是它）
MODEL = "FMC400"
REG_COUNT = 14

DEFAULT_JSONL = "/opt/ecas/rtu-master/logs/rtu-live.jsonl"
DEFAULT_STATE = "/opt/ecas/flow/state.json"
DEFAULT_PSQL = "docker exec -i ecas-db psql -U ecas -d ecas -v ON_ERROR_STOP=1 -q -t -A -f -"
#: 一批最多多少行（一次 INSERT 太大会超过 psql 命令行/内存的舒适区；5000 行 ≈ 700 KB SQL）
BATCH = 5000


def log(msg: str) -> None:
    print(f"[flow-ingest] {msg}", file=sys.stderr, flush=True)


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_state(path: Path, offset: int, last_ts: str | None, inserted: int) -> None:
    """原子写：先写临时文件再 rename —— 断电时不至于留下半个 JSON 让下次启动从头再来。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "offset": offset,
            "last_ts": last_ts,
            "inserted": inserted,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:  # 存不了偏移不该让入库停摆（最坏是下次重复扫一遍，ON CONFLICT 兜住）
        log(f"state 写入失败（{exc}），继续运行")


def iter_frames(jsonl: Path, start_offset: int) -> Iterator[tuple[int, dict[str, Any]]]:
    """从 `start_offset` 起逐行产出 `(行尾偏移, 帧 dict)`。

    只产出**以换行结尾**的完整行：文件正被 rtu-master 追加，末尾大概率是半行，
    把半行解析成 JSON 会静默丢一帧（或更糟，抛异常打断整轮）。
    """
    with jsonl.open("rb") as fh:
        fh.seek(start_offset)
        offset = start_offset
        while True:
            raw = fh.readline()
            if not raw:
                return
            offset += len(raw)
            if not raw.endswith(b"\n"):
                # 半行：回到本行开头，等下一次读（offset 不前移）
                return
            try:
                frame = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue  # 坏行跳过，但不阻塞后续（JSONL 是一行一帧，坏一行不影响别的）
            if isinstance(frame, dict):
                yield offset, frame


def extract(frame: dict[str, Any]) -> tuple[str, list[int]] | None:
    """一帧 → `(ts, regs)`；不是「成功的 flow 帧」就返回 None。"""
    if frame.get("station") != "flow" or not frame.get("ok"):
        return None
    ts = frame.get("ts")
    regs = frame.get("regs")
    if not isinstance(ts, str) or not isinstance(regs, list) or len(regs) < REG_COUNT:
        return None
    try:
        values = [int(v) for v in regs[:REG_COUNT]]
    except (TypeError, ValueError):
        return None
    if any(v < 0 or v > 0xFFFF for v in values):
        return None  # 寄存器只可能是 16 位无符号；越界说明帧坏了，宁可不入库
    return ts, values


def row_sql(ts: str, regs: list[int]) -> str:
    """一行 VALUES 字面量。时间戳只允许 ISO 8601 里的安全字符（防拼接问题）。"""
    if not all(ch.isdigit() or ch in "-:+.TZ" for ch in ts):
        raise ValueError(f"非法时间戳：{ts!r}")
    array = "{" + ",".join(str(v) for v in regs) + "}"
    return f"('{MODEL}','{ts}','{array}'::integer[])"


def insert(psql: str, rows: list[str], dry_run: bool) -> None:
    """一批 INSERT。psql 经 stdin 收 SQL，口令不进 argv（凭据铁律）。"""
    sql = (
        "INSERT INTO flow_reading (model, ts, regs) VALUES\n"
        + ",\n".join(rows)
        + "\nON CONFLICT (model, ts) DO NOTHING;\n"
    )
    if dry_run:
        log(f"[dry-run] 会执行 {len(rows)} 行（SQL {len(sql)} 字节），首行 {rows[0]}")
        return
    proc = subprocess.run(
        shlex.split(psql),
        input=sql.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace").strip()[:800])


def run_once(args: argparse.Namespace) -> int:
    jsonl = Path(args.jsonl)
    state_path = Path(args.state)
    if not jsonl.exists():
        log(f"JSONL 不存在：{jsonl}")
        return 1
    state = load_state(state_path)
    size = jsonl.stat().st_size
    offset = 0 if args.from_start else int(state.get("offset") or 0)
    if offset > size:
        log(f"文件变小（{offset} → {size}），按轮转处理，从头读")
        offset = 0

    seen = inserted = 0
    last_ts: str | None = state.get("last_ts")
    pending: list[str] = []
    batch_rows = 0

    def flush() -> None:
        nonlocal pending, batch_rows, inserted
        if not pending:
            return
        insert(args.psql, pending, args.dry_run)
        inserted += len(pending)
        pending = []
        batch_rows = 0

    for end_offset, frame in iter_frames(jsonl, offset):
        offset = end_offset
        parsed = extract(frame)
        if parsed is None:
            continue
        ts, regs = parsed
        pending.append(row_sql(ts, regs))
        last_ts = ts
        seen += 1
        batch_rows += 1
        if batch_rows >= BATCH:
            flush()
            if not args.dry_run:
                save_state(state_path, offset, last_ts, inserted)
        if args.limit and seen >= args.limit:
            break
    flush()
    if not args.dry_run:
        save_state(state_path, offset, last_ts, inserted)
    log(
        f"{'[dry-run] ' if args.dry_run else ''}本轮：读 {seen} 条成功 flow 帧，"
        f"写 {inserted} 行（PSQL 侧按 PK 去重），偏移 {offset}，最后时间 {last_ts}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="把 rtu-live.jsonl 里的流量帧写进 flow_reading")
    parser.add_argument("--jsonl", default=DEFAULT_JSONL, help=f"采集日志路径（默认 {DEFAULT_JSONL}）")
    parser.add_argument("--state", default=DEFAULT_STATE, help=f"断点状态文件（默认 {DEFAULT_STATE}）")
    parser.add_argument("--psql", default=DEFAULT_PSQL, help="psql 命令（默认走 docker exec）")
    parser.add_argument("--once", action="store_true", help="读到当前 EOF 就退出")
    parser.add_argument("--follow", action="store_true", help="常驻：增量读（默认模式）")
    parser.add_argument("--from-start", action="store_true", help="忽略 state，从文件开头回填")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不连数据库、不写 state")
    parser.add_argument("--limit", type=int, default=0, help="本轮最多处理多少帧（调试用）")
    parser.add_argument("--interval", type=float, default=2.0, help="--follow 的轮询间隔秒")
    args = parser.parse_args()

    if not args.once and not args.follow:
        args.follow = True  # 不带参数 = 常驻，和 systemd 单元一致

    if args.once:
        return run_once(args)

    while True:
        try:
            code = run_once(args)
        except Exception as exc:  # 数据库没起/psql 不通：不能退出，等下一轮（rtu-master 还在写）
            log(f"本轮失败：{exc}")
            code = 1
        if code != 0 and not Path(args.jsonl).exists():
            log("JSONL 一直不存在，退出（避免 systemd 无限重启刷日志）")
            return 1
        time.sleep(max(0.2, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
