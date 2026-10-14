#!/usr/bin/env python3
"""BPX1 MiBeacon 扫描解码器 — 捕获米家智能电子血压计(ihealth.bpm.bpx1)的测量广播并解密

用法:
    python3 bpx1_scan.py [扫描秒数，默认90]

配置（二选一）:
    export BPX1_BINDKEY=<32位hex>     # 解密密钥（小米云 blt_get_beaconkey 获取）
    export BPX1_MAC=<设备MAC>          # 设备MAC，如 8C:D0:B2:xx:xx:xx（macOS 必需）
    或将 bindkey 写入仓库根目录 bp_bindkey.hex

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
DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 90


def load_bindkey():
    v = os.environ.get("BPX1_BINDKEY", "").strip()
    if not v:
        p = os.path.join(ROOT, "bp_bindkey.hex")
        if os.path.exists(p):
            v = open(p).read().strip()
    if len(v) != 32:
        print("错误: 未找到 bindkey（32位hex）。请设置环境变量 BPX1_BINDKEY 或写入 bp_bindkey.hex")
        sys.exit(1)
    return bytes.fromhex(v)


BINDKEY = load_bindkey()
DEVICE_MAC = os.environ.get("BPX1_MAC", "").strip().upper()

t0 = time.time()
seen = {}
stats = {"frames": 0, "bpx1": 0, "decrypted": 0, "failed": 0}


def looks_like_mac(s):
    p = s.split(":")
    return len(p) == 6 and all(len(x) == 2 for x in p)


def mac_to_bytes(mac):
    return bytes(int(x, 16) for x in mac.split(":"))


def decode(service_data, source_addr):
    """解析一个 FE95 服务数据帧。返回 (measurement|None, note)"""
    if len(service_data) < 5:
        return None, "short"
    fc = service_data[0] | (service_data[1] << 8)
    pid = service_data[2] | (service_data[3] << 8)
    if pid != PRODUCT_ID:
        return None, "other_pid"
    stats["bpx1"] += 1
    counter = service_data[4]
    encrypted = bool(fc & 0x08)
    cur = 5
    if fc & 0x10:  # 帧内含 MAC（反序存储）
        if len(service_data) < cur + 6:
            return None, "truncated"
        mac = ":".join(f"{b:02X}" for b in reversed(service_data[cur:cur + 6]))
        cur += 6
    else:
        # macOS 上广播不含 MAC（CoreBluetooth 亦不给出），需预先配置设备 MAC
        mac = DEVICE_MAC or (source_addr if looks_like_mac(source_addr) else "")
        if not mac:
            return None, "no_mac"
    if fc & 0x20:  # capability 字段
        cap = service_data[cur]
        cur += 1
        if cap & 0x20:
            cur += 1
    if not (fc & 0x40):
        return None, "heartbeat"
    if encrypted:
        if len(service_data) < cur + 8:
            return None, "truncated"
        ciphertext = service_data[cur:-7]
        rand3 = service_data[-7:-4]
        tag = service_data[-4:]
        # nonce = MAC反序(6) + 产品号(2) + 计数器(1) + 随机数(3)
        nonce = bytes(reversed(mac_to_bytes(mac))) + service_data[2:5] + rand3
        try:
            payload = AESCCM(BINDKEY, tag_length=4).decrypt(nonce, ciphertext + tag, b"\x11")
            stats["decrypted"] += 1
        except Exception:
            stats["failed"] += 1
            return None, "decrypt_failed"
    else:
        payload = service_data[cur:]
    i = 0
    while i + 3 <= len(payload):
        oid = payload[i] | (payload[i + 1] << 8)
        ln = payload[i + 2]
        val = payload[i + 3:i + 3 + ln]
        if len(val) < ln:
            break
        if ln == 9:  # 血压测量对象
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
                    "object_id": oid,
                }, "measurement"
        i += 3 + ln
    return None, "no_measurement"


def on_detect(device, adv):
    for uuid, data in (adv.service_data or {}).items():
        if "fe95" not in uuid.lower():
            continue
        stats["frames"] += 1
        m, note = decode(bytes(data), device.address)
        if note == "measurement" and m:
            key = (m["time"], m["systolic"], m["diastolic"], m["heart_rate"])
            n = seen.get(key, 0) + 1
            seen[key] = n
            if n == 1:
                print(
                    f"  [{time.time() - t0:5.1f}s] ★ {m['time']}  "
                    f"{m['systolic']}/{m['diastolic']} mmHg  ❤{m['heart_rate']}  "
                    f"flags=0x{m['flags']:02x}  (counter={m['counter']})",
                    flush=True,
                )
                os.makedirs(os.path.join(ROOT, "out"), exist_ok=True)
                with open(os.path.join(ROOT, "out", "beacon_log.jsonl"), "a") as f:
                    f.write(json.dumps({**m, "received_at": datetime.now().isoformat()}, ensure_ascii=False) + "\n")
        elif note == "decrypt_failed":
            print(f"  [{time.time() - t0:5.1f}s] ⚠️ 解密失败（检查 BPX1_MAC / bindkey 是否正确）", flush=True)
        elif note == "no_mac" and stats["bpx1"] == 1:
            print("  提示: 帧内不含 MAC 且未配置 BPX1_MAC，无法解密。请设置 BPX1_MAC。", flush=True)


async def main():
    print(f"扫描 {DURATION} 秒（FE95 / 产品ID {PRODUCT_ID} / MAC {DEVICE_MAC or '未配置'}）...")
    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()
    await asyncio.sleep(DURATION)
    await scanner.stop()
    print(f"\n统计: FE95帧={stats['frames']} 血压计帧={stats['bpx1']} 解密成功={stats['decrypted']} 失败={stats['failed']}")
    print(f"唯一测量 {len(seen)} 条:")
    for (t, s, d, h), n in sorted(seen.items()):
        print(f"  {t}  {s}/{d} mmHg  ❤{h}  (x{n})")


if __name__ == "__main__":
    asyncio.run(main())
