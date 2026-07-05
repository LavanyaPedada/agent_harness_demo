"""RevenueAnalysisWorkflow — durable orchestration on top of Temporal.

Flow:
  1. plan_activity                 -> ordered list of skills
  2. for each step in the plan:
        execute the skill activity, accumulate output + provenance
        if the activity raises ToolValidationError -> replan_activity ->
            swap in the new plan and resume from the failed step.
        if the skill is marked requires_approval AND HITL is enabled ->
            wait for an `approve_step` signal before running.
  3. return final workflow state.

Why this is durable:
  * Every activity completion is persisted in Temporal's event history.
  * Worker restart resumes from the next un-completed activity.
  * Activities have configurable retry policies — transient failures auto-retry.

What's new in v2:
  * `_provenance`           — per-output metadata (skill, ts, elapsed, attempts).
  * Replan-on-failure        — workflow catches ApplicationError("ToolValidationError")
                               and calls replan_activity to rewrite the remaining plan.
  * HITL approval gating    — `requires_approval` skills block on `approve_step`.
  * Demo failure injection  — `failure_mode="drop_weather"` strips weather_fetch
                               from the initial plan so the audience sees the
                               replan path fire.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from src.activities import plan_activity, replan_activity, run_skill_activity


SKILL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=4),
    maximum_attempts=5,
    # ToolValidationError is structured — don't retry it; let the workflow
    # catch it and route into replan.
    non_retryable_error_types=["ToolValidationError"],
)
HEARTBEAT_TIMEOUT = timedelta(seconds=10)
MAX_REPLANS = 2  # hard cap — don't loop forever in a degenerate plan/replan cycle


@workflow.defn(name="RevenueAnalysisWorkflow")
class RevenueAnalysisWorkflow:
    def __init__(self) -> None:
        self._current_step: str = "init"
        self._plan: list[dict] = []
        self._completed_steps: list[str] = []
        self._approvals: set[str] = set()  # skills user approved
        self._denials: set[str] = set()    # skills user denied
        self._approval_required: set[str] = set()  # skills the planner says need it
        self._hitl_enabled: bool = False
        self._pending_approval: str | None = None
        self._replan_count: int = 0
        self._failures: list[dict] = []

    # -- queries --
    @workflow.query
    def status(self) -> dict:
        return {
            "current_step": self._current_step,
            "plan": self._plan,
            "completed_steps": self._completed_steps,
            "pending_approval": self._pending_approval,
            "approvals": sorted(self._approvals),
            "denials": sorted(self._denials),
            "approval_required": sorted(self._approval_required),
            "hitl_enabled": self._hitl_enabled,
            "replan_count": self._replan_count,
            "failures": self._failures,
        }

    # -- signals (HITL) --
    @workflow.signal
    def approve_step(self, skill: str) -> None:
        self._approvals.add(skill)

    @workflow.signal
    def deny_step(self, skill: str) -> None:
        self._denials.add(skill)

    # -- internals --
    async def _gate_approval(self, skill: str) -> bool:
        """Block until user approves/denies. Returns True to proceed, False to skip."""
        if not self._hitl_enabled:
            return True
        if skill not in self._approval_required:
            return True
        if skill in self._approvals:
            return True
        if skill in self._denials:
            return False
        self._pending_approval = skill
        # Wait until the user either approves or denies via signal.
        await workflow.wait_condition(
            lambda: skill in self._approvals or skill in self._denials,
        )
        self._pending_approval = None
        return skill in self._approvals

    async def _run_skill(self, skill_name: str, state: dict[str, Any]) -> dict:
        """Try the per-skill activity first; fall back to the generic
        run_skill_activity if Temporal doesn't know that skill (which happens
        for skills hot-added AFTER worker startup)."""
        try:
            return await workflow.execute_activity(
                skill_name,
                state,
                start_to_close_timeout=timedelta(minutes=20),
                heartbeat_timeout=HEARTBEAT_TIMEOUT,
                retry_policy=SKILL_RETRY,
            )
        except ApplicationError as e:
            # "Activity function ... not registered" — fall back to generic.
            msg = str(e).lower()
            if "not registered" in msg or "unknown activity" in msg:
                return await workflow.execute_activity(
                    run_skill_activity,
                    args=[skill_name, state],
                    start_to_close_timeout=timedelta(minutes=20),
                    heartbeat_timeout=HEARTBEAT_TIMEOUT,
                    retry_policy=SKILL_RETRY,
                )
            raise

    @workflow.run
    async def run(
        self,
        question: str,
        csv_path: str,
        hitl_enabled: bool = False,
        failure_mode: str = "",  # "" | "drop_weather"
    ) -> dict:
        self._hitl_enabled = bool(hitl_enabled)

        # --- Step: planner ---
        self._current_step = "planner"
        plan_result = await workflow.execute_activity(
            plan_activity,
            args=[question, csv_path],
            start_to_close_timeout=timedelta(minutes=15),
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=SKILL_RETRY,
        )
        self._plan = plan_result["plan"]
        self._approval_required = set(plan_result.get("approval_required_skills", []))

        # Demo-only failure injection: strip weather_fetch from the plan so
        # correlation_analysis fails validation -> replan path fires on stage.
        if failure_mode == "drop_weather":
            self._plan = [s for s in self._plan if s["skill"] != "weather_fetch"]
            self._plan = [{"step": i + 1, **{k: v for k, v in s.items() if k != "step"}} for i, s in enumerate(self._plan)]

        # Accumulated workflow state passed into each skill.
        state: dict[str, Any] = {"question": question, "csv_path": csv_path}
        provenance: dict[str, dict] = {}

        # --- Step: execute the plan, with replan on failure ---
        idx = 0
        while idx < len(self._plan):
            step = self._plan[idx]
            skill = step["skill"]
            self._current_step = skill

            # HITL gate
            allowed = await self._gate_approval(skill)
            if not allowed:
                # User denied — record and skip.
                self._failures.append({"skill": skill, "reason": "denied-by-user"})
                idx += 1
                continue

            try:
                out = await self._run_skill(skill, state)
            except ActivityError as ae:
                # Unwrap to find the original cause.
                cause = ae.cause if hasattr(ae, "cause") else None
                kind = ""
                msg = str(ae)
                if isinstance(cause, ApplicationError):
                    kind = cause.type or ""
                    msg = str(cause)

                self._failures.append({"skill": skill, "kind": kind or "ActivityError", "message": msg[:600]})

                # Only ToolValidationError gets the replan treatment. Anything
                # else (e.g. transient infra error after retries) still raises.
                if kind != "ToolValidationError" or self._replan_count >= MAX_REPLANS:
                    raise

                self._replan_count += 1
                rep = await workflow.execute_activity(
                    replan_activity,
                    args=[
                        question,
                        csv_path,
                        list(self._plan),
                        list(self._completed_steps),
                        skill,
                        kind,
                        msg,
                    ],
                    start_to_close_timeout=timedelta(minutes=10),
                    heartbeat_timeout=HEARTBEAT_TIMEOUT,
                    retry_policy=SKILL_RETRY,
                )
                # Splice: keep already-completed prefix, replace remaining
                # tail with the new plan's steps.
                completed_prefix = [s for s in self._plan if s["skill"] in self._completed_steps]
                new_tail = rep.get("plan", [])
                self._plan = []
                for i, s in enumerate(completed_prefix + new_tail, 1):
                    self._plan.append({"step": i, "skill": s["skill"], "reason": s.get("reason", "")})
                self._approval_required = set(rep.get("approval_required_skills", []))
                # Resume from the first non-completed step.
                idx = len(completed_prefix)
                continue

            # Success — accumulate state + provenance, advance.
            state[out["produces"]] = out["value"]
            if "_provenance" in out:
                provenance[out["produces"]] = out["_provenance"]
            self._completed_steps.append(skill)
            idx += 1

        self._current_step = "done"
        # Stash provenance into state so the result blob carries it.
        state["_provenance"] = provenance
        return {
            "plan": self._plan,
            "completed_steps": self._completed_steps,
            "state": state,
            "failures": self._failures,
            "replan_count": self._replan_count,
        }
