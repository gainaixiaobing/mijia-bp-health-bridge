#!/usr/bin/env python3
"""从你自己的小米云账号获取 BLE 设备 bindkey（MiBeacon 解密密钥）

用法:
    python3 get_beaconkey.py <设备DID> [--auth auth.json]

依赖:
    pip install mijiaAPI        # https://github.com/Do1e/mijia-api
    首次使用需按 mijiaAPI 文档完成一次登录（生成 auth.json）

说明:
    - 设备 DID 在 米家 App → 设备 → 右上角「…」→ 设备信息 中查看（形如 blt.3.xxxxxx）
    - 接口 data 中必须带 "pdid": 1，否则小米服务器会返回 -8 invalid req
    - 返回的 beaconkey 即 16 字节 bindkey（32位 hex）
"""
import json
import sys


def main():
    args = sys.argv[1:]
    did = None
    auth = "auth.json"
    i = 0
    while i < len(args):
        if args[i] == "--auth" and i + 1 < len(args):
            auth = args[i + 1]
            i += 2
            continue
        if did is None:
            did = args[i]
        i += 1
    if not did:
        print(__doc__)
        sys.exit(1)

    from mijiaAPI import mijiaAPI  # noqa: E402

    api = mijiaAPI(auth)
    r = api._request("/v2/device/blt_get_beaconkey", {"did": did, "pdid": 1})
    print(json.dumps(r, ensure_ascii=False, indent=2))
    key = None
    if isinstance(r, dict):
        key = r.get("beaconkey")
    if key:
        print(f"\n✓ bindkey: {key}")
        print("提示: 将其写入仓库根目录 bp_bindkey.hex，供扫描脚本使用（该文件已 gitignore）")


if __name__ == "__main__":
    main()
