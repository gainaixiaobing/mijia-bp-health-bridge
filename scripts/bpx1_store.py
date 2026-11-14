#!/usr/bin/env python3
"""落盘层：staging → inbox 的原子写入，done 标记（幂等），以及本地流水 journal。

为什么要有 staging：
    v0.1 直接在 inbox 里写 `xxx.json.tmp` 再 rename。iCloud Drive 是异步同步的，
    中间态有可能被同步到 iPhone；而快捷指令是"遍历文件夹内容"，碰到非 JSON 文件
    会整轮报错。把临时文件放到不被同步/不被遍历的 staging 目录，再 rename 进 inbox，
    快捷指令就只会看到完整的 JSON。

为什么要有 done：
    快捷指令导入成功后会写 `done/<id>.json`。守护进程（以及 refill）看到 done 标记
    就不再补发同一个 id，避免"导入一半失败 → 下一轮重复写进健康"。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

from bpx1_config import Paths


class StoreError(RuntimeError):
    pass


class Store:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    # ------------------------------------------------------------ 初始化

    def ensure(self) -> None:
        for directory in (self.paths.inbox, self.paths.done, self.paths.staging, self.paths.data_dir):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StoreError(f"无法创建目录 {directory}：{exc}") from exc

    # ------------------------------------------------------------ inbox

    def record_path(self, record_id: str) -> Path:
        return self.paths.inbox / f"{record_id}.json"

    def done_path(self, record_id: str) -> Path:
        return self.paths.done / f"{record_id}.json"

    def is_done(self, record_id: str) -> bool:
        return self.done_path(record_id).exists()

    def write(self, record: dict) -> str:
        """写一条记录。返回 written / skipped_done / skipped_exists。"""
        record_id = str(record["id"])
        if self.is_done(record_id):
            return "skipped_done"

        target = self.record_path(record_id)
        if target.exists():
            return "skipped_exists"

        self.ensure()
        staging_file = self.paths.staging / f"{record_id}.json.tmp"
        try:
            with open(staging_file, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staging_file, target)
        except OSError as exc:
            raise StoreError(f"写入 {target} 失败：{exc}") from exc
        finally:
            # rename 成功后 staging_file 已不存在；异常时清掉残骸，避免堆积
            if staging_file.exists():
                try:
                    staging_file.unlink()
                except OSError:
                    pass
        return "written"

    def mark_done(self, record_id: str, *, note: str = "") -> Path:
        """写入 done 标记。正常情况下由 iPhone 快捷指令完成，这里供补写/测试使用。"""
        self.ensure()
        path = self.done_path(record_id)
        payload = {"id": record_id, "note": note, "marked_by": "bpx1"}
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return path

    # ------------------------------------------------------------ journal

    def journal_append(self, record: dict) -> None:
        self.paths.data_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with open(self.paths.journal, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()

    def journal_records(self) -> Iterator[dict]:
        return iter_records(self.paths.journal)

    def journal_tail(self, limit: int = 5) -> list[dict]:
        records = list(self.journal_records())
        return records[-limit:]


def iter_records(path: Path) -> Iterator[dict]:
    """逐行读 JSONL；坏行跳过，不因为一行损坏就整份不可用。"""
    if not path.exists():
        return
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record
