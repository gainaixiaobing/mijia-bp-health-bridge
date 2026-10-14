#!/usr/bin/env python3
"""把 out/beacon_log.jsonl 里的历史测量补写回 inbox（调试/补货用）

用法:
    python3 refill_inbox.py
"""
import json
import os
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INBOX = os.environ.get("BPX1_INBOX") or os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/bp-health/inbox"
)

log_path = os.path.join(ROOT, "out", "beacon_log.jsonl")
if not os.path.exists(log_path):
    print(f"未找到 {log_path}")
    raise SystemExit(1)

seen = set()
n = 0
total = 0
for line in open(log_path):
    d = json.loads(line)
    t = (d.get("time") or "").replace(" ", "T")
    if len(t) < 19:
        continue
    key = (t, d["systolic"], d["diastolic"], d["heart_rate"])
    if key in seen:
        continue
    seen.add(key)
    total += 1
    fdate = t[:10].replace("-", "")
    ftime = t[11:19].replace(":", "")
    fname = f"bp-{fdate}-{ftime}-{d['systolic']}-{d['diastolic']}-{d['heart_rate']}.json"
    target = os.path.join(INBOX, fname)
    if os.path.exists(target):
        continue
    rec = {
        "id": fname[:-5],
        "time": t,
        "systolic": d["systolic"],
        "diastolic": d["diastolic"],
        "heart_rate": d["heart_rate"],
        "flags": d.get("flags", 0),
        "irregular": bool(d.get("flags", 0) & 1),
        "movement": bool(d.get("flags", 0) & 2),
        "source": "bpx1-beacon",
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }
    with open(target + ".tmp", "w") as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(target + ".tmp", target)
    n += 1

print(f"inbox 现况: 本次补写 {n} 条（日志内共 {total} 条唯一测量）")
if os.path.isdir(INBOX):
    for f in sorted(os.listdir(INBOX)):
        if f.startswith("bp-"):
            print("  -", f)
