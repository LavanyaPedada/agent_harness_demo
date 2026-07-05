"""Context compaction.

Maintains a running transcript of (role, content) tuples. When the rough token
estimate exceeds the soft limit, asks the LLM to summarise older entries into
a single 'summary' message, keeping only the most recent N raw entries.

Used by the coding agent's correction loop so multi-attempt traces don't blow
up the context window — and so the talk can show the before/after diff.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from src.llm import chat
from src import events


def _estimate_tokens(text: str) -> int:
    # Crude 4-chars-per-token heuristic. Good enough for a demo.
    return max(1, len(text) // 4)


@dataclass
class Conversation:
    entries: list[dict] = field(default_factory=list)
    soft_token_limit: int = 600
    keep_last: int = 2
    summaries: list[str] = field(default_factory=list)

    def add(self, role: str, content: str) -> None:
        self.entries.append({"role": role, "content": content})

    def total_tokens(self) -> int:
        return sum(_estimate_tokens(e["content"]) for e in self.entries) + sum(
            _estimate_tokens(s) for s in self.summaries
        )

    def needs_compaction(self) -> bool:
        return self.total_tokens() > self.soft_token_limit and len(self.entries) > self.keep_last

    def snapshot(self) -> dict:
        return {
            "summaries": list(self.summaries),
            "entries": list(self.entries),
            "tokens": self.total_tokens(),
        }

    def compact(self) -> dict:
        """Summarise the older portion. Returns a diff dict for the demo to display."""
        if len(self.entries) <= self.keep_last:
            return {"compacted": False, "reason": "not enough entries"}

        old = self.entries[: -self.keep_last]
        kept = self.entries[-self.keep_last :]

        old_serialised = "\n".join(f"[{e['role']}] {e['content']}" for e in old)
        before_tokens = self.total_tokens()

        system = (
            "You are a context compactor. Summarise the conversation below into "
            "a tight bullet list of facts that future steps must remember "
            "(schemas, fixes that worked, transient state, intermediate results). "
            "Drop chit-chat. Be specific about column names, file paths, and numeric values."
        )
        summary = chat(system, old_serialised, temperature=0.0).strip()

        self.summaries.append(summary)
        self.entries = kept
        after_tokens = self.total_tokens()

        result = {
            "compacted": True,
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "saved_tokens": before_tokens - after_tokens,
            "summarised_entries": len(old),
            "summary": summary,
        }
        events.emit(
            "context.compact",
            f"compacted {len(old)} entries → 1 summary ({before_tokens} → {after_tokens} tokens)",
            result,
            src="compaction.py:compact",
        )
        return result
