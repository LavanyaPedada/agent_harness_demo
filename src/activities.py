"""Temporal activities — one per skill plus a planner activity.

Activities are the unit of durability in Temporal. The workflow records each
activity completion in the event history, so killing the worker mid-run and
restarting it picks up at the next un-completed activity.

This module also handles three v2-demo features:

  * **Hot-add skills** — refresh_skill_activities() rescans src/skills/ and
    adds activities for new files. worker_mgr.start() calls it before each
    (re)start so dropping a new file + clicking "Reload Skills" works.

  * **Tool-failure self-debug** — each activity calls skill.validate(inputs)
    if the skill defines one. Validation failures raise ToolValidationError
    which Temporal surfaces as ApplicationError("ToolValidationError"); the
    workflow catches it and routes into the replan path.

  * **Provenance** — every activity returns a `_provenance` dict alongside
    its value, capturing skill, ts, elapsed, validation status, and any
    coding-agent attempts/lessons. The workflow accumulates these into
    state["_provenance"] so the UI can show a Sources panel under the
    insight.
"""
from __future__ import annotations

import contextvars
import datetime as dt
import json
import threading
import time
from contextlib import contextmanager
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from src.skills import load_skill, list_skills, ToolValidationError
from src.orchestrator import make_plan, replan
from src import events


@contextmanager
def heartbeat(interval: float = 2.0):
    """Periodically calls activity.heartbeat() so Temporal can detect a dead
    worker within `heartbeat_timeout` (set on the workflow side). Sync activities
    can't await — we use a daemon thread, but we must copy the activity's
    context into that thread (activity.heartbeat() reads contextvars)."""
    stop = threading.Event()
    ctx = contextvars.copy_context()  # snapshot the current activity context

    def _ping():
        while not stop.wait(interval):
            try:
                ctx.run(activity.heartbeat)
            except Exception:
                # Activity may have completed or been cancelled; silently exit.
                break

    t = threading.Thread(target=_ping, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()


def _now_iso() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


@activity.defn(name="plan_activity")
def plan_activity(question: str, csv_path: str) -> dict:
    events.set_workflow(activity.info().workflow_id)
    activity.logger.info(f"Planning for question: {question!r}")
    with heartbeat():
        events.emit("workflow.step", "planner", {"question": question}, src="activities.py:plan_activity")
        result = make_plan(question=question, csv_path=csv_path)
        # Compute which skills in the plan need approval (HITL gating). The
        # workflow needs this to gate steps without importing skill modules.
        approval_required: list[str] = []
        for step in result.get("plan", []):
            try:
                sk = load_skill(step["skill"])
                if sk.requires_approval:
                    approval_required.append(sk.name)
            except Exception:
                continue
        result["approval_required_skills"] = sorted(set(approval_required))
        events.emit(
            "planner.emit",
            f"{len(result.get('plan', []))}-step plan emitted",
            {"plan": result.get("plan", []), "approval_required_skills": result["approval_required_skills"]},
            src="activities.py:plan_activity",
        )
        return result


@activity.defn(name="replan_activity")
def replan_activity(
    question: str,
    csv_path: str,
    prior_plan: list[dict],
    completed_steps: list[str],
    failed_skill: str,
    failure_kind: str,
    failure_message: str,
) -> dict:
    """Called by the workflow when an activity fails. Asks the planner LLM to
    emit a NEW plan that routes around the failure, given what's already done.

    The new plan must NOT include any of completed_steps (they're already
    materialised in workflow state) and SHOULD avoid the failed_skill or
    propose a different ordering / substitute."""
    events.set_workflow(activity.info().workflow_id)
    with heartbeat():
        events.emit(
            "planner.replan",
            f"replanning around failed skill '{failed_skill}'",
            {
                "failed_skill": failed_skill,
                "failure_kind": failure_kind,
                "failure_message": failure_message[:500],
                "completed_steps": completed_steps,
            },
            src="activities.py:replan_activity",
        )
        result = replan(
            question=question,
            csv_path=csv_path,
            prior_plan=prior_plan,
            completed_steps=completed_steps,
            failed_skill=failed_skill,
            failure_kind=failure_kind,
            failure_message=failure_message,
        )
        approval_required: list[str] = []
        for step in result.get("plan", []):
            try:
                sk = load_skill(step["skill"])
                if sk.requires_approval:
                    approval_required.append(sk.name)
            except Exception:
                continue
        result["approval_required_skills"] = sorted(set(approval_required))
        events.emit(
            "planner.emit",
            f"{len(result.get('plan', []))}-step plan emitted (after replan)",
            {"plan": result.get("plan", []), "approval_required_skills": result["approval_required_skills"], "after_replan": True},
            src="activities.py:replan_activity",
        )
        return result


def _summarise_value(v: Any) -> dict:
    """Cheap summary of a skill's output, embedded in provenance. Keeps
    the trace compact while still letting the UI show 'this is what came
    out of step N'."""
    if v is None:
        return {"type": "none"}
    if isinstance(v, dict):
        return {"type": "dict", "keys": list(v.keys())[:12]}
    if isinstance(v, list):
        return {"type": "list", "len": len(v), "sample": v[:1] if v else []}
    return {"type": type(v).__name__}


def _run_skill_with_provenance(skill_name: str, inputs: dict, src_label: str) -> dict:
    """Shared body for both run_skill_activity and the per-skill activities.

    1. emits skill.start
    2. runs skill.validate (if present) — raises ApplicationError on bad input
       so Temporal surfaces it as a non-retryable, planner-visible failure.
    3. runs the handler
    4. emits skill.end
    5. wraps the result in {produces, value, _provenance}
    """
    skill = load_skill(skill_name)
    kwargs = {k: inputs[k] for k in skill.expects if k in inputs}
    missing = [k for k in skill.expects if k not in inputs]

    events.emit(
        "skill.start",
        skill_name,
        {"expects": skill.expects, "got": list(kwargs.keys()), "missing": missing},
        src=src_label,
    )

    # 1. Validate
    if missing:
        # Missing inputs is a structured failure the planner can act on.
        evt = {"skill": skill_name, "missing": missing, "got": list(kwargs.keys())}
        events.emit("tool.invalid_args", f"{skill_name} missing inputs: {missing}", evt, src=src_label)
        raise ApplicationError(
            f"missing required inputs for '{skill_name}': {missing}",
            evt,
            type="ToolValidationError",
            non_retryable=True,
        )
    if skill.validate is not None:
        try:
            skill.validate(**kwargs)
        except ToolValidationError as ve:
            evt = {"skill": skill_name, "missing": ve.missing, "message": str(ve)}
            events.emit("tool.invalid_args", f"{skill_name} validation failed: {ve}", evt, src=src_label)
            raise ApplicationError(
                str(ve), evt, type="ToolValidationError", non_retryable=True,
            )

    # 2. Run
    t0 = time.time()
    result = skill.handler(**kwargs)
    elapsed_ms = int((time.time() - t0) * 1000)

    events.emit(
        "skill.end",
        skill_name,
        {"elapsed_ms": elapsed_ms, "produces": skill.produces},
        src=src_label,
    )

    # 3. Provenance — accumulate any sub-trace already in the value (e.g.
    # the trend_analysis skill bundles _agent_trace inside its return).
    sub_trace = None
    if isinstance(result, dict) and "_agent_trace" in result:
        sub_trace = result.get("_agent_trace")

    provenance = {
        "skill": skill_name,
        "produced_key": skill.produces,
        "ts": _now_iso(),
        "elapsed_ms": elapsed_ms,
        "expects": list(skill.expects),
        "summary": _summarise_value(result),
        "agent_trace": sub_trace,
    }
    return {"produces": skill.produces, "value": result, "_provenance": provenance}


@activity.defn(name="run_skill_activity")
def run_skill_activity(skill_name: str, inputs: dict) -> dict:
    """Generic skill runner — used for skills hot-added AFTER worker startup
    (per-skill activities are registered at startup only)."""
    events.set_workflow(activity.info().workflow_id)
    activity.logger.info(f"Running skill: {skill_name}")
    with heartbeat():
        return _run_skill_with_provenance(skill_name, inputs, src_label="activities.py:run_skill_activity")


# --------------------------------------------------------------------------
# Per-skill activity types — registered with Temporal under the skill's
# real name (csv_loader, trend_analysis, weather_fetch, …) so the Temporal
# UI timeline labels each step with what's actually running.
# --------------------------------------------------------------------------
def _make_skill_activity(skill_name: str):
    @activity.defn(name=skill_name)
    def _impl(inputs: dict) -> dict:
        events.set_workflow(activity.info().workflow_id)
        activity.logger.info(f"Running skill: {skill_name}")
        with heartbeat():
            return _run_skill_with_provenance(skill_name, inputs, src_label=f"activities.py:{skill_name}")

    _impl.__name__ = f"{skill_name}_activity"
    _impl.__qualname__ = f"{skill_name}_activity"
    return _impl


# Build one activity per registered skill. worker_mgr.start() calls
# refresh_skill_activities() before each (re)start so newly-dropped skill files
# are picked up — that's the "hot-add skills" demo moment.
SKILL_ACTIVITIES: dict[str, Any] = {}


def refresh_skill_activities() -> list[str]:
    """Re-scan src/skills/ and (re)build SKILL_ACTIVITIES.

    Existing entries for skills that still exist are kept (so we don't churn
    activity defs across every Stop/Start cycle); brand-new skills get a
    fresh activity definition.
    """
    current = set(list_skills())
    for name in list(SKILL_ACTIVITIES.keys()):
        if name not in current:
            SKILL_ACTIVITIES.pop(name, None)
    for name in current:
        if name not in SKILL_ACTIVITIES:
            SKILL_ACTIVITIES[name] = _make_skill_activity(name)
    return sorted(SKILL_ACTIVITIES.keys())


# Initial population so existing imports (worker.py) keep working.
refresh_skill_activities()
