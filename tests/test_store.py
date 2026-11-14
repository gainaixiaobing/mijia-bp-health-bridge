"""落盘层测试：原子写入、done 幂等、journal。"""

from __future__ import annotations

import json
from pathlib import Path

from bpx1_config import Paths
from bpx1_store import Store, iter_records


def make_paths(tmp_path: Path) -> Paths:
    return Paths(
        inbox=tmp_path / "inbox",
        done=tmp_path / "done",
        staging=tmp_path / "staging",
        data_dir=tmp_path / "data",
    )


def sample_record(record_id: str = "bp-20261010-163428-115-91-69") -> dict:
    return {
        "schema": 1,
        "id": record_id,
        "time": "2026-10-10T16:34:28",
        "systolic": 115,
        "diastolic": 91,
        "heart_rate": 69,
    }


def test_write_lands_in_inbox_and_clears_staging(tmp_path: Path) -> None:
    store = Store(make_paths(tmp_path))

    assert store.write(sample_record()) == "written"
    assert (tmp_path / "inbox" / "bp-20261010-163428-115-91-69.json").exists()
    assert list((tmp_path / "staging").glob("*")) == []  # 不留 .tmp 残骸


def test_write_is_idempotent(tmp_path: Path) -> None:
    store = Store(make_paths(tmp_path))

    assert store.write(sample_record()) == "written"
    assert store.write(sample_record()) == "skipped_exists"


def test_done_marker_suppresses_rewrite(tmp_path: Path) -> None:
    store = Store(make_paths(tmp_path))
    store.mark_done("bp-20261010-163428-115-91-69")

    assert store.is_done("bp-20261010-163428-115-91-69")
    assert store.write(sample_record()) == "skipped_done"
    assert not store.record_path("bp-20261010-163428-115-91-69").exists()


def test_record_content_is_readable_json(tmp_path: Path) -> None:
    store = Store(make_paths(tmp_path))
    store.write(sample_record())

    payload = json.loads(store.record_path("bp-20261010-163428-115-91-69").read_text(encoding="utf-8"))
    assert payload["systolic"] == 115
    assert payload["id"] == "bp-20261010-163428-115-91-69"


def test_journal_append_and_skip_bad_lines(tmp_path: Path) -> None:
    store = Store(make_paths(tmp_path))
    store.journal_append(sample_record())
    store.journal_append(sample_record("bp-2"))
    with open(store.paths.journal, "a", encoding="utf-8") as handle:
        handle.write("{ 这不是 JSON\n")

    records = list(store.journal_records())

    assert [record["id"] for record in records] == ["bp-20261010-163428-115-91-69", "bp-2"]
    assert len(store.journal_tail(limit=1)) == 1


def test_iter_records_on_missing_file(tmp_path: Path) -> None:
    assert list(iter_records(tmp_path / "nope.jsonl")) == []
