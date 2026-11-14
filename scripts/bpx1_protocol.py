#!/usr/bin/env python3
"""BPX1（ihealth.bpm.bpx1）MiBeacon 协议层。

职责：把一帧 FE95 服务数据解析成一次血压测量。
约束：不做任何 I/O、不 import bleak —— 便于在无蓝牙环境（含 CI）做单元测试。

帧结构、加密方式、对象布局见 docs/protocol-notes.md。

设计要点（相对 v0.1 的修正）：
- 能力字段/载荷读取全部做边界检查，畸形帧返回原因码而不是抛异常；
- 用对象 ID（0x561B）而不是"长度恰好是 9"来识别血压对象；
- 校验协议版本，避免固件换代后静默误读；
- 设备时间戳做合理性判断，防止把未对时的 2000-01-01 写进健康。
"""

from __future__ import annotations

import re
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

# ---------------------------------------------------------------- 常量

PRODUCT_ID = 8120
PROTOCOL_VERSION = 5  # frameControl 高 4 位
OBJECT_ID_BLOOD_PRESSURE = 0x561B
BP_OBJECT_LENGTH = 9

AAD = b"\x11"
TAG_LENGTH = 4

# frameControl 位定义
FC_ENCRYPTED = 0x0008
FC_HAS_MAC = 0x0010
FC_HAS_CAPABILITY = 0x0020
FC_HAS_PAYLOAD = 0x0040

# 设备时间戳基准：2000-01-01T00:00:00Z 对应的 Unix 秒。
# 注意：这里是 UTC 基准，不是 UTC+8（v0.1 文档曾写错，见 protocol-notes §4）。
EPOCH_2000_UTC = 946_684_800

# 合理性窗口：单次测量一定是"刚刚"发生的，偏差过大说明设备没对时。
DEFAULT_MAX_TIME_SKEW = 24 * 3600  # 超过则拒收
DEFAULT_SUSPECT_TIME_SKEW = 300  # 超过则标记 time_suspect，但仍写入

# 标志位（来自广播，语义未经完整验证，仅作提示）
FLAG_IRREGULAR_HINT = 0x01
FLAG_MOVEMENT_HINT = 0x02

_MAC_JUNK = re.compile(r"[^0-9A-Fa-f]")


# ---------------------------------------------------------------- 数据模型


@dataclass(frozen=True)
class Measurement:
    """一次解密后的血压测量（时间已转成本地时区的 aware datetime）。"""

    measured_at: datetime
    systolic: int
    diastolic: int
    heart_rate: int
    flags: int
    counter: int
    object_id: int
    time_suspect: bool = False

    @property
    def id(self) -> str:
        return record_id(self)

    @property
    def key(self) -> tuple[str, int, int, int]:
        """去重键：测量时间 + 三个数值。同一测量的连续广播帧会命中同一个键。"""
        return (
            self.measured_at.strftime("%Y-%m-%dT%H:%M:%S"),
            self.systolic,
            self.diastolic,
            self.heart_rate,
        )


class _FrameError(Exception):
    """内部使用：把"帧不完整"转成原因码。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------- 参数校验


def normalize_mac(value: str) -> str:
    """把各种写法归一成 8C:D0:B2:AA:BB:CC。

    接受 8C:D0:B2:AA:BB:CC / 8c-d0-b2-aa-bb-cc / 8CD0B2AABBCC。
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("MAC 为空")
    hexdigits = _MAC_JUNK.sub("", text)
    if len(hexdigits) != 12:
        raise ValueError(f"MAC 格式不对：{value!r}（应为 8C:D0:B2:xx:xx:xx 这样的 6 段十六进制）")
    return ":".join(hexdigits[i : i + 2] for i in range(0, 12, 2)).upper()


def mac_to_bytes(mac: str) -> bytes:
    return bytes(int(part, 16) for part in normalize_mac(mac).split(":"))


def parse_bindkey(value: str) -> bytes:
    """校验并解析 32 位十六进制 bindkey。"""
    text = (value or "").strip()
    if len(text) != 32:
        raise ValueError(f"bindkey 长度应为 32 位十六进制，实际 {len(text)} 位")
    try:
        return bytes.fromhex(text)
    except ValueError as exc:  # noqa: TRY003 - 对用户输出的中文错误
        raise ValueError("bindkey 含非十六进制字符") from exc


# ---------------------------------------------------------------- 时间


def convert_device_time(raw: int) -> datetime:
    """设备秒数 → 本地时区 aware datetime。"""
    return datetime.fromtimestamp(EPOCH_2000_UTC + raw).astimezone()


def _resolve_time(
    raw: int,
    *,
    now: float,
    max_skew: int,
    suspect_skew: int,
) -> tuple[datetime | None, bool, str]:
    ts = EPOCH_2000_UTC + raw
    skew = abs(ts - now)
    if skew > max_skew:
        return None, False, "time_suspect"
    return convert_device_time(raw), skew > suspect_skew, "ok"


# ---------------------------------------------------------------- 帧解析


def parse_frame(
    data: bytes,
    *,
    bindkey: bytes,
    device_mac: str = "",
    now: float | None = None,
    max_skew: int = DEFAULT_MAX_TIME_SKEW,
    suspect_skew: int = DEFAULT_SUSPECT_TIME_SKEW,
) -> tuple[Measurement | None, str]:
    """解析一帧 FE95 服务数据。

    返回 (测量 or None, 原因码)。原因码：
      ok / short / other_pid / unsupported_version / no_mac / truncated /
      bad_frame / heartbeat / decrypt_failed / bad_values / time_suspect /
      no_measurement
    """
    if len(data) < 5:
        return None, "short"

    frame_control, product_id = struct.unpack_from("<HH", data, 0)
    if product_id != PRODUCT_ID:
        return None, "other_pid"
    if (frame_control >> 12) != PROTOCOL_VERSION:
        return None, "unsupported_version"

    counter = data[4]
    offset = 5

    try:
        if frame_control & FC_HAS_MAC:
            offset = _consume_mac(data, offset)
            mac = ":".join(f"{b:02X}" for b in reversed(data[offset - 6 : offset]))
        else:
            if not device_mac:
                return None, "no_mac"
            mac = normalize_mac(device_mac)

        offset = _consume_capability(data, offset, frame_control)

        if not frame_control & FC_HAS_PAYLOAD:
            return None, "heartbeat"

        payload = _decrypt_or_read(data, offset, frame_control, mac, bindkey)
    except _FrameError as exc:
        return None, exc.reason

    return _parse_objects(
        payload,
        counter=counter,
        now=time.time() if now is None else now,
        max_skew=max_skew,
        suspect_skew=suspect_skew,
    )


def _need(data: bytes, offset: int, length: int, reason: str = "truncated") -> None:
    if len(data) < offset + length:
        raise _FrameError(reason)


def _consume_mac(data: bytes, offset: int) -> int:
    _need(data, offset, 6)
    return offset + 6


def _consume_capability(data: bytes, offset: int, frame_control: int) -> int:
    if not frame_control & FC_HAS_CAPABILITY:
        return offset
    _need(data, offset, 1, reason="bad_frame")
    capability = data[offset]
    offset += 1
    if capability & 0x20:
        _need(data, offset, 1)
        offset += 1
    return offset


def _decrypt_or_read(
    data: bytes,
    offset: int,
    frame_control: int,
    mac: str,
    bindkey: bytes,
) -> bytes:
    if not frame_control & FC_ENCRYPTED:
        return data[offset:]

    _need(data, offset, 1 + 7)
    ciphertext = data[offset:-7]
    random3 = data[-7:-4]
    tag = data[-4:]
    nonce = bytes(reversed(mac_to_bytes(mac))) + data[2:5] + random3
    try:
        return AESCCM(bindkey, tag_length=TAG_LENGTH).decrypt(nonce, ciphertext + tag, AAD)
    except InvalidTag as exc:
        raise _FrameError("decrypt_failed") from exc
    except ValueError as exc:  # nonce/数据长度异常
        raise _FrameError("bad_frame") from exc


def _parse_objects(
    payload: bytes,
    *,
    counter: int,
    now: float,
    max_skew: int,
    suspect_skew: int,
) -> tuple[Measurement | None, str]:
    cursor = 0
    size = len(payload)
    while cursor + 3 <= size:
        object_id, length = struct.unpack_from("<HB", payload, cursor)
        end = cursor + 3 + length
        if end > size:
            return None, "bad_frame"
        if object_id == OBJECT_ID_BLOOD_PRESSURE:
            if length != BP_OBJECT_LENGTH:
                return None, "bad_frame"
            value = payload[cursor + 3 : end]
            return _measurement_from_value(
                value,
                counter=counter,
                now=now,
                max_skew=max_skew,
                suspect_skew=suspect_skew,
            )
        cursor = end
    return None, "no_measurement"


def _measurement_from_value(
    value: bytes,
    *,
    counter: int,
    now: float,
    max_skew: int,
    suspect_skew: int,
) -> tuple[Measurement | None, str]:
    raw_time, systolic, diastolic, heart_rate, flags = struct.unpack("<IHBBB", value)
    if not (
        40 <= systolic <= 260
        and 20 <= diastolic <= 200
        and 20 <= heart_rate <= 250
        and systolic > diastolic
    ):
        return None, "bad_values"

    measured_at, suspect, reason = _resolve_time(
        raw_time, now=now, max_skew=max_skew, suspect_skew=suspect_skew
    )
    if measured_at is None:
        return None, reason

    return (
        Measurement(
            measured_at=measured_at,
            systolic=systolic,
            diastolic=diastolic,
            heart_rate=heart_rate,
            flags=flags,
            counter=counter,
            object_id=OBJECT_ID_BLOOD_PRESSURE,
            time_suspect=suspect,
        ),
        "ok",
    )


# ---------------------------------------------------------------- 记录


def record_id(measurement: Measurement) -> str:
    """文件名/id：确定性命名 —— 同一次测量永远得到同一个名字（天然幂等）。"""
    stamp = measurement.measured_at.strftime("%Y%m%d-%H%M%S")
    return f"bp-{stamp}-{measurement.systolic}-{measurement.diastolic}-{measurement.heart_rate}"


def build_record(
    measurement: Measurement,
    *,
    device_mac: str = "",
    rssi: int | None = None,
    source: str = "bpx1-beacon",
    captured_at: datetime | None = None,
) -> dict:
    """生成落盘 JSON。

    `time` 保持 v0.1 的"本地时间、无时区后缀"写法（快捷指令已在用，不改动）；
    `time_iso` / `time_utc` 是带时区的规范表示，新搭建的快捷指令建议用后者。
    """
    captured = (captured_at or datetime.now().astimezone()).isoformat(timespec="seconds")
    measured = measurement.measured_at
    return {
        "schema": 1,
        "id": measurement.id,
        "time": measured.strftime("%Y-%m-%dT%H:%M:%S"),
        "time_iso": measured.isoformat(timespec="seconds"),
        "time_utc": measured.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "systolic": measurement.systolic,
        "diastolic": measurement.diastolic,
        "heart_rate": measurement.heart_rate,
        "flags": measurement.flags,
        "irregular_hint": bool(measurement.flags & FLAG_IRREGULAR_HINT),
        "movement_hint": bool(measurement.flags & FLAG_MOVEMENT_HINT),
        "time_suspect": measurement.time_suspect,
        "source": source,
        "device": device_mac or None,
        "rssi": rssi,
        "object_id": measurement.object_id,
        "captured_at": captured,
    }
