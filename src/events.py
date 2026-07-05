"""Structured event emitter for the live UI.

Every harness touchpoint emits one JSONL line to logs/events-{wf_id}.jsonl.
The FastAPI server tails that file and streams the events to the browser
over SSE.

Workflow id is stored per-thread (threading.local) — Temporal runs activities
in a thread pool and contextvars don't reliably propagate across the executor
boundary. Activities call set_workflow() at entry; nested calls on the same
thread see it via get_workflow().
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_local = threading.local()


def set_workflow(wf_id: str | None) -> None:
    _local.wf_id = wf_id


def get_workflow() -> str | None:
    return getattr(_local, "wf_id", None)


def log_path(wf_id: str) -> Path:
    return LOG_DIR / f"events-{wf_id}.jsonl"


def emit(kind: str, msg: str, payload: dict[str, Any] | None = None, src: str = "") -> None:
    """Append a structured event. No-op if no workflow context is set."""
    wf = get_workflow()
    if not wf:
        return
    record = {
        "ts": time.time(),
        "wf": wf,
        "kind": kind,
        "msg": msg,
        "src": src,
        "payload": payload or {},
    }
    line = json.dumps(record, default=str, ensure_ascii=False) + "\n"
    path = log_path(wf)
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def tail_events(wf_id: str, from_offset: int = 0, poll_interval: float = 0.25) -> Iterator[tuple[int, dict]]:
    """Yield (new_offset, event_dict) tuples as they're written. Blocks forever.

    Caller decides when to stop (e.g. when the workflow status flips to done
    on a query). Safe to start before the file exists.
    """
    path = log_path(wf_id)
    offset = from_offset
    last_check = time.time()
    while True:
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    f.seek(offset)
                    chunk = f.read()
                    if chunk:
                        for raw in chunk.splitlines():
                            raw = raw.strip()
                            if not raw:
                                continue
                            try:
                                yield offset + len(raw) + 1, json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            offset += len(raw.encode("utf-8")) + 1
            except Exception:
                pass
        # Heartbeat every 5s so SSE doesn't look dead
        if time.time() - last_check > 5:
            yield offset, {"ts": time.time(), "wf": wf_id, "kind": "heartbeat", "msg": "", "src": "", "payload": {}}
            last_check = time.time()
        time.sleep(poll_interval)


def reset(wf_id: str) -> None:
    p = log_path(wf_id)
    if p.exists():
        p.unlink()
