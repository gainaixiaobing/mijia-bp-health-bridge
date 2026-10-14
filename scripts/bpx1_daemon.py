#!/usr/bin/env python3
"""BPX1 常驻守护 — 持续监听血压计测量广播，解密后写入 iCloud inbox

用法:
    python3 bpx1_daemon.py            # 常驻运行
    python3 bpx1_daemon.py 60         # 测试运行 60 秒

配置:
    BPX1_BINDKEY / BPX1_MAC （或仓库根目录 bp_bindkey.hex）
    BPX1_INBOX    输出目录（默认 ~/Library/Mobile Documents/com~apple~CloudDocs/bp-health/inbox）

依赖: pip install bleak cryptography
"""
import asyncio
import json
import os
import struct
import sys
import time
from datetime import datetime

from bleak import BleakScanner
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRODUCT_ID = 8120
INBOX = os.environ.get("BPX1_INBOX") or os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/bp-health/inbox"
)
TEST_SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 0

seen = {}
stats = {"frames": 0, "bpx1": 0, "caught": 0}


def load_bindkey():
    v = os.environ.get("BPX1_BINDKEY", "").strip()
    if not v:
        p = os.path.join(ROOT, "bp_bindkey.hex")
        if os.path.exists(p):
            v = open(p).read().strip()
    if len(v) != 32:
        print("错误: 未找到 bindkey（32位hex）。请设置 BPX1_BINDKEY 或写入 bp_bindkey.hex")
        sys.exit(1)
    return bytes.fromhex(v)


BINDKEY = load_bindkey()
DEVICE_MAC = os.environ.get("BPX1_MAC", "").strip().upper()


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def mac_to_bytes(mac):
    return bytes(int(x, 16) for x in mac.split(":"))


def decode(service_data):
    if len(service_data) < 5:
        return None
    fc = service_data[0] | (service_data[1] << 8)
    pid = service_data[2] | (service_data[3] << 8)
    if pid != PRODUCT_ID:
        return None
    stats["bpx1"] += 1
    counter = service_data[4]
    cur = 5
    if fc & 0x10:
        if len(service_data) < cur + 6:
            return None
        mac = ":".join(f"{b:02X}" for b in reversed(service_data[cur:cur + 6]))
        cur += 6
    else:
        mac = DEVICE_MAC
        if not mac:
            return None
    if fc & 0x20:
        cap = service_data[cur]
        cur += 1
        if cap & 0x20:
            cur += 1
    if not (fc & 0x40):
        return None
    if fc & 0x08:
        if len(service_data) < cur + 8:
            return None
        ciphertext = service_data[cur:-7]
        rand3 = service_data[-7:-4]
        tag = service_data[-4:]
        nonce = bytes(reversed(mac_to_bytes(mac))) + service_data[2:5] + rand3
        try:
            payload = AESCCM(BINDKEY, tag_length=4).decrypt(nonce, ciphertext + tag, b"\x11")
        except Exception:
            return None
    else:
        payload = service_data[cur:]
    i = 0
    while i + 3 <= len(payload):
        oid = payload[i] | (payload[i + 1] << 8)
        ln = payload[i + 2]
        val = payload[i + 3:i + 3 + ln]
        if len(val) < ln:
            break
        if ln == 9:
            raw_time = struct.unpack("<I", val[0:4])[0]
            syst = val[4] | (val[5] << 8)
            dia = val[6]
            hr = val[7]
            flags = val[8]
            if 40 <= syst <= 260 and 20 <= dia <= 200 and 20 <= hr <= 250 and syst > dia:
                now = time.time()
                best = min([raw_time, 946684800 + raw_time], key=lambda c: abs(c - now))
                return {
                    "time": datetime.fromtimestamp(best).strftime("%Y-%m-%dT%H:%M:%S"),
                    "systolic": syst,
                    "diastolic": dia,
                    "heart_rate": hr,
                    "flags": flags,
                    "counter": counter,
                }
        i += 3 + ln
    return None


def write_inbox(m):
    key = (m["time"], m["systolic"], m["diastolic"], m["heart_rate"])
    if key in seen:
        return
    seen[key] = datetime.now().isoformat()
    stats["caught"] += 1
    os.makedirs(INBOX, exist_ok=True)
    fdate = m["time"][:10].replace("-", "")
    ftime = m["time"][11:19].replace(":", "")
    fname = f"bp-{fdate}-{ftime}-{m['systolic']}-{m['diastolic']}-{m['heart_rate']}.json"
    target = os.path.join(INBOX, fname)
    rec = {
        "id": fname[:-5],
        **{k: m[k] for k in ("time", "systolic", "diastolic", "heart_rate", "flags")},
        "irregular": bool(m["flags"] & 1),
        "movement": bool(m["flags"] & 2),
        "source": "bpx1-beacon",
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }
    tmp = target + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(tmp, target)
    log(f"★ 新测量已写入: {fname}  ({m['systolic']}/{m['diastolic']} mmHg ❤{m['heart_rate']})")


def on_detect(device, adv):
    for uuid, data in (adv.service_data or {}).items():
        if "fe95" not in uuid.lower():
            continue
        stats["frames"] += 1
        m = decode(bytes(data))
        if m:
            write_inbox(m)


async def run_session():
    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()
    return scanner


async def main():
    log(f"BPX1 守护启动（产品ID {PRODUCT_ID} / MAC {DEVICE_MAC or '未配置'} / inbox {INBOX}）")
    if TEST_SECONDS:
        scanner = await run_session()
        await asyncio.sleep(TEST_SECONDS)
        await scanner.stop()
        log(f"测试结束: frames={stats['frames']} bpx1={stats['bpx1']} caught={stats['caught']}")
        return
    while True:
        try:
            scanner = await run_session()
            log("scanner ACTIVE")
            while True:
                await asyncio.sleep(1800)  # 每 30 分钟滚动重启扫描，防止长跑卡死
                log(f"stats: frames={stats['frames']} bpx1={stats['bpx1']} caught={stats['caught']}")
                await scanner.stop()
                scanner = await run_session()
        except Exception as e:
            log(f"异常: {e}; 10 秒后重试")
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
