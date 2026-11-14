"""配置层测试：路径、bindkey 读取与权限、前置条件检查。"""

from __future__ import annotations

from pathlib import Path

import pytest

import bpx1_config as config
from bpx1_config import Bindkey, check_environment, load_bindkey, load_device_mac, load_paths

VALID_KEY = "00112233445566778899aabbccddeeff"


def test_default_paths_use_icloud() -> None:
    paths = load_paths({})

    assert paths.uses_icloud_default is True
    assert paths.inbox.parent.name == "bp-health"
    assert paths.done == paths.inbox.parent / "done"
    assert paths.staging == paths.inbox.parent / "staging"


def test_custom_inbox_implies_custom_layout(tmp_path: Path) -> None:
    inbox = tmp_path / "mine" / "inbox"

    paths = load_paths({"BPX1_INBOX": str(inbox)})

    assert paths.uses_icloud_default is False
    assert paths.inbox == inbox
    assert paths.done == inbox.parent / "done"
    assert paths.data_dir.name == "out"  # 未设 BPX1_DATA_DIR 时用仓库 out/


def test_bindkey_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    bindkey = load_bindkey({"BPX1_BINDKEY": VALID_KEY})

    assert bindkey.value is not None
    assert bindkey.error is None


def test_bindkey_from_file_with_tight_permissions(tmp_path: Path) -> None:
    key_file = tmp_path / "bp_bindkey.hex"
    key_file.write_text(VALID_KEY + "\n", encoding="utf-8")
    key_file.chmod(0o600)

    bindkey = load_bindkey({"BPX1_BINDKEY_FILE": str(key_file)})

    assert bindkey.value is not None
    assert bindkey.insecure is False


def test_bindkey_file_with_loose_permissions_is_flagged(tmp_path: Path) -> None:
    key_file = tmp_path / "bp_bindkey.hex"
    key_file.write_text(VALID_KEY, encoding="utf-8")
    key_file.chmod(0o644)

    bindkey = load_bindkey({"BPX1_BINDKEY_FILE": str(key_file)})

    assert bindkey.value is not None
    assert bindkey.insecure is True


def test_missing_bindkey_reports_error(tmp_path: Path) -> None:
    bindkey = load_bindkey({"BPX1_BINDKEY_FILE": str(tmp_path / "missing.hex")})

    assert bindkey.value is None
    assert bindkey.error is not None and "未找到" in bindkey.error


def test_invalid_bindkey_reports_error(tmp_path: Path) -> None:
    key_file = tmp_path / "bad.hex"
    key_file.write_text("not-a-key", encoding="utf-8")

    bindkey = load_bindkey({"BPX1_BINDKEY_FILE": str(key_file)})

    assert bindkey.value is None
    assert bindkey.error is not None


def test_device_mac_is_normalized() -> None:
    mac, error = load_device_mac({"BPX1_MAC": "8cd0b2aabbcc"})

    assert error is None
    assert mac == "8C:D0:B2:AA:BB:CC"


def test_device_mac_rejects_garbage() -> None:
    mac, error = load_device_mac({"BPX1_MAC": "8C:D0:B2"})

    assert mac is None
    assert error is not None


def test_missing_icloud_drive_is_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "ICLOUD_ROOT", tmp_path / "no-icloud")
    paths = load_paths({})

    issues = check_environment(paths, Bindkey(b"0" * 16, "test"), "8C:D0:B2:AA:BB:CC")

    assert any(issue.level == "error" and "iCloud" in issue.message for issue in issues)


def test_loose_key_permission_emits_warning(tmp_path: Path) -> None:
    key_file = tmp_path / "bp_bindkey.hex"
    key_file.write_text(VALID_KEY, encoding="utf-8")
    key_file.chmod(0o644)
    bindkey = load_bindkey({"BPX1_BINDKEY_FILE": str(key_file)})
    paths = load_paths({"BPX1_INBOX": str(tmp_path / "inbox")})

    issues = check_environment(paths, bindkey, "8C:D0:B2:AA:BB:CC")

    assert any(issue.level == "warn" and "权限过宽" in issue.message for issue in issues)


def test_ensure_dirs_creates_everything(tmp_path: Path) -> None:
    paths = load_paths({"BPX1_INBOX": str(tmp_path / "bp" / "inbox"), "BPX1_DATA_DIR": str(tmp_path / "data")})

    config.ensure_dirs(paths)

    for directory in (paths.inbox, paths.done, paths.staging, paths.data_dir):
        assert directory.is_dir()
