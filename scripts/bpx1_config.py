#!/usr/bin/env python3
"""运行配置：路径、bindkey、设备 MAC，以及"装没装对"的前置条件检查。

所有可调项都走环境变量，避免把密钥写进代码或 launchd 参数里：

    BPX1_BINDKEY       32 位十六进制 bindkey（优先）
    BPX1_BINDKEY_FILE  bindkey 文件路径（默认 <repo>/bp_bindkey.hex）
    BPX1_MAC           设备 MAC，接受 8C:D0:B2:AA:BB:CC / 8cd0b2aabbcc 等写法
    BPX1_INBOX         inbox 目录（不设则用 iCloud Drive/bp-health/inbox）
    BPX1_DONE          done 目录（默认 inbox 同级 done）
    BPX1_STAGING       staging 目录（默认 inbox 同级 staging）
    BPX1_DATA_DIR      日志/流水目录（默认 <repo>/out）
    BPX1_MAX_TIME_SKEW 设备时钟允许偏差秒数（默认 86400）
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from bpx1_protocol import normalize_mac, parse_bindkey

ROOT = Path(__file__).resolve().parent.parent
ICLOUD_ROOT = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs"
DEFAULT_BP_ROOT = ICLOUD_ROOT / "bp-health"
DEFAULT_BINDKEY_FILE = ROOT / "bp_bindkey.hex"


@dataclass(frozen=True)
class Paths:
    inbox: Path
    done: Path
    staging: Path
    data_dir: Path
    uses_icloud_default: bool = False

    @property
    def journal(self) -> Path:
        return self.data_dir / "journal.jsonl"

    @property
    def daemon_log(self) -> Path:
        return self.data_dir / "daemon.log"

    @property
    def beacon_log(self) -> Path:
        return self.data_dir / "beacon_log.jsonl"


def load_paths(environ: dict[str, str] | None = None) -> Paths:
    env = os.environ if environ is None else environ
    inbox_env = (env.get("BPX1_INBOX") or "").strip()

    if inbox_env:
        inbox = Path(inbox_env).expanduser()
        base = inbox.parent
        done = Path(env.get("BPX1_DONE") or base / "done").expanduser()
        staging = Path(env.get("BPX1_STAGING") or base / "staging").expanduser()
        uses_icloud = False
    else:
        inbox = DEFAULT_BP_ROOT / "inbox"
        done = DEFAULT_BP_ROOT / "done"
        staging = DEFAULT_BP_ROOT / "staging"
        uses_icloud = True

    data_dir = Path(env.get("BPX1_DATA_DIR") or ROOT / "out").expanduser()
    return Paths(
        inbox=inbox,
        done=done,
        staging=staging,
        data_dir=data_dir,
        uses_icloud_default=uses_icloud,
    )


@dataclass(frozen=True)
class Bindkey:
    value: bytes | None
    source: str
    path: Path | None = None
    error: str | None = None
    insecure: bool = False


def load_bindkey(environ: dict[str, str] | None = None) -> Bindkey:
    env = os.environ if environ is None else environ
    raw = (env.get("BPX1_BINDKEY") or "").strip()
    if raw:
        try:
            return Bindkey(parse_bindkey(raw), "环境变量 BPX1_BINDKEY")
        except ValueError as exc:
            return Bindkey(None, "环境变量 BPX1_BINDKEY", error=str(exc))

    path = Path(env.get("BPX1_BINDKEY_FILE") or DEFAULT_BINDKEY_FILE).expanduser()
    if not path.exists():
        return Bindkey(
            None,
            f"{path}",
            path=path,
            error=f"未找到 bindkey：{path} 不存在（用 scripts/get_beaconkey.py 获取后写入）",
        )

    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return Bindkey(None, str(path), path=path, error=f"读取 {path} 失败：{exc}")

    try:
        value = parse_bindkey(text)
    except ValueError as exc:
        return Bindkey(None, str(path), path=path, error=f"{path} 内容无效：{exc}")

    mode = stat.S_IMODE(path.stat().st_mode)
    insecure = bool(mode & 0o077)
    return Bindkey(value, str(path), path=path, insecure=insecure)


def load_device_mac(environ: dict[str, str] | None = None) -> tuple[str | None, str | None]:
    env = os.environ if environ is None else environ
    raw = (env.get("BPX1_MAC") or "").strip()
    if not raw:
        return None, None
    try:
        return normalize_mac(raw), None
    except ValueError as exc:
        return None, str(exc)


def load_max_time_skew(environ: dict[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    raw = (env.get("BPX1_MAX_TIME_SKEW") or "").strip()
    if not raw:
        return 24 * 3600
    try:
        value = int(raw)
    except ValueError:
        return 24 * 3600
    return max(60, value)


@dataclass(frozen=True)
class Issue:
    level: str  # "error" | "warn"
    message: str
    hint: str = ""


def check_environment(
    paths: Paths,
    bindkey: Bindkey,
    mac: str | None,
    mac_error: str | None = None,
    *,
    need_icloud: bool = True,
) -> list[Issue]:
    """启动前的前置条件检查。error 级应阻止启动，warn 级只提示。

    need_icloud=False 用于只做扫描/解码的场景（bpx1_scan.py），它不写 iCloud。
    """
    issues: list[Issue] = []

    if need_icloud and paths.uses_icloud_default and not ICLOUD_ROOT.exists():
        issues.append(
            Issue(
                "error",
                f"未检测到 iCloud Drive：{ICLOUD_ROOT}",
                "打开「系统设置 → Apple 账户 → iCloud → iCloud 云盘」，"
                "或设置 BPX1_INBOX 指向自定义目录。",
            )
        )
    elif need_icloud and not paths.inbox.parent.exists():
        issues.append(
            Issue(
                "warn",
                f"inbox 的上级目录不存在，将在启动时创建：{paths.inbox.parent}",
                "若是自定义路径，请确认它真的会被同步到手机。",
            )
        )

    if bindkey.error:
        issues.append(Issue("error", bindkey.error, "先运行 scripts/get_beaconkey.py 取得 bindkey。"))
    if bindkey.insecure:
        mode = oct(stat.S_IMODE(Path(bindkey.path).stat().st_mode))
        issues.append(
            Issue(
                "warn",
                f"bindkey 文件权限过宽（{mode}）：{bindkey.path}",
                f"执行 chmod 600 {bindkey.path}，避免同机其他用户读取。",
            )
        )
    if mac_error:
        issues.append(Issue("error", f"BPX1_MAC 无效：{mac_error}", "示例：8C:D0:B2:AA:BB:CC"))
    if not mac and not mac_error:
        issues.append(
            Issue(
                "warn",
                "未配置 BPX1_MAC",
                "macOS 上广播帧默认不含 MAC。若日志出现 no_mac，请设置 BPX1_MAC。",
            )
        )

    return issues


def ensure_dirs(paths: Paths) -> None:
    for directory in (paths.inbox, paths.done, paths.staging, paths.data_dir):
        directory.mkdir(parents=True, exist_ok=True)


def describe(paths: Paths, bindkey_source: str, mac: str | None) -> str:
    return (
        f"inbox={paths.inbox} done={paths.done} staging={paths.staging} "
        f"data={paths.data_dir} mac={mac or '未配置'} "
        f"bindkey={bindkey_source or '未配置'}"
    )


__all__ = [
    "ROOT",
    "ICLOUD_ROOT",
    "DEFAULT_BP_ROOT",
    "DEFAULT_BINDKEY_FILE",
    "Paths",
    "Bindkey",
    "Issue",
    "load_paths",
    "load_bindkey",
    "load_device_mac",
    "load_max_time_skew",
    "check_environment",
    "ensure_dirs",
    "describe",
]
