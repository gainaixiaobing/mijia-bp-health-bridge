#!/usr/bin/env python3
"""往 inbox 注入一条测试测量。

用途：不量血压也能把 iPhone 端（快捷指令 → 健康）单独跑通。注入的记录带
`"source": "bpx1-inject"`，方便事后区分。

用法:
    python3 scripts/bpx1_inject.py                     # 118/76 mmHg ❤68，时间=现在
    python3 scripts/bpx1_inject.py --sys 130 --dia 85 --hr 72
    python3 scripts/bpx1_inject.py --time 2026-10-10T16:34:28
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from bpx1_config import ensure_dirs, load_paths
from bpx1_protocol import Measurement, build_record
from bpx1_store import Store


def parse_time(value: str) -> datetime:
    text = value.strip().replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"时间格式应为 2026-10-10T16:34:28：{value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.replace(microsecond=0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="注入一条测试测量到 inbox")
    parser.add_argument("--sys", dest="systolic", type=int, default=118, help="收缩压（默认 118）")
    parser.add_argument("--dia", dest="diastolic", type=int, default=76, help="舒张压（默认 76）")
    parser.add_argument("--hr", dest="heart_rate", type=int, default=68, help="心率（默认 68）")
    parser.add_argument("--time", type=parse_time, default=None, help="测量时间（默认现在）")
    args = parser.parse_args(argv)

    if not (40 <= args.systolic <= 260 and 20 <= args.diastolic <= 200 and args.systolic > args.diastolic):
        print("数值不合法：收缩压应 40~260、舒张压 20~200，且收缩压 > 舒张压。", file=sys.stderr)
        return 2
    if not 20 <= args.heart_rate <= 250:
        print("心率应在 20~250 之间。", file=sys.stderr)
        return 2

    measured_at = args.time or datetime.now().astimezone().replace(microsecond=0)
    measurement = Measurement(
        measured_at=measured_at,
        systolic=args.systolic,
        diastolic=args.diastolic,
        heart_rate=args.heart_rate,
        flags=0,
        counter=0,
        object_id=0x561B,
    )

    paths = load_paths()
    ensure_dirs(paths)
    store = Store(paths)
    record = build_record(measurement, source="bpx1-inject")
    outcome = store.write(record)

    if outcome == "written":
        print(f"✓ 已注入 {store.record_path(record['id'])}")
        print("  现在到 iPhone 上运行快捷指令，健康 App 里应出现这条记录。")
        return 0
    if outcome == "skipped_exists":
        print(f"该时间点已有同样数值的记录：{store.record_path(record['id'])}")
        print("  换一个 --time 再试。")
        return 1
    print(f"跳过：{outcome}（该 id 已有 done 标记，说明手机端已导入过）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
