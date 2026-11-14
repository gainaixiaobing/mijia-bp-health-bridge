#!/usr/bin/env python3
"""BPX1 扫描解码器 —— 捕获血压计测量广播并解密（测试/排障用，不常驻）。

用法:
    python3 scripts/bpx1_scan.py            # 扫描 90 秒
    python3 scripts/bpx1_scan.py 30         # 扫描 30 秒
    python3 scripts/bpx1_scan.py 60 --verbose

配置与守护进程完全一致（见 scripts/bpx1_config.py）：
    BPX1_BINDKEY  或 <repo>/bp_bindkey.hex
    BPX1_MAC      设备 MAC（macOS 广播不含 MAC，必需）

依赖: pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter

from bleak import BleakScanner

from bpx1_config import check_environment, load_bindkey, load_device_mac, load_paths
from bpx1_protocol import build_record, parse_frame


class Session:
    def __init__(self, bindkey: bytes, mac: str, beacon_log, verbose: bool) -> None:
        self.bindkey = bindkey
        self.mac = mac
        self.beacon_log = beacon_log
        self.verbose = verbose
        self.reasons: Counter[str] = Counter()
        self.seen: dict[tuple, int] = {}
        self.started = time.time()

    def on_detect(self, _device, adv) -> None:
        try:
            self._handle(adv)
        except Exception as exc:  # 回调里的异常不能打断扫描
            print(f"  ⚠️ 处理广播异常（已忽略）：{exc!r}", flush=True)

    def _handle(self, adv) -> None:
        for uuid, payload in (adv.service_data or {}).items():
            if "fe95" not in uuid.lower():
                continue
            measurement, reason = parse_frame(
                bytes(payload), bindkey=self.bindkey, device_mac=self.mac
            )
            self.reasons[reason] += 1
            self.reasons["frames"] += 1

            if measurement is None:
                if self.verbose or reason in {"decrypt_failed", "bad_frame", "time_suspect", "no_mac"}:
                    print(f"  [{self.elapsed():5.1f}s] · {reason}", flush=True)
                continue

            self.reasons["decrypted_ok"] += 1
            key = measurement.key
            count = self.seen.get(key, 0) + 1
            self.seen[key] = count
            if count == 1:
                print(
                    f"  [{self.elapsed():5.1f}s] ★ {measurement.measured_at:%Y-%m-%d %H:%M:%S}  "
                    f"{measurement.systolic}/{measurement.diastolic} mmHg  ❤{measurement.heart_rate}  "
                    f"flags=0x{measurement.flags:02x}  (counter={measurement.counter})",
                    flush=True,
                )
                record = build_record(measurement, device_mac=self.mac, rssi=getattr(adv, "rssi", None))
                self.beacon_log.parent.mkdir(parents=True, exist_ok=True)
                with open(self.beacon_log, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def elapsed(self) -> float:
        return time.time() - self.started

    def summary(self) -> str:
        r = self.reasons
        return (
            f"FE95帧={r['frames']} 解密成功={r['decrypted_ok']} "
            f"解密失败={r['decrypt_failed']} 畸形帧={r['bad_frame']} "
            f"缺MAC={r['no_mac']} 时钟可疑={r['time_suspect']} "
            f"心跳={r['heartbeat']} 其他产品={r['other_pid']}"
        )


async def run(session: Session, seconds: int) -> None:
    scanner = BleakScanner(detection_callback=session.on_detect)
    await scanner.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        await scanner.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BPX1 广播扫描/解密（测试用）")
    parser.add_argument("seconds", nargs="?", type=int, default=90, help="扫描秒数（默认 90）")
    parser.add_argument("--verbose", action="store_true", help="打印每一帧的结局")
    args = parser.parse_args(argv)

    paths = load_paths()
    bindkey = load_bindkey()
    mac, mac_error = load_device_mac()
    issues = check_environment(paths, bindkey, mac, mac_error, need_icloud=False)
    for issue in issues:
        prefix = "错误" if issue.level == "error" else "提示"
        print(f"{prefix}: {issue.message}" + (f"（{issue.hint}）" if issue.hint else ""))
    if bindkey.value is None or mac_error:
        return 2

    session = Session(bindkey.value, mac or "", paths.beacon_log, args.verbose)
    print(f"扫描 {args.seconds} 秒（产品ID 8120 / MAC {mac or '未配置'}）...")
    try:
        asyncio.run(run(session, args.seconds))
    except KeyboardInterrupt:
        print("\n已中断。")

    print(f"\n统计: {session.summary()}")
    print(f"唯一测量 {len(session.seen)} 条")
    for (stamp, systolic, diastolic, heart_rate), count in sorted(session.seen.items()):
        print(f"  {stamp}  {systolic}/{diastolic} mmHg  ❤{heart_rate}  (收到 {count} 帧)")
    if session.reasons["frames"] == 0:
        print(
            "\n一帧 FE95 都没收到：确认 Mac 在血压计附近、蓝牙已开，"
            "并且测量后 1~2 秒内不要离开范围（广播不补发）。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
