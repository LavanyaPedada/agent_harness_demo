"""LangGraph planner / orchestrator (the *non-coding* agent).

Asks the LLM to map the user's question + available skills into an ordered
plan. The plan is what the Temporal workflow then executes step-by-step,
so the workflow logic is dynamic — you can add a new skill and the planner
will pick it up without changing the workflow code.

Falls back to a hardcoded plan if the LLM output can't be parsed (demo
reliability matters more than purity).
"""
from __future__ import annotations

import json
import os
import re
from typing import TypedDict

from langgraph.graph import StateGraph, END

from src.llm import chat
from src.skills import list_skills, load_skill
from src import events


def _demo_fast() -> bool:
    return os.environ.get("DEMO_FAST", "").lower() in ("1", "true", "yes", "on")


class PlannerState(TypedDict, total=False):
    question: str
    csv_path: str
    plan: list[dict]
    rationale: str


SYSTEM = (
    "You are an analytics planner. Given a user question, a CSV path, and a "
    "set of available skills, produce a STRICT JSON plan. Each step is "
    "{\"step\": int, \"skill\": str, \"reason\": str}. Use ONLY skills from "
    "the provided list. Plans MUST start with csv_loader and end with "
    "report_generator. Output ONLY JSON, no prose."
)


def _skill_catalog() -> str:
    lines = []
    for name in list_skills():
        sk = load_skill(name)
        lines.append(f"- {name}: {sk.description}")
    return "\n".join(lines)


_FALLBACK_PLAN = [
    {"step": 1, "skill": "csv_loader",            "reason": "Load and inspect CSV schema."},
    {"step": 2, "skill": "trend_analysis",        "reason": "Aggregate revenue and find drop periods."},
    {"step": 3, "skill": "weather_fetch",         "reason": "Fetch external rainfall signal per region."},
    {"step": 4, "skill": "correlation_analysis",  "reason": "Correlate rainfall with revenue per region."},
    {"step": 5, "skill": "report_generator",      "reason": "Produce the executive insight."},
]


def _parse_plan(text: str) -> list[dict] | None:
    # Strip code fences if present.
    m = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    payload = m.group(1) if m else text
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if isinstance(data, dict) and "plan" in data:
        data = data["plan"]
    if not isinstance(data, list):
        return None
    valid = list_skills()
    cleaned = []
    for i, step in enumerate(data, 1):
        if not isinstance(step, dict):
            return None
        skill = step.get("skill")
        if skill not in valid:
            return None
        cleaned.append({"step": i, "skill": skill, "reason": step.get("reason", "")})
    return cleaned or None


def _plan_node(state: PlannerState) -> PlannerState:
    if _demo_fast():
        events.emit(
            "planner.cached",
            f"Plan retrieved from cache ({len(_FALLBACK_PLAN)} steps)",
            {"plan_steps": len(_FALLBACK_PLAN)},
            src="orchestrator.py:_plan_node",
        )
        state["plan"] = _FALLBACK_PLAN
        state["rationale"] = "Plan retrieved from cache."
        return state

    user = (
        f"Question:\n{state['question']}\n\n"
        f"CSV path: {state['csv_path']}\n\n"
        f"Available skills:\n{_skill_catalog()}\n\n"
        "Output the JSON plan now."
    )
    events.emit("llm.call", "planner LLM", {"system_chars": len(SYSTEM), "user_chars": len(user)}, src="orchestrator.py:_plan_node")
    resp = chat(SYSTEM, user, temperature=0.0)
    plan = _parse_plan(resp)
    if not plan:
        plan = _FALLBACK_PLAN
        state["rationale"] = "LLM output unparseable, using fallback plan."
        events.emit("planner.fallback", "LLM output unparseable, using fallback", {"raw_response": resp[:300]}, src="orchestrator.py:_plan_node")
    else:
        state["rationale"] = resp.strip()[:500]
    state["plan"] = plan
    return state


def build_planner_graph():
    g = StateGraph(PlannerState)
    g.add_node("plan", _plan_node)
    g.set_entry_point("plan")
    g.add_edge("plan", END)
    return g.compile()


_GRAPH = None


def _graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_planner_graph()
    return _GRAPH


def make_plan(question: str, csv_path: str) -> dict:
    final = _graph().invoke({"question": question, "csv_path": csv_path})
    return {"plan": final.get("plan", []), "rationale": final.get("rationale", "")}


# --------------------------------------------------------------------------
# Replanning — called by the workflow when an activity fails.
#
# Strategy: try a *deterministic* remedy first (if we know which skill
# produces the missing input, just insert it before the failed step). Fall
# back to an LLM replan if the remedy isn't obvious. Either way, return a
# plan whose steps have NOT yet been executed — the workflow uses
# completed_steps to skip past the prefix.
# --------------------------------------------------------------------------
def _produces_index() -> dict[str, str]:
    """Map produced-key -> skill name. e.g. 'weather' -> 'weather_fetch'."""
    out: dict[str, str] = {}
    for name in list_skills():
        try:
            sk = load_skill(name)
            out[sk.produces] = name
        except Exception:
            continue
    return out


def _deterministic_remedy(
    prior_plan: list[dict],
    completed_steps: list[str],
    failed_skill: str,
    failure_kind: str,
    failure_message: str,
) -> list[dict] | None:
    """If the failure is a ToolValidationError pointing at a known missing
    key (e.g. 'weather.regions'), find the skill that produces that key and
    insert it before the failed step.

    Returns the new plan (with renumbered steps) or None if no obvious fix.
    """
    if failure_kind != "ToolValidationError":
        return None
    # Try to extract referenced keys from the message — accept either bare
    # key names ('trends') or dotted ('trends.region_monthly').
    refs = re.findall(r"['\"]?([a-zA-Z_][\w]*)(?:\.[\w]+)?['\"]?", failure_message)
    produces = _produces_index()
    insert_skills: list[str] = []
    for r in refs:
        if r in produces and produces[r] not in completed_steps and produces[r] != failed_skill:
            cand = produces[r]
            if cand not in insert_skills:
                insert_skills.append(cand)
    if not insert_skills:
        return None

    # Build new plan: completed prefix unchanged, then insert remedy skills,
    # then resume from the failed step.
    remaining_after_failure = []
    started_remaining = False
    for step in prior_plan:
        if step["skill"] in completed_steps and not started_remaining:
            continue
        if step["skill"] == failed_skill and not started_remaining:
            started_remaining = True
            for s in insert_skills:
                remaining_after_failure.append({
                    "step": 0,
                    "skill": s,
                    "reason": f"Inserted by replan: {failed_skill} needed its output.",
                })
            remaining_after_failure.append(step)
            continue
        remaining_after_failure.append(step)

    if not started_remaining:
        # Failed skill wasn't in prior_plan? Append the remedy + a synthesised
        # final step.
        remaining_after_failure = [
            {"step": 0, "skill": s, "reason": f"Inserted by replan."} for s in insert_skills
        ]

    # Renumber.
    out = []
    for i, s in enumerate(remaining_after_failure, 1):
        out.append({"step": i, "skill": s["skill"], "reason": s.get("reason", "")})
    return out


REPLAN_SYSTEM = (
    "You are an analytics replanner. The previous plan failed at one step. "
    "Produce a NEW JSON plan that picks up from where things failed and "
    "still answers the user's question. Each step is "
    '{"step": int, "skill": str, "reason": str}. Use ONLY skills from the '
    "provided list. Do NOT include any of the already-completed steps. "
    "Output ONLY JSON, no prose."
)


def _llm_replan(
    question: str,
    completed_steps: list[str],
    failed_skill: str,
    failure_kind: str,
    failure_message: str,
) -> list[dict] | None:
    user = (
        f"Question:\n{question}\n\n"
        f"Already completed (do NOT repeat): {completed_steps}\n"
        f"Failed step: {failed_skill}\n"
        f"Failure kind: {failure_kind}\n"
        f"Failure message: {failure_message[:600]}\n\n"
        f"Available skills:\n{_skill_catalog()}\n\n"
        "Output the JSON plan now (only the steps that have not yet run)."
    )
    try:
        resp = chat(REPLAN_SYSTEM, user, temperature=0.0)
    except Exception:
        return None
    return _parse_plan(resp)


def replan(
    *,
    question: str,
    csv_path: str,
    prior_plan: list[dict],
    completed_steps: list[str],
    failed_skill: str,
    failure_kind: str,
    failure_message: str,
) -> dict:
    """Produce a remediation plan after an activity failure."""
    # 1. Deterministic remedy first.
    remedy = _deterministic_remedy(
        prior_plan, completed_steps, failed_skill, failure_kind, failure_message
    )
    if remedy:
        events.emit(
            "planner.remedy",
            f"deterministic remedy applied (insert {[s['skill'] for s in remedy[:2]]})",
            {"plan": remedy, "source": "deterministic"},
            src="orchestrator.py:replan",
        )
        return {"plan": remedy, "rationale": "deterministic-remedy", "source": "deterministic"}

    # 2. LLM fallback.
    plan = _llm_replan(question, completed_steps, failed_skill, failure_kind, failure_message)
    if plan:
        events.emit(
            "planner.remedy",
            "LLM replan produced",
            {"plan": plan, "source": "llm"},
            src="orchestrator.py:replan",
        )
        return {"plan": plan, "rationale": "llm-replan", "source": "llm"}

    # 3. Last resort — drop the failed skill and continue.
    pruned = [s for s in prior_plan if s["skill"] != failed_skill and s["skill"] not in completed_steps]
    pruned = [{"step": i + 1, "skill": s["skill"], "reason": s.get("reason", "")} for i, s in enumerate(pruned)]
    events.emit(
        "planner.remedy",
        f"giving up on {failed_skill}; pruning from plan",
        {"plan": pruned, "source": "prune"},
        src="orchestrator.py:replan",
    )
    return {"plan": pruned, "rationale": "prune-failed-skill", "source": "prune"}
