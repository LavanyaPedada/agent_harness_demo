"""Memory + AGENT.md updater.

Two storage layers:
  1. learned_patterns.json — structured, machine-readable lessons used by the
     coding agent BEFORE it generates code (so it avoids past mistakes).
  2. AGENT.md — human-readable lessons appended after every successful
     self-correction. The talk shows this file growing on screen.
"""
from __future__ import annotations

import json
import datetime as dt
from pathlib import Path
from typing import Any

from src import events

MEM_DIR = Path(__file__).resolve().parent.parent / "memory"
MEM_DIR.mkdir(parents=True, exist_ok=True)
PATTERNS_PATH = MEM_DIR / "learned_patterns.json"
AGENT_MD_PATH = MEM_DIR / "AGENT.md"


def _load_patterns() -> list[dict]:
    if not PATTERNS_PATH.exists():
        return []
    try:
        return json.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_patterns(patterns: list[dict]) -> None:
    PATTERNS_PATH.write_text(json.dumps(patterns, indent=2), encoding="utf-8")


def list_patterns() -> list[dict]:
    return _load_patterns()


def remember(pattern: str, fix: str, evidence: str = "", task_kind: str = "general") -> dict:
    """Record a lesson. Idempotent on (pattern, fix)."""
    patterns = _load_patterns()
    for p in patterns:
        if p.get("pattern") == pattern and p.get("fix") == fix:
            p["count"] = p.get("count", 1) + 1
            p["last_seen"] = dt.datetime.utcnow().isoformat() + "Z"
            _save_patterns(patterns)
            _append_agent_md(p, updated=True)
            events.emit("memory.write", f"lesson reinforced (count={p['count']})", {"entry": p, "updated": True, "agent_md_path": str(AGENT_MD_PATH)}, src="memory.py:remember")
            return p
    entry = {
        "pattern": pattern,
        "fix": fix,
        "evidence": evidence,
        "task_kind": task_kind,
        "count": 1,
        "first_seen": dt.datetime.utcnow().isoformat() + "Z",
        "last_seen": dt.datetime.utcnow().isoformat() + "Z",
    }
    patterns.append(entry)
    _save_patterns(patterns)
    _append_agent_md(entry, updated=False)
    events.emit("memory.write", "new lesson written to AGENT.md", {"entry": entry, "updated": False, "agent_md_path": str(AGENT_MD_PATH)}, src="memory.py:remember")
    return entry


def _append_agent_md(entry: dict, updated: bool) -> None:
    if not AGENT_MD_PATH.exists():
        AGENT_MD_PATH.write_text(
            "# AGENT.md\n\n"
            "Operational memory for the analytics agent. Lessons here are read "
            "by the coding agent BEFORE it generates any code so previously-seen "
            "mistakes are not repeated.\n\n"
            "## Learned patterns\n\n",
            encoding="utf-8",
        )
    block = (
        f"### {entry['pattern']}\n"
        f"- **Fix**: {entry['fix']}\n"
        f"- **Task kind**: {entry.get('task_kind', 'general')}\n"
        f"- **Seen**: {entry['count']} time(s) (first {entry['first_seen']})\n"
    )
    if entry.get("evidence"):
        block += f"- **Evidence**: `{entry['evidence']}`\n"
    block += "\n"
    if updated:
        block = "<!-- updated -->\n" + block
    with AGENT_MD_PATH.open("a", encoding="utf-8") as f:
        f.write(block)


def patterns_for_prompt(task_kind: str = "general") -> str:
    """Render the lesson set as a compact prompt-ready block."""
    patterns = _load_patterns()
    if not patterns:
        return "(no prior lessons recorded)"
    lines = []
    for i, p in enumerate(patterns, 1):
        lines.append(f"{i}. PATTERN: {p['pattern']}\n   FIX: {p['fix']}")
    return "\n".join(lines)


def reset() -> None:
    """Wipe memory — used by the demo to show 'first run vs second run'."""
    if PATTERNS_PATH.exists():
        PATTERNS_PATH.unlink()
    if AGENT_MD_PATH.exists():
        AGENT_MD_PATH.unlink()
