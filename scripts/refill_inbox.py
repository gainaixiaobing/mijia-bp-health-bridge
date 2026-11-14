#!/usr/bin/env python3
"""把本地流水里的历史测量补写回 inbox（换机、iCloud 出问题、误删后的恢复用）。

数据来源优先级：
    out/journal.jsonl    守护进程每捕获一条就追加（v0.2 起）
    out/beacon_log.jsonl 只有 bpx1_scan.py 会写（兼容 v0.1 的旧数据）

已经导入过的（done/<id>.json 存在）不会重复补写。

用法:
    python3 scripts/refill_inbox.py
    python3 scripts/refill_inbox.py --from out/beacon_log.jsonl
    python3 scripts/refill_inbox.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from bpx1_config import ensure_dirs, load_paths
from bpx1_store import Store, iter_records

REQUIRED = ("id", "time", "systolic", "diastolic", "heart_rate")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把历史测量补写回 inbox")
    parser.add_argument(
        "--from",
        dest="source",
        default=None,
        help="指定 JSONL 来源（默认自动选 out/journal.jsonl，其次 out/beacon_log.jsonl）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    args = parser.parse_args(argv)

    paths = load_paths()
    ensure_dirs(paths)
    store = Store(paths)

    if args.source:
        from pathlib import Path

        source = Path(args.source).expanduser()
    elif paths.journal.exists():
        source = paths.journal
    else:
        source = paths.beacon_log

    if not source.exists():
        print(f"未找到流水文件：{source}")
        print("提示：先常驻运行一次守护进程（或 bpx1_scan.py）再补写。")
        return 1

    counts: Counter[str] = Counter()
    seen: set[str] = set()
    for record in iter_records(source):
        counts["read"] += 1
        if not all(key in record for key in REQUIRED):
            counts["malformed"] += 1
            continue
        record_id = str(record["id"])
        if record_id in seen:
            counts["duplicate"] += 1
            continue
        seen.add(record_id)

        if args.dry_run:
            outcome = "skipped_done" if store.is_done(record_id) else "would_write"
        else:
            try:
                outcome = store.write(record)
            except Exception as exc:  # 单条失败不该中断整批
                print(f"  写入失败 {record_id}: {exc}")
                counts["error"] += 1
                continue
        counts[outcome] += 1

    print(f"来源: {source}")
    print(
        f"读取 {counts['read']} 条 / 唯一 {len(seen)} 条 → 写入 {counts['written']} 条"
        f"（已导入跳过 {counts['skipped_done']}，已存在 {counts['skipped_exists']}，"
        f"坏行 {counts['malformed']}，重复 {counts['duplicate']}，失败 {counts['error']}）"
        + ("（dry-run，未落盘）" if args.dry_run else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
