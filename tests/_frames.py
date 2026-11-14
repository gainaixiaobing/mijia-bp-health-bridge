"""构造合法的 FE95 帧（测试用），不依赖真机。"""

from __future__ import annotations

import struct
from datetime import datetime

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from bpx1_protocol import EPOCH_2000_UTC, OBJECT_ID_BLOOD_PRESSURE

BINDKEY = bytes.fromhex("00112233445566778899aabbccddeeff")
OTHER_KEY = bytes.fromhex("ffeeddccbbaa99887766554433221100")
MAC = "8C:D0:B2:AA:BB:CC"
MEASURED_AT = datetime.fromisoformat("2026-10-10T16:34:28+08:00")
NOW = MEASURED_AT.timestamp()


def mac_bytes(mac: str = MAC) -> bytes:
    return bytes(int(part, 16) for part in mac.split(":"))


def raw_time(when: datetime = MEASURED_AT) -> int:
    return int(when.timestamp()) - EPOCH_2000_UTC


def bp_payload(
    systolic: int = 115,
    diastolic: int = 91,
    heart_rate: int = 69,
    when: datetime = MEASURED_AT,
    flags: int = 0,
) -> bytes:
    return struct.pack("<HB", OBJECT_ID_BLOOD_PRESSURE, 9) + struct.pack(
        "<IHBBB", raw_time(when), systolic, diastolic, heart_rate, flags
    )


def build_frame(
    payload: bytes,
    *,
    bindkey: bytes = BINDKEY,
    mac: str = MAC,
    counter: int = 2,
    rand3: bytes = b"\x74\x00\x00",
    encrypt: bool = True,
    embed_mac: bool = False,
    capability: int | None = None,
    version: int = 5,
    product_id: int = 8120,
) -> bytes:
    frame_control = (version << 12) | 0x40
    if encrypt:
        frame_control |= 0x08
    if embed_mac:
        frame_control |= 0x10
    if capability is not None:
        frame_control |= 0x20

    body = bytearray(struct.pack("<HHB", frame_control, product_id, counter))
    if embed_mac:
        body += bytes(reversed(mac_bytes(mac)))
    if capability is not None:
        body.append(capability)
        if capability & 0x20:
            body.append(0x00)

    if encrypt:
        nonce = bytes(reversed(mac_bytes(mac))) + struct.pack("<HB", product_id, counter) + rand3
        sealed = AESCCM(bindkey, tag_length=4).encrypt(nonce, payload, b"\x11")
        body += sealed[:-4] + rand3 + sealed[-4:]
    else:
        body += payload
    return bytes(body)
