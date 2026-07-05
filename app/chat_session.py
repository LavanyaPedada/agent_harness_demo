"""Routes a chat message to either a fresh Temporal workflow (cold) or an
LLM call grounded in a finished workflow's cached state (warm).

A workflow is "cold" the first time you ask a question — it spins up the full
plan / coding agent / weather / correlation / report pipeline (~3 min on CPU).

Subsequent questions in the same session are "warm": we reuse the prior
workflow's accumulated state as evidence and answer with a single LLM call
(sub-second). The audience sees the cached state in the right pane.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from temporalio.client import Client

from src.workflow import RevenueAnalysisWorkflow
from src.llm import chat
from src import events

TASK_QUEUE = "analytics-demo"
TEMPORAL_TARGET = os.environ.get("TEMPORAL_TARGET", "localhost:7233")
DEFAULT_CSV = str(Path(__file__).resolve().parent.parent / "data" / "sales.csv")

STATE_DIR = Path(__file__).resolve().parent.parent / "logs" / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)


WARM_SYSTEM = (
    "You are a senior business analyst. Answer the user's follow-up question "
    "in 2-4 sentences using ONLY the EVIDENCE provided. Do not invent regions, "
    "months, or numbers. If the evidence does not contain the answer, say so."
)


def state_path(wf_id: str) -> Path:
    return STATE_DIR / f"{wf_id}.json"


def save_state(wf_id: str, state: dict) -> None:
    try:
        state_path(wf_id).write_text(json.dumps(state, default=str), encoding="utf-8")
    except Exception:
        pass


def load_state(wf_id: str) -> dict | None:
    p = state_path(wf_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


async def start_cold_workflow(
    question: str,
    csv_path: str | None = None,
    *,
    hitl_enabled: bool = False,
    failure_mode: str = "",
) -> str:
    """Kick off a fresh workflow. Returns the workflow_id immediately; caller
    streams events via SSE while the workflow runs."""
    csv = csv_path or DEFAULT_CSV
    wf_id = f"revenue-analysis-{uuid.uuid4().hex[:8]}"
    client = await Client.connect(TEMPORAL_TARGET)
    await client.start_workflow(
        RevenueAnalysisWorkflow.run,
        args=[question, csv, hitl_enabled, failure_mode],
        id=wf_id,
        task_queue=TASK_QUEUE,
    )
    return wf_id


async def fetch_workflow_result(wf_id: str) -> dict | None:
    """Block until the workflow completes; persist + return the result."""
    client = await Client.connect(TEMPORAL_TARGET)
    handle = client.get_workflow_handle(wf_id)
    result = await handle.result()
    save_state(wf_id, result)
    return result


async def workflow_status(wf_id: str) -> dict:
    client = await Client.connect(TEMPORAL_TARGET)
    handle = client.get_workflow_handle(wf_id)
    try:
        status = await handle.query("status")
    except Exception as e:
        return {"error": str(e), "current_step": None, "completed_steps": [], "plan": []}
    desc = await handle.describe()
    status["execution_status"] = desc.status.name if desc.status else None
    return status


def warm_answer(question: str, wf_id: str) -> dict:
    """Answer a follow-up question grounded in the prior workflow's state."""
    state = load_state(wf_id)
    if not state:
        return {"answer": "No prior workflow state cached for this session — try asking a fresh question.", "evidence": None}

    inner = state.get("state", {})
    evidence = {
        "question_was": inner.get("question"),
        "monthly_totals": (inner.get("trends") or {}).get("monthly_totals", []),
        "drop_months": (inner.get("trends") or {}).get("drop_months", []),
        "regions_in_data": sorted({(r.get("region") or "?") for r in (inner.get("trends") or {}).get("region_monthly", [])}),
        "correlations": (inner.get("correlation") or {}).get("correlations", []),
        "weather_window": (inner.get("weather") or {}).get("window"),
        "prior_insight": (inner.get("report") or {}).get("insight"),
    }
    user = (
        f"Follow-up question:\n{question}\n\n"
        f"EVIDENCE (the prior workflow's cached findings — the ONLY data you may reference):\n"
        f"{json.dumps(evidence, indent=2, default=str)}\n\n"
        "Answer concisely."
    )
    # emit so the UI shows what we're doing
    events.set_workflow(wf_id)
    events.emit("warm.lookup", "follow-up answered from cached state (no new workflow)", {"question": question}, src="chat_session.py:warm_answer")
    answer = chat(WARM_SYSTEM, user, temperature=0.1).strip()
    events.emit("warm.answer", "follow-up LLM call complete", {"chars": len(answer)}, src="chat_session.py:warm_answer")
    return {"answer": answer, "evidence": evidence}
