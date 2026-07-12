from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from company_twin.recorder import RunRecorder


def _read_ledger(run_root: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (run_root / "world_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_concurrent_appends_keep_file_order_equal_to_chain_order(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    recorder = RunRecorder(run_root, run_id="unit", meta={})
    thread_count = 8
    per_thread = 50
    barrier = threading.Barrier(thread_count)

    def append_events(thread_id: int) -> None:
        barrier.wait()
        for n in range(per_thread):
            recorder.append_ledger("unit_event", {"n": n, "t": thread_id})

    with ThreadPoolExecutor(max_workers=thread_count) as executor:
        list(executor.map(append_events, range(thread_count)))

    rows = _read_ledger(run_root)
    assert len(rows) == thread_count * per_thread
    for index in range(1, len(rows)):
        assert rows[index]["prev_hash"] == rows[index - 1]["hash"]


def test_single_thread_chain_unchanged(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    recorder = RunRecorder(run_root, run_id="unit", meta={})

    for n in range(5):
        recorder.append_ledger("unit_event", {"n": n, "t": 0})

    rows = _read_ledger(run_root)
    assert len(rows) == 5
    assert set(rows[0]) == {"ts", "run_id", "tick", "event_type", "payload", "prev_hash", "hash"}
    for index in range(1, len(rows)):
        assert rows[index]["prev_hash"] == rows[index - 1]["hash"]
        assert set(rows[index]) == {"ts", "run_id", "tick", "event_type", "payload", "prev_hash", "hash"}
