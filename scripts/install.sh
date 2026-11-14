#!/usr/bin/env bash
# 一键安装：建 venv、装依赖、建 iCloud 目录、渲染并加载 launchd 守护进程。
#
# 用法:
#   scripts/install.sh                    # 安装/更新
#   BPX1_MAC=8C:D0:B2:AA:BB:CC scripts/install.sh
#   scripts/install.sh --dry-run          # 只打印将要做什么
#   scripts/install.sh --uninstall        # 卸载守护进程（保留数据）
#
# 设计约束:
#   - 幂等：重复执行等于"更新"，不会重复加载；
#   - 可预演：--dry-run 不写任何一个字节；
#   - 不猜密钥：bindkey 缺失只提示，不代填。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.mijia.bpx1scan"
TEMPLATE="$ROOT/launchd/$LABEL.plist.template"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
VENV="$ROOT/.venv"
VENV_PY="$VENV/bin/python"
DAEMON="$ROOT/scripts/bpx1_daemon.py"
LAUNCHD_LOG="$ROOT/out/launchd.log"

DRY_RUN=0
UNINSTALL=0
PYTHON_BIN="${PYTHON_BIN:-python3}"

usage() {
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --python) shift; PYTHON_BIN="${1:-}" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数: $1（用 --help 查看用法）" >&2; exit 2 ;;
    esac
    shift
done

say() { printf '\n== %s ==\n' "$*"; }
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

if [ "$(uname -s)" != "Darwin" ]; then
    echo "本项目依赖 macOS 的 CoreBluetooth 与 launchd（当前: $(uname -s)）。" >&2
    exit 2
fi

# ---------------------------------------------------------------- 卸载

if [ "$UNINSTALL" -eq 1 ]; then
    say "卸载 $LABEL"
    run launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    if [ -f "$PLIST" ]; then
        run rm -f "$PLIST"
        echo "已移除 $PLIST"
    else
        echo "未安装（$PLIST 不存在）"
    fi
    echo "数据目录（iCloud inbox/done、out/、bp_bindkey.hex）已保留，需要时手动删除。"
    exit 0
fi

# ---------------------------------------------------------------- 前置检查

say "检查 Python"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "找不到 $PYTHON_BIN。安装 Python 3.10+ 后重试，或用 --python 指定解释器。" >&2
    exit 2
fi
version="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$version" in
    3.1[0-9]|3.[2-9][0-9]) ;;
    *) echo "需要 Python ≥ 3.10，当前 $version" >&2; exit 2 ;;
esac
echo "  $PYTHON_BIN ($version)"

if [ ! -f "$TEMPLATE" ]; then
    echo "缺少模板：$TEMPLATE" >&2
    exit 2
fi

# ---------------------------------------------------------------- venv + 依赖

say "准备虚拟环境"
if [ "$DRY_RUN" -eq 0 ] && [ ! -x "$VENV_PY" ]; then
    "$PYTHON_BIN" -m venv "$VENV"
fi
run "$VENV_PY" -m pip install -q --disable-pip-version-check -r "$ROOT/requirements.txt"

# ---------------------------------------------------------------- 目录

say "准备目录"
INBOX="${BPX1_INBOX:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/bp-health/inbox}"
BP_ROOT="$(dirname "$INBOX")"
if [ -z "${BPX1_INBOX:-}" ] && [ ! -d "$HOME/Library/Mobile Documents/com~apple~CloudDocs" ]; then
    echo "警告：未检测到 iCloud Drive。数据不会同步到手机。" >&2
    echo "      打开「系统设置 → Apple 账户 → iCloud → iCloud 云盘」，或设置 BPX1_INBOX。" >&2
fi
run mkdir -p "$INBOX" "$BP_ROOT/done" "$BP_ROOT/staging" "${BPX1_DATA_DIR:-$ROOT/out}"
run touch "$LAUNCHD_LOG"

# ---------------------------------------------------------------- 密钥

say "检查 bindkey"
KEY_FILE="${BPX1_BINDKEY_FILE:-$ROOT/bp_bindkey.hex}"
if [ -f "$KEY_FILE" ]; then
    run chmod 600 "$KEY_FILE"
    echo "  已收紧权限: $KEY_FILE"
else
    echo "  还没有 bindkey："
    echo "    $VENV_PY scripts/get_beaconkey.py <设备DID> --save"
    echo "  守护进程会先启动并等待（不会反复崩溃），补上后 5 分钟内自动生效。"
fi

# ---------------------------------------------------------------- launchd

say "安装 launchd 守护"
if [ "$DRY_RUN" -eq 0 ]; then
    "$VENV_PY" - "$TEMPLATE" "$PLIST" "$VENV_PY" "$DAEMON" "$LAUNCHD_LOG" <<'PY'
import os
import pathlib
import sys

template, target, python, script, log = sys.argv[1:6]
text = pathlib.Path(template).read_text(encoding="utf-8")

wanted = [
    ("BPX1_MAC", os.environ.get("BPX1_MAC", "").strip()),
    ("BPX1_INBOX", os.environ.get("BPX1_INBOX", "").strip()),
    ("BPX1_DATA_DIR", os.environ.get("BPX1_DATA_DIR", "").strip()),
    ("BPX1_BINDKEY_FILE", os.environ.get("BPX1_BINDKEY_FILE", "").strip()),
]
env_lines = "".join(
    f"        <key>{name}</key>\n        <string>{value}</string>\n" for name, value in wanted if value
)
if env_lines:
    block = (
        "    <key>EnvironmentVariables</key>\n    <dict>\n" + env_lines + "    </dict>"
    )
    print(f"  已写入环境变量: {', '.join(name for name, value in wanted if value)}")
else:
    block = "    <!-- 未写入任何环境变量：如需 BPX1_MAC 等，重新执行 "
    block += "BPX1_MAC=... scripts/install.sh -->"

placeholders = {
    "__PYTHON__": python,
    "__SCRIPT__": script,
    "__LOG__": log,
    "__ENVIRONMENT__": block,
}
missing = [key for key in placeholders if key not in text]
if missing:
    sys.exit(f"模板缺少占位符: {', '.join(missing)}")
for placeholder, value in placeholders.items():
    text = text.replace(placeholder, value)

target = pathlib.Path(target)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(text, encoding="utf-8")
print(f"  已生成 {target}")
PY
else
    printf '  [dry-run] 渲染 %s → %s\n' "$TEMPLATE" "$PLIST"
fi

if command -v plutil >/dev/null 2>&1 && [ "$DRY_RUN" -eq 0 ]; then
    plutil -lint "$PLIST" >/dev/null
    echo "  plist 校验通过"
fi

run launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
run launchctl bootstrap "gui/$(id -u)" "$PLIST"
run launchctl kickstart -k "gui/$(id -u)/$LABEL"

# ---------------------------------------------------------------- 自检

if [ "$DRY_RUN" -eq 0 ]; then
    say "自检"
    "$VENV_PY" "$ROOT/scripts/bpx1_doctor.py" || true
fi

cat <<EOF

安装完成。接下来：
  1. 造一条测试数据，验证手机端：
       $VENV_PY scripts/bpx1_inject.py
  2. 按 docs/iPhone快捷指令-导入血压.md 建好快捷指令，然后运行它。
  3. 真机测试：量一次血压，看日志：
       tail -f ${BPX1_DATA_DIR:-$ROOT/out}/daemon.log
  4. 有异常先跑：$VENV_PY scripts/bpx1_doctor.py

EOF
