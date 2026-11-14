#!/usr/bin/env python3
"""BPX1 常驻守护 —— 监听血压计测量广播，解密后写入 iCloud inbox。

用法:
    python3 scripts/bpx1_daemon.py            # 常驻
    python3 scripts/bpx1_daemon.py 60         # 测试运行 60 秒

相对 v0.1 的行为变化（都是为了让"收不到数据"这件事可诊断）：
- 每一帧的结局都分类计数（heartbeat / decrypt_failed / no_mac / bad_frame ...），
  bindkey 不匹配、未配置 MAC 这类问题会主动打 WARN，而不是静默；
- 写盘走 staging 目录再 rename 进 inbox，避免 .tmp 中间态被 iCloud 同步给手机；
- 见到 done/<id>.json 就不再补发，重启/重跑不会重复导入；
- 记录同时进本地 journal，refill 有据可依；
- 扫描器只在"真的没帧了"或"跑满 6 小时"时回收，不再每 30 分钟无条件重启；
- 配置不全时不会进入 10 秒一次的重启风暴，而是打日志 + 每 5 分钟重试一次。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import traceback
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from bleak import BleakScanner

from bpx1_config import (
    Issue,
    Paths,
    check_environment,
    describe,
    ensure_dirs,
    load_bindkey,
    load_device_mac,
    load_max_time_skew,
    load_paths,
)
from bpx1_protocol import build_record, parse_frame
from bpx1_store import Store, StoreError

TICK_SECONDS = 30.0
IDLE_RECYCLE_SECONDS = 15 * 60.0  # 连续 15 分钟没有任何 FE95 帧 → 认为扫描器卡住
MAX_SESSION_SECONDS = 6 * 3600.0  # 兜底：跑满 6 小时回收一次
RETRY_SECONDS = 10.0
STATS_EVERY_SECONDS = 30 * 60.0
CONFIG_RETRY_SECONDS = 300.0


# ---------------------------------------------------------------- 日志


class RotatingLog:
    """带按大小轮转的日志。launchd 的 StandardOutPath 只增不减，所以主日志自己管。"""

    def __init__(self, path: Path, max_bytes: int = 5 * 1024 * 1024, backups: int = 3) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, message: str) -> None:
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n"
        sys.stdout.write(line)
        sys.stdout.flush()
        try:
            self._rotate_if_needed()
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass  # 日志写不进去不应该影响采集

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
        except OSError:
            return
        for index in range(self.backups - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))


LOG: RotatingLog | None = None


def log(message: str) -> None:
    if LOG is None:
        print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)
    else:
        LOG.write(message)


# ---------------------------------------------------------------- 统计


class Stats:
    def __init__(self) -> None:
        self.reasons: Counter[str] = Counter()
        self.outcomes: Counter[str] = Counter()
        self.duplicate = 0

    def bump(self, reason: str) -> None:
        self.reasons[reason] += 1

    def record_outcome(self, outcome: str) -> None:
        self.outcomes[outcome] += 1

    @property
    def written(self) -> int:
        return self.outcomes["written"]

    @property
    def decrypted(self) -> int:
        # 能走到 bad_values / time_suspect，说明 AES-CCM 已经解开了
        return self.reasons["ok"] + self.reasons["bad_values"] + self.reasons["time_suspect"]

    @property
    def bpx1_frames(self) -> int:
        excluded = {"frames", "short", "other_pid", "unsupported_version"}
        return sum(count for reason, count in self.reasons.items() if reason not in excluded)

    def summary(self) -> str:
        return (
            f"frames={self.reasons['frames']} bpx1={self.bpx1_frames} "
            f"ok={self.reasons['ok']} dup={self.duplicate} written={self.written} "
            f"skipped_done={self.outcomes['skipped_done']} "
            f"decrypt_failed={self.reasons['decrypt_failed']} "
            f"bad_frame={self.reasons['bad_frame']} no_mac={self.reasons['no_mac']} "
            f"time_suspect={self.reasons['time_suspect']} "
            f"heartbeat={self.reasons['heartbeat']} other_pid={self.reasons['other_pid']}"
        )


@dataclass
class Runtime:
    paths: Paths
    bindkey: bytes
    bindkey_source: str
    mac: str
    store: Store
    max_skew: int
    stats: Stats = field(default_factory=Stats)
    seen: dict[str, float] = field(default_factory=dict)
    warn_at: dict[str, float] = field(default_factory=dict)
    last_frame_at: float = 0.0

    def warn(self, key: str, message: str, interval: float = 3600.0) -> None:
        now = time.monotonic()
        if now - self.warn_at.get(key, -1e9) >= interval:
            self.warn_at[key] = now
            log(f"WARN {message}")


# ---------------------------------------------------------------- 采集


def handle_advertisement(runtime: Runtime, adv) -> None:
    rssi = getattr(adv, "rssi", None)
    for uuid, payload in (adv.service_data or {}).items():
        if "fe95" not in uuid.lower():
            continue

        runtime.last_frame_at = time.monotonic()
        runtime.stats.bump("frames")
        measurement, reason = parse_frame(
            bytes(payload),
            bindkey=runtime.bindkey,
            device_mac=runtime.mac,
            max_skew=runtime.max_skew,
        )
        runtime.stats.bump(reason)

        if measurement is None:
            _explain_miss(runtime, reason)
            continue

        record_id = measurement.id
        if record_id in runtime.seen:
            runtime.stats.duplicate += 1
            continue
        runtime.seen[record_id] = time.monotonic()
        if len(runtime.seen) > 5000:
            runtime.seen = dict(list(runtime.seen.items())[-2000:])

        record = build_record(measurement, device_mac=runtime.mac, rssi=rssi)
        try:
            runtime.store.journal_append(record)
        except OSError as exc:
            runtime.warn("journal", f"写 journal 失败：{exc}", 600)

        try:
            outcome = runtime.store.write(record)
        except StoreError as exc:
            runtime.warn("write", str(exc), 600)
            continue

        runtime.stats.record_outcome(outcome)
        if outcome == "written":
            log(
                f"★ 新测量 {record['time']} {record['systolic']}/{record['diastolic']} mmHg "
                f"❤{record['heart_rate']} → {record_id}.json"
                + ("（设备时钟可疑）" if record["time_suspect"] else "")
            )
        elif outcome == "skipped_done":
            runtime.warn("done", f"{record_id} 已导入过（done 标记存在），跳过", 600)


def _explain_miss(runtime: Runtime, reason: str) -> None:
    if reason == "no_mac":
        runtime.warn(
            "no_mac",
            "广播帧不含 MAC 且未配置 BPX1_MAC，无法解密。"
            "请设置 BPX1_MAC=<设备MAC>（米家 App → 设备 → 设备信息）。",
            1800,
        )
    elif reason == "decrypt_failed":
        if runtime.stats.decrypted == 0 and runtime.stats.reasons["decrypt_failed"] >= 5:
            runtime.warn(
                "decrypt",
                f"已收到 {runtime.stats.reasons['decrypt_failed']} 个加密帧但无一解开 —— "
                "bindkey 与设备不匹配，或 BPX1_MAC 写错（nonce 用 MAC 反序）。",
                1800,
            )
    elif reason == "bad_frame":
        runtime.warn("bad_frame", "收到结构异常的 FE95 帧（已跳过）。若持续出现，多半是固件改版。", 3600)
    elif reason == "time_suspect":
        runtime.warn(
            "time_suspect",
            "测量时间与当前时间相差超过 24 小时，已拒收 —— 设备未对时？"
            "先在米家 App 里连一次设备再测。",
            1800,
        )


def make_callback(runtime: Runtime):
    def on_detect(_device, adv) -> None:
        try:
            handle_advertisement(runtime, adv)
        except Exception:  # 回调里的异常绝不能打断扫描循环
            runtime.warn("callback", f"处理广播异常（已忽略）：{traceback.format_exc(limit=3)}", 600)

    return on_detect


# ---------------------------------------------------------------- 运行


async def run_forever(runtime: Runtime) -> None:
    callback = make_callback(runtime)
    while True:
        scanner = None
        try:
            scanner = BleakScanner(detection_callback=callback)
            await scanner.start()
            started = time.monotonic()
            runtime.last_frame_at = started
            last_stats = started
            frames_at_start = runtime.stats.reasons["frames"]
            log(f"scanner ACTIVE（{describe(runtime.paths, runtime.bindkey_source, runtime.mac)}）")

            while True:
                await asyncio.sleep(TICK_SECONDS)
                now = time.monotonic()
                if now - last_stats >= STATS_EVERY_SECONDS:
                    last_stats = now
                    log(f"stats: {runtime.stats.summary()}")

                idle = now - runtime.last_frame_at
                age = now - started
                frames_this_session = runtime.stats.reasons["frames"] - frames_at_start
                # 只有"本来收得到、后来突然没帧"才急着回收；设备不在附近时不必每 15 分钟折腾一次
                if (frames_this_session > 0 and idle > IDLE_RECYCLE_SECONDS) or age > MAX_SESSION_SECONDS:
                    log(
                        f"回收扫描器（本轮 {frames_this_session} 帧, idle={idle:.0f}s, "
                        f"session={age:.0f}s）"
                    )
                    await _stop(scanner)
                    scanner = None
                    break
        except asyncio.CancelledError:
            await _stop(scanner)
            raise
        except Exception as exc:
            log(f"扫描器异常：{exc!r}；{RETRY_SECONDS:.0f} 秒后重试")
            await _stop(scanner)
            await asyncio.sleep(RETRY_SECONDS)


async def run_for_seconds(runtime: Runtime, seconds: int) -> int:
    callback = make_callback(runtime)
    scanner: BleakScanner | None = None
    try:
        scanner = BleakScanner(detection_callback=callback)
        await scanner.start()
        runtime.last_frame_at = time.monotonic()
        log(f"测试运行 {seconds} 秒 ...")
        await asyncio.sleep(seconds)
    finally:
        await _stop(scanner)
    log(f"测试结束：{runtime.stats.summary()}")
    return 0


async def _stop(scanner: BleakScanner | None) -> None:
    if scanner is None:
        return
    try:
        await scanner.stop()
    except Exception as exc:
        log(f"停止扫描器时异常（已忽略）：{exc!r}")


# ---------------------------------------------------------------- 启动


def build_runtime() -> tuple[Runtime | None, list[Issue]]:
    paths = load_paths()
    bindkey = load_bindkey()
    mac, mac_error = load_device_mac()
    issues = check_environment(paths, bindkey, mac, mac_error)

    if bindkey.value is None or any(issue.level == "error" for issue in issues):
        return None, issues

    ensure_dirs(paths)
    store = Store(paths)
    store.ensure()
    return Runtime(paths, bindkey.value, bindkey.source, mac or "", store, load_max_time_skew()), issues


def report_issues(issues: list[Issue]) -> None:
    for issue in issues:
        tag = "ERROR" if issue.level == "error" else "WARN"
        suffix = f"；{issue.hint}" if issue.hint else ""
        log(f"{tag} {issue.message}{suffix}")


def wait_for_runtime() -> Runtime:
    """配置不全时留在进程里等（launchd 就不必 10 秒重启一次）。"""
    while True:
        time.sleep(CONFIG_RETRY_SECONDS)
        runtime, issues = build_runtime()
        report_issues(issues)
        if runtime is not None:
            log("配置已就绪，继续启动。")
            return runtime


def main(argv: list[str] | None = None) -> int:
    global LOG

    parser = argparse.ArgumentParser(description="BPX1 米家血压计 → iCloud inbox 守护进程")
    parser.add_argument("seconds", nargs="?", type=int, default=0, help="测试运行秒数（省略则常驻）")
    args = parser.parse_args(argv)

    paths = load_paths()
    LOG = RotatingLog(paths.daemon_log)
    log(f"BPX1 守护启动（产品ID 8120 / inbox {paths.inbox}）")

    runtime, issues = build_runtime()
    report_issues(issues)

    if runtime is None:
        if args.seconds:
            log("配置不完整，测试模式直接退出。")
            return 2
        runtime = wait_for_runtime()

    log(f"配置：{describe(runtime.paths, runtime.bindkey_source, runtime.mac)}")
    try:
        if args.seconds:
            return asyncio.run(run_for_seconds(runtime, args.seconds))
        asyncio.run(run_forever(runtime))
        return 0
    except KeyboardInterrupt:
        log("收到中断，退出。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
