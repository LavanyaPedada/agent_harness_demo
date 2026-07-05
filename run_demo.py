"""Demo client. Starts (or attaches to) a workflow and live-prints its progress.

Usage:
    D:\\analytics-demo\\dev_env\\Scripts\\python.exe D:\\analytics-demo\\run_demo.py

What it does on stage:
  1. Starts a Temporal workflow with the question + CSV path.
  2. Polls workflow status via a Query and prints each step as it lights up.
  3. When the workflow finishes, prints:
       - the executive insight
       - the coding-agent attempt trace (showing the bug + the fix)
       - the compaction event (before/after token counts)
       - the lesson recorded into AGENT.md

Resume demo:
  - While the workflow is mid-flight, kill the worker (Ctrl+C in the worker
    terminal). This script will keep polling and just show "current_step
    unchanged" until the worker comes back.
  - Restart the worker. Temporal hands the workflow back; it resumes from the
    next un-completed activity. This client never lost state because the
    workflow state lives on the Temporal server, not in this process.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

# Windows console defaults to cp1252; force UTF-8 so box-drawing chars render.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
# Also make ANSI colour codes work on legacy Windows consoles.
if os.name == "nt":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

from temporalio.client import Client, WorkflowFailureError
from temporalio.service import RPCError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.workflow import RevenueAnalysisWorkflow
from src import memory as mem

TASK_QUEUE = "analytics-demo"
TEMPORAL_TARGET = os.environ.get("TEMPORAL_TARGET", "localhost:7233")
DEFAULT_CSV = str(Path(__file__).parent / "data" / "sales.csv")
DEFAULT_QUESTION = (
    "Why did revenue drop in Q2, and what external factors contributed?"
)


# ---- pretty printing (no rich dependency required) -------------------------
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAG = "\033[35m"
BLUE = "\033[34m"


def hr(label: str = "", color: str = CYAN) -> None:
    line = "─" * 78
    if label:
        print(f"\n{color}{BOLD}── {label} {line[len(label) + 4:]}{RESET}")
    else:
        print(f"{DIM}{line}{RESET}")


def kv(k: str, v, color: str = "") -> None:
    print(f"  {DIM}{k}:{RESET} {color}{v}{RESET}")


# ---- main client logic -----------------------------------------------------
async def stream_status(client: Client, wf_id: str) -> dict:
    """Poll the workflow's status query until it's done."""
    handle = client.get_workflow_handle(wf_id)
    last_step = None
    last_completed_count = -1

    while True:
        try:
            status = await handle.query("status")
        except RPCError as e:
            print(f"{YELLOW}  [client] query failed (worker may be down): {e}{RESET}")
            await asyncio.sleep(2)
            continue

        cur = status.get("current_step")
        completed = status.get("completed_steps", [])

        if len(completed) != last_completed_count:
            for skill in completed[last_completed_count + 1 if last_completed_count >= 0 else 0:]:
                print(f"  {GREEN}✓ completed:{RESET} {BOLD}{skill}{RESET}")
            last_completed_count = len(completed) - 1

        if cur != last_step:
            if cur == "done":
                print(f"  {GREEN}{BOLD}✓ workflow complete{RESET}")
            else:
                print(f"  {YELLOW}→ running:{RESET}  {BOLD}{cur}{RESET}")
            last_step = cur

        # Check if the workflow has actually completed.
        try:
            desc = await handle.describe()
            if desc.status and desc.status.name in ("COMPLETED", "FAILED", "CANCELED", "TERMINATED"):
                break
        except Exception:
            pass

        await asyncio.sleep(1.5)

    return await handle.result()


async def run(args) -> None:
    print()
    hr("Autonomous Business Analyst (Temporal + LangGraph + Ollama)", CYAN)
    kv("CSV", args.csv)
    kv("Question", args.question)
    kv("Memory before run", f"{len(mem.list_patterns())} learned pattern(s)")

    if args.reset_memory:
        mem.reset()
        print(f"  {YELLOW}↻ memory reset (fresh demo){RESET}")

    client = await Client.connect(TEMPORAL_TARGET)

    wf_id = args.workflow_id or f"revenue-analysis-{uuid.uuid4().hex[:8]}"
    print(f"\n  {DIM}workflow_id:{RESET} {wf_id}")
    print(f"  {DIM}task_queue :{RESET} {TASK_QUEUE}")
    print(f"  {DIM}temporal UI:{RESET} http://localhost:8233/namespaces/default/workflows/{wf_id}")

    # Start (idempotent if reusing id with start fails)
    try:
        handle = await client.start_workflow(
            RevenueAnalysisWorkflow.run,
            args=[args.question, args.csv],
            id=wf_id,
            task_queue=TASK_QUEUE,
        )
        print(f"  {GREEN}▶ workflow started{RESET}")
    except Exception as e:
        # Already exists → attach
        print(f"  {YELLOW}↺ attaching to existing workflow ({type(e).__name__}){RESET}")
        handle = client.get_workflow_handle(wf_id)

    hr("Live progress", CYAN)
    try:
        result = await stream_status(client, wf_id)
    except WorkflowFailureError as e:
        print(f"\n{RED}{BOLD}Workflow failed:{RESET} {e}")
        return

    # ---- present results ---------------------------------------------------
    state = result.get("state", {})

    hr("Plan", MAG)
    for step in result.get("plan", []):
        print(f"  {BOLD}{step['step']}.{RESET} {CYAN}{step['skill']}{RESET}  {DIM}— {step.get('reason','')}{RESET}")

    if "trends" in state and isinstance(state["trends"], dict):
        trace = state["trends"].get("_agent_trace") or {}
        attempts = trace.get("attempts", [])
        if attempts:
            hr(f"Coding agent: {len(attempts)} attempt(s)", BLUE)
            for i, att in enumerate(attempts, 1):
                ok = att.get("ok")
                color = GREEN if ok else RED
                src = att.get("source", "?")
                print(f"  {color}attempt #{i}{RESET}  source={src}  ok={ok}  has_artifact={att.get('artifact') is not None}")
                if not ok and att.get("stderr"):
                    err_line = (att["stderr"].strip().splitlines() or ["(no stderr)"])[-1]
                    print(f"    {RED}stderr ⇒{RESET} {err_line[:140]}")
                if att.get("diagnosis"):
                    diag = att["diagnosis"].strip().splitlines()[0][:140]
                    print(f"    {YELLOW}LLM diagnosis ⇒{RESET} {diag}")

        compaction = trace.get("compaction_event")
        if compaction and compaction.get("compacted"):
            hr("Context compaction", YELLOW)
            kv("Before tokens", compaction["before_tokens"])
            kv("After  tokens", compaction["after_tokens"])
            kv("Saved        ", f"{compaction['saved_tokens']} tokens")
            print(f"  {DIM}summary:{RESET}\n    {compaction['summary'][:500]}")

        lesson = trace.get("lesson_recorded")
        if lesson:
            hr("New lesson recorded → memory/AGENT.md", GREEN)
            kv("Pattern", lesson["pattern"])
            kv("Fix    ", lesson["fix"])
            print(f"  {DIM}(next run will read this BEFORE generating code){RESET}")

        drops = state["trends"].get("drop_months", [])
        if drops:
            hr(f"Drop-month detection (deterministic, post-agent)", CYAN)
            for d in drops[:6]:
                pct = d.get("pct_change") or 0
                print(f"  {RED}↓{RESET}  {d['region']:6s} {d['month']}  rev={d.get('revenue', 0):>7.0f}  prev={d.get('prev_revenue', 0):>7.0f}  {pct:+.1%}")

    if "weather" in state:
        hr("External signal: Open-Meteo rainfall", CYAN)
        for region, payload in state["weather"].get("regions", {}).items():
            mp = payload.get("monthly_precip_mm", [])
            top3 = sorted(mp, key=lambda r: -(r.get("precipitation_mm") or 0))[:3]
            top3_str = ", ".join(f"{r['month']}:{r['precipitation_mm']:.0f}mm" for r in top3)
            kv(region, f"top wet months → {top3_str}")

    if "correlation" in state:
        hr("Correlation (revenue vs rainfall)", CYAN)
        for c in state["correlation"].get("correlations", []):
            r = c.get("pearson_r")
            kv(c["region"], f"pearson_r = {r:+.3f}  (n={c['n_months']} months)")

    if "report" in state:
        hr("Executive insight", GREEN)
        print(f"  {state['report'].get('insight','(no insight)')}")

    hr("Memory after run", MAG)
    kv("Patterns now stored", f"{len(mem.list_patterns())}")
    kv("AGENT.md path      ", str(mem.AGENT_MD_PATH))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default=DEFAULT_CSV)
    p.add_argument("--question", default=DEFAULT_QUESTION)
    p.add_argument("--workflow-id", default=None, help="Reuse a workflow id to attach instead of starting new.")
    p.add_argument("--reset-memory", action="store_true", help="Wipe memory/ before running (for the 'first run' demo).")
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
