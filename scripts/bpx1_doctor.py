#!/usr/bin/env python3
"""环境自检：在"为什么收不到数据"之前，先把能查的都查一遍。

用法:
    python3 scripts/bpx1_doctor.py
    python3 scripts/bpx1_doctor.py --json

退出码: 0 = 没有致命问题（可以有 ⚠️）；1 = 有 ✘ 级问题。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bpx1_config import ICLOUD_ROOT, ROOT, load_bindkey, load_device_mac, load_paths
from bpx1_store import Store

LAUNCH_AGENT = Path.home() / "Library/LaunchAgents/com.mijia.bpx1scan.plist"
LABEL = "com.mijia.bpx1scan"

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    hint: str = ""


def check_python() -> Check:
    version = sys.version_info
    detail = f"{version.major}.{version.minor}.{version.micro} ({platform.platform()})"
    if version < (3, 10):
        return Check("Python 版本", FAIL, detail, "需要 Python ≥ 3.10")
    return Check("Python 版本", OK, detail)


def check_module(module: str, package: str) -> Check:
    if importlib.util.find_spec(module) is None:
        return Check(f"依赖 {module}", FAIL, "未安装", f"pip install {package}")
    return Check(f"依赖 {module}", OK, "已安装")


def check_bindkey() -> tuple[Check, object]:
    bindkey = load_bindkey()
    if bindkey.error:
        return Check("bindkey", FAIL, bindkey.error, "运行 scripts/get_beaconkey.py --save"), bindkey
    mode = oct(bindkey.path.stat().st_mode & 0o777) if bindkey.path else "env"
    if bindkey.insecure:
        return (
            Check("bindkey", WARN, f"来源 {bindkey.source}，文件权限 {mode}", f"chmod 600 {bindkey.path}"),
            bindkey,
        )
    return Check("bindkey", OK, f"来源 {bindkey.source}，权限 {mode}"), bindkey


def check_mac() -> Check:
    mac, error = load_device_mac()
    if error:
        return Check("设备 MAC", FAIL, error, "示例：BPX1_MAC=8C:D0:B2:AA:BB:CC")
    if not mac:
        return Check(
            "设备 MAC",
            WARN,
            "未配置",
            "macOS 广播帧不含 MAC，未配置时无法解密。设置 BPX1_MAC 或写进 plist 的 EnvironmentVariables。",
        )
    return Check("设备 MAC", OK, mac)


def check_paths() -> tuple[list[Check], object]:
    paths = load_paths()
    checks: list[Check] = []
    if paths.uses_icloud_default:
        checks.append(
            Check(
                "iCloud Drive",
                OK if ICLOUD_ROOT.exists() else FAIL,
                str(ICLOUD_ROOT) + ("" if ICLOUD_ROOT.exists() else "（不存在）"),
                "打开系统设置里的 iCloud 云盘，或设置 BPX1_INBOX 用自定义目录。",
            )
        )
    for label, directory in (("inbox", paths.inbox), ("done", paths.done), ("staging", paths.staging)):
        exists = directory.exists()
        writable = os.access(directory, os.W_OK) if exists else False
        checks.append(
            Check(
                f"目录 {label}",
                OK if exists and writable else (WARN if exists else FAIL),
                f"{directory}" + ("" if exists else "（尚未创建，安装脚本会建）"),
                "运行 scripts/install.sh 或手动 mkdir -p。",
            )
        )
    return checks, paths


def check_launchd() -> Check:
    if sys.platform != "darwin":
        return Check("launchd 服务", WARN, "非 macOS，跳过", "")
    if not LAUNCH_AGENT.exists():
        return Check("launchd 服务", WARN, "未安装守护进程", "运行 scripts/install.sh")
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("launchd 服务", WARN, f"查询失败：{exc}", "")
    if result.returncode != 0:
        return Check("launchd 服务", WARN, "已安装但未加载", f"launchctl bootstrap gui/$(id -u) {LAUNCH_AGENT}")
    return Check("launchd 服务", OK, f"运行中（{LABEL}）")


def check_activity(paths) -> list[Check]:
    checks: list[Check] = []
    store = Store(paths)
    journal = paths.journal
    if not journal.exists():
        checks.append(Check("采集流水", WARN, f"{journal} 不存在", "守护进程还没跑过，或 DATA_DIR 设错。"))
    else:
        records = store.journal_tail(limit=1)
        if records:
            latest = records[-1]
            captured = str(latest.get("captured_at") or latest.get("time") or "未知时间")
            checks.append(
                Check(
                    "最近一次采集",
                    OK,
                    f"{captured} 的记录：{latest.get('systolic')}/"
                    f"{latest.get('diastolic')} mmHg",
                )
            )
        else:
            checks.append(Check("采集流水", WARN, f"{journal} 存在但没有可用记录", ""))

    inbox_files = sorted(paths.inbox.glob("bp-*.json")) if paths.inbox.exists() else []
    done_files = sorted(paths.done.glob("bp-*.json")) if paths.done.exists() else []
    checks.append(
        Check(
            "待导入 / 已导入",
            OK,
            f"inbox {len(inbox_files)} 条，done {len(done_files)} 条",
            "inbox 长期堆积说明手机端快捷指令没跑或没有写健康权限。",
        )
    )

    if paths.daemon_log.exists():
        tail = paths.daemon_log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        errors = [line for line in tail if " ERROR " in f" {line} "]
        if errors:
            checks.append(Check("守护进程日志", WARN, errors[-1][:160], f"完整日志：{paths.daemon_log}"))
        else:
            checks.append(Check("守护进程日志", OK, f"最近 {len(tail)} 行无 ERROR"))
    else:
        checks.append(Check("守护进程日志", WARN, f"{paths.daemon_log} 不存在", ""))
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BPX1 环境自检")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    bindkey_check, _bindkey = check_bindkey()
    path_checks, paths = check_paths()

    checks: list[Check] = [check_python()]
    checks.append(check_module("cryptography", "cryptography"))
    checks.append(check_module("bleak", "bleak"))
    checks.append(bindkey_check)
    checks.append(check_mac())
    checks.extend(path_checks)
    checks.extend(check_activity(paths))
    checks.append(check_launchd())

    failed = [check for check in checks if check.status == FAIL]
    warned = [check for check in checks if check.status == WARN]

    if args.json:
        print(
            json.dumps(
                {
                    "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "root": str(ROOT),
                    "checks": [check.__dict__ for check in checks],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        icons = {OK: "✓", WARN: "⚠", FAIL: "✗"}
        print(f"BPX1 自检（{ROOT}）\n")
        for check in checks:
            print(f"  {icons[check.status]} {check.name}: {check.detail}")
            if check.hint and check.status != OK:
                print(f"      → {check.hint}")
        print(f"\n结果：{len(checks) - len(failed) - len(warned)} 项正常，{len(warned)} 项提示，{len(failed)} 项失败。")
        if failed:
            print("先解决 ✗ 项，再看 docs/README 的排查章节。")
        elif warned:
            print("没有致命问题；⚠ 项按提示处理即可。")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
