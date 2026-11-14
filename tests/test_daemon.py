"""守护进程采集逻辑测试：用伪造的广播对象驱动 handle_advertisement。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

import bpx1_daemon as daemon
from _frames import BINDKEY, MAC, OTHER_KEY, bp_payload, build_frame
from bpx1_config import Paths
from bpx1_store import Store

FE95 = "0000fe95-0000-1000-8000-00805f9b34fb"


class FakeAdv:
    def __init__(self, frame: bytes, rssi: int = -55) -> None:
        self.service_data = {FE95: frame}
        self.rssi = rssi


def now_payload_and_id(**kwargs) -> tuple[bytes, str]:
    """一次取时间，保证 payload 里的时间与 id 一定对得上。"""
    stamp = datetime.now().astimezone().replace(microsecond=0)
    return bp_payload(when=stamp, **kwargs), f"bp-{stamp:%Y%m%d-%H%M%S}-115-91-69"


def make_runtime(tmp_path: Path, bindkey: bytes = BINDKEY, mac: str = MAC) -> daemon.Runtime:
    paths = Paths(
        inbox=tmp_path / "inbox",
        done=tmp_path / "done",
        staging=tmp_path / "staging",
        data_dir=tmp_path / "data",
    )
    store = Store(paths)
    store.ensure()
    return daemon.Runtime(
        paths=paths,
        bindkey=bindkey,
        bindkey_source="test",
        mac=mac,
        store=store,
        max_skew=24 * 3600,
    )


@pytest.fixture(autouse=True)
def capture_log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    messages: list[str] = []
    monkeypatch.setattr(daemon, "log", messages.append)
    return messages


def test_advertisement_writes_inbox_and_journal(tmp_path: Path, capture_log: list[str]) -> None:
    runtime = make_runtime(tmp_path)
    payload, expected_id = now_payload_and_id()

    daemon.handle_advertisement(runtime, FakeAdv(build_frame(payload)))

    assert runtime.stats.written == 1
    assert runtime.store.record_path(expected_id).exists()
    assert len(list(runtime.store.journal_records())) == 1
    assert any("新测量" in message for message in capture_log)


def test_repeated_burst_writes_once(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    payload, _ = now_payload_and_id()
    frame = build_frame(payload)

    for _ in range(6):
        daemon.handle_advertisement(runtime, FakeAdv(frame))

    assert runtime.stats.written == 1
    assert runtime.stats.duplicate == 5
    assert len(list(runtime.store.journal_records())) == 1


def test_done_marker_prevents_reimport(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    payload, expected_id = now_payload_and_id()
    runtime.store.mark_done(expected_id)

    daemon.handle_advertisement(runtime, FakeAdv(build_frame(payload)))

    assert runtime.stats.written == 0
    assert runtime.stats.outcomes["skipped_done"] == 1
    assert not runtime.store.record_path(expected_id).exists()


def test_wrong_bindkey_is_loud(tmp_path: Path, capture_log: list[str]) -> None:
    runtime = make_runtime(tmp_path, bindkey=OTHER_KEY)
    payload, _ = now_payload_and_id()
    frame = build_frame(payload)

    for _ in range(5):
        daemon.handle_advertisement(runtime, FakeAdv(frame))

    assert runtime.stats.reasons["decrypt_failed"] == 5
    assert runtime.stats.written == 0
    assert any("无一解开" in message for message in capture_log)


def test_missing_mac_is_loud(tmp_path: Path, capture_log: list[str]) -> None:
    runtime = make_runtime(tmp_path, mac="")
    payload, _ = now_payload_and_id()

    daemon.handle_advertisement(runtime, FakeAdv(build_frame(payload)))

    assert runtime.stats.reasons["no_mac"] == 1
    assert any("BPX1_MAC" in message for message in capture_log)


def test_irrelevant_service_data_is_ignored(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)

    class Adv:
        service_data = {"0000180f-0000-1000-8000-00805f9b34fb": b"\x64"}
        rssi = -70

    daemon.handle_advertisement(runtime, Adv())

    assert runtime.stats.reasons["frames"] == 0


def test_callback_never_propagates_exceptions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = make_runtime(tmp_path)

    def boom(*_args):
        raise RuntimeError("boom")

    monkeypatch.setattr(daemon, "handle_advertisement", boom)

    daemon.make_callback(runtime)(None, FakeAdv(b""))  # 不应抛出


def test_stats_summary_reports_bpx1_frames(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    payload, _ = now_payload_and_id()
    daemon.handle_advertisement(runtime, FakeAdv(build_frame(payload)))

    summary = runtime.stats.summary()

    assert "bpx1=1" in summary
    assert "written=1" in summary
