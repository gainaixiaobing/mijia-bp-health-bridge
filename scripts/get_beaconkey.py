#!/usr/bin/env python3
"""从自己的小米云账号获取 BLE 设备 bindkey（MiBeacon 解密密钥）。

用法:
    python3 scripts/get_beaconkey.py blt.3.xxxxxx
    python3 scripts/get_beaconkey.py blt.3.xxxxxx --save
    python3 scripts/get_beaconkey.py blt.3.xxxxxx --save ~/.config/bpx1/bindkey

依赖:
    pip install mijiaAPI        # https://github.com/Do1e/mijia-api
    首次使用需按 mijiaAPI 文档完成一次登录（生成 auth.json）

说明:
    - 设备 DID 在 米家 App → 设备 → 右上角「…」→ 设备信息 查看（形如 blt.3.xxxxxx）
    - 接口 data 中必须带 "pdid": 1，否则小米服务器会返回 -8 invalid req
    - 返回的 beaconkey 即 16 字节 bindkey（32 位 hex）
    - --save 会以 0600 权限写入文件，避免同机其他用户读取

注意:
    blt_get_beaconkey 是未公开接口，随时可能变更或失效；本项目封了一层薄适配，
    上游改动时只需要改 fetch_beaconkey()。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from bpx1_config import DEFAULT_BINDKEY_FILE, ROOT
from bpx1_protocol import parse_bindkey


def fetch_beaconkey(did: str, auth_path: str) -> dict:
    """唯一一处对 mijiaAPI 私有方法的依赖，方便上游变更时替换。"""
    try:
        from mijiaAPI import mijiaAPI
    except ImportError as exc:
        raise SystemExit(
            "缺少依赖 mijiaAPI。先执行：pip install mijiaAPI\n"
            "（其登录/鉴权方式见 https://github.com/Do1e/mijia-api）"
        ) from exc

    api = mijiaAPI(auth_path)
    return api._request("/v2/device/blt_get_beaconkey", {"did": did, "pdid": 1})


def save_key(key: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(0o600)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(key + "\n")
    path.chmod(0o600)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从小米云获取设备 bindkey")
    parser.add_argument("did", help="设备 DID（米家 App → 设备信息，形如 blt.3.xxxxxx）")
    parser.add_argument("--auth", default="auth.json", help="mijiaAPI 的登录态文件（默认 auth.json）")
    parser.add_argument(
        "--save",
        nargs="?",
        const=str(DEFAULT_BINDKEY_FILE),
        default=None,
        help=f"把 bindkey 写入文件（默认 {DEFAULT_BINDKEY_FILE}）",
    )
    args = parser.parse_args(argv)

    response = fetch_beaconkey(args.did, args.auth)
    print(json.dumps(response, ensure_ascii=False, indent=2))

    key = response.get("beaconkey") if isinstance(response, dict) else None
    if not key:
        print("\n没有拿到 beaconkey：确认 DID 正确、账号下确实绑定了该设备。", file=sys.stderr)
        return 1

    try:
        parse_bindkey(str(key))
    except ValueError as exc:
        print(f"\n返回的 beaconkey 不是合法的 32 位 hex：{exc}", file=sys.stderr)
        return 1

    print(f"\n✓ bindkey: {key}")
    if args.save:
        saved = save_key(str(key), Path(args.save))
        print(f"已写入 {saved}（权限 0600）")
    else:
        print(f"提示：加 --save 可直接写入 {DEFAULT_BINDKEY_FILE}（已 gitignore）")
        print(f"仓库根目录：{ROOT}")
    print("提醒：bindkey 等价于该设备的解密密钥，别贴到聊天/issue 里，也别放进 iCloud。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
