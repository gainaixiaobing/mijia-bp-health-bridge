"""协议层测试：帧构造 → 解析 → 记录。不需要蓝牙，也不需要真机。"""

from __future__ import annotations

import struct
from datetime import datetime

import pytest

from bpx1_protocol import (
    OBJECT_ID_BLOOD_PRESSURE,
    Measurement,
    build_record,
    convert_device_time,
    normalize_mac,
    parse_bindkey,
    parse_frame,
    record_id,
)

from _frames import BINDKEY, MAC, MEASURED_AT, NOW, OTHER_KEY, bp_payload, build_frame


# ---------------------------------------------------------------- 主路径


def test_roundtrip_measurement() -> None:
    measurement, reason = parse_frame(build_frame(bp_payload()), bindkey=BINDKEY, device_mac=MAC, now=NOW)

    assert reason == "ok"
    assert measurement is not None
    assert (measurement.systolic, measurement.diastolic, measurement.heart_rate) == (115, 91, 69)
    assert measurement.id == "bp-20261010-163428-115-91-69"
    assert measurement.time_suspect is False


def test_embedded_mac_frame_needs_no_config() -> None:
    frame = build_frame(bp_payload(), embed_mac=True)
    measurement, reason = parse_frame(frame, bindkey=BINDKEY, now=NOW)

    assert reason == "ok"
    assert measurement is not None


def test_accepts_mac_variants() -> None:
    for mac in ("8cd0b2aabbcc", "8C-D0-B2-AA-BB-CC", "8C:D0:B2:AA:BB:CC"):
        measurement, reason = parse_frame(build_frame(bp_payload()), bindkey=BINDKEY, device_mac=mac, now=NOW)
        assert reason == "ok", mac
        assert measurement is not None


# ---------------------------------------------------------------- 时间基准


def test_documented_sample_timestamp_renders_in_local_time() -> None:
    """文档样例：0x325cb514 应渲染成 2026-10-10 16:34:28（+08:00）。

    这条同时把"时间基准是 2000-01-01 UTC，不是 UTC+8"钉死：
    若按 UTC+8 基准实现，会得到 08:34:28。
    """
    rendered = convert_device_time(0x325CB514)

    assert rendered.isoformat() == "2026-10-10T16:34:28+08:00"


def test_stale_device_clock_is_rejected() -> None:
    payload = bp_payload(when=datetime.fromisoformat("2000-01-01T08:00:01+08:00"))
    measurement, reason = parse_frame(build_frame(payload), bindkey=BINDKEY, device_mac=MAC, now=NOW)

    assert measurement is None
    assert reason == "time_suspect"


def test_small_clock_skew_is_flagged_not_dropped() -> None:
    now = NOW + 600  # 设备慢 10 分钟
    measurement, reason = parse_frame(build_frame(bp_payload()), bindkey=BINDKEY, device_mac=MAC, now=now)

    assert reason == "ok"
    assert measurement is not None
    assert measurement.time_suspect is True


# ---------------------------------------------------------------- 畸形帧


def test_documented_sample_frame_with_wrong_key_fails_cleanly() -> None:
    """文档里的实测帧：用错 key 应返回 decrypt_failed，而不是抛异常。"""
    frame = bytes.fromhex("4858" "b81f" "02" "a13836970d6cb77a78580bb0" "740000" "f6038438")

    measurement, reason = parse_frame(frame, bindkey=OTHER_KEY, device_mac=MAC)

    assert measurement is None
    assert reason == "decrypt_failed"


def test_capability_bit_without_byte_is_bad_frame() -> None:
    """v0.1 在这里会抛 IndexError。"""
    frame = bytes.fromhex("6850" "b81f" "02")  # payload|capability|encrypted，但没有后续字节

    measurement, reason = parse_frame(frame, bindkey=BINDKEY, device_mac=MAC)

    assert measurement is None
    assert reason == "bad_frame"


def test_foreign_nine_byte_object_is_not_a_measurement() -> None:
    """v0.1 用"长度 == 9"识别血压对象，会把任何 9 字节对象误读成测量。"""
    payload = struct.pack("<HB", 0x0000, 9) + bytes.fromhex("14b55c3273005b4500")

    measurement, reason = parse_frame(build_frame(payload), bindkey=BINDKEY, device_mac=MAC, now=NOW)

    assert measurement is None
    assert reason == "no_measurement"


def test_blood_pressure_object_with_bad_length() -> None:
    payload = struct.pack("<HB", OBJECT_ID_BLOOD_PRESSURE, 8) + bytes(8)

    measurement, reason = parse_frame(build_frame(payload), bindkey=BINDKEY, device_mac=MAC, now=NOW)

    assert measurement is None
    assert reason == "bad_frame"


def test_unsupported_protocol_version() -> None:
    measurement, reason = parse_frame(
        build_frame(bp_payload(), version=4), bindkey=BINDKEY, device_mac=MAC, now=NOW
    )

    assert measurement is None
    assert reason == "unsupported_version"


def test_heartbeat_frame() -> None:
    frame = bytearray(build_frame(bp_payload()))
    frame[0] &= ~0x40  # 清掉"含载荷"位
    measurement, reason = parse_frame(bytes(frame), bindkey=BINDKEY, device_mac=MAC, now=NOW)

    assert measurement is None
    assert reason == "heartbeat"


def test_missing_mac_configuration() -> None:
    measurement, reason = parse_frame(build_frame(bp_payload()), bindkey=BINDKEY, device_mac="", now=NOW)

    assert measurement is None
    assert reason == "no_mac"


def test_tampered_tag_fails_authentication() -> None:
    frame = bytearray(build_frame(bp_payload()))
    frame[-1] ^= 0xFF

    measurement, reason = parse_frame(bytes(frame), bindkey=BINDKEY, device_mac=MAC, now=NOW)

    assert measurement is None
    assert reason == "decrypt_failed"


def test_truncated_encrypted_frame() -> None:
    frame = build_frame(bp_payload())[:7]

    measurement, reason = parse_frame(frame, bindkey=BINDKEY, device_mac=MAC, now=NOW)

    assert measurement is None
    assert reason == "truncated"


def test_short_frame() -> None:
    measurement, reason = parse_frame(b"\x48\x58", bindkey=BINDKEY, device_mac=MAC)

    assert measurement is None
    assert reason == "short"


def test_other_product_id() -> None:
    measurement, reason = parse_frame(
        build_frame(bp_payload(), product_id=1234), bindkey=BINDKEY, device_mac=MAC, now=NOW
    )

    assert measurement is None
    assert reason == "other_pid"


def test_impossible_values_are_rejected() -> None:
    measurement, reason = parse_frame(
        build_frame(bp_payload(systolic=90, diastolic=95)), bindkey=BINDKEY, device_mac=MAC, now=NOW
    )

    assert measurement is None
    assert reason == "bad_values"


# ---------------------------------------------------------------- 记录


def test_record_shape() -> None:
    measurement, _ = parse_frame(build_frame(bp_payload(flags=0x03)), bindkey=BINDKEY, device_mac=MAC, now=NOW)
    assert measurement is not None

    record = build_record(measurement, device_mac=MAC, rssi=-61, captured_at=datetime.fromisoformat("2026-10-10T16:35:00+08:00"))

    assert record["schema"] == 1
    assert record["id"] == "bp-20261010-163428-115-91-69"
    assert record["time"] == "2026-10-10T16:34:28"
    assert record["time_iso"] == "2026-10-10T16:34:28+08:00"
    assert record["time_utc"] == "2026-10-10T08:34:28Z"
    assert record["device"] == MAC
    assert record["rssi"] == -61
    assert record["object_id"] == OBJECT_ID_BLOOD_PRESSURE
    assert record["irregular_hint"] is True
    assert record["movement_hint"] is True
    assert record["time_suspect"] is False
    assert record["captured_at"].startswith("2026-10-10T16:35:00")


def test_record_id_is_stable_and_filesystem_safe() -> None:
    measurement = Measurement(
        measured_at=MEASURED_AT,
        systolic=115,
        diastolic=91,
        heart_rate=69,
        flags=0,
        counter=1,
        object_id=OBJECT_ID_BLOOD_PRESSURE,
    )

    assert record_id(measurement) == measurement.id
    assert record_id(measurement) == record_id(measurement)
    assert "/" not in measurement.id and ":" not in measurement.id


# ---------------------------------------------------------------- 参数


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("8C:D0:B2:AA:BB:CC", "8C:D0:B2:AA:BB:CC"),
        ("8cd0b2aabbcc", "8C:D0:B2:AA:BB:CC"),
        ("8C-D0-B2-AA-BB-CC", "8C:D0:B2:AA:BB:CC"),
        (" 8c:d0:b2:aa:bb:cc ", "8C:D0:B2:AA:BB:CC"),
    ],
)
def test_normalize_mac(raw: str, expected: str) -> None:
    assert normalize_mac(raw) == expected


@pytest.mark.parametrize("raw", ["", "8C:D0:B2", "8C:D0:B2:AA:BB:CG", "8C:D0:B2:AA:BB:CC:DD"])
def test_normalize_mac_rejects_garbage(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_mac(raw)


def test_parse_bindkey() -> None:
    assert parse_bindkey("00112233445566778899aabbccddeeff") == BINDKEY
    assert parse_bindkey("  00112233445566778899AABBCCDDEEFF  ") == BINDKEY


@pytest.mark.parametrize("raw", ["", "abc", "00" * 15, "zz" * 16])
def test_parse_bindkey_rejects_garbage(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_bindkey(raw)
