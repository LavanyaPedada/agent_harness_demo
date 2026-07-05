"""trend_analysis skill — wraps the *coding agent* so the LLM writes the
analysis code itself. The skill returns the agent's final result + the trace
of attempts so the demo can show the self-correction loop on screen.

The LLM only has to produce two simple groupbys; the >20% drop detection is
done deterministically here after the agent returns. Keeping the LLM task
small makes the self-correction loop converge in 1-2 attempts on a 7B model.
"""
from __future__ import annotations

import pandas as pd

from src.skills import Skill
from src.coding_agent import run_coding_agent


def _detect_drops(region_monthly: list[dict]) -> list[dict]:
    if not region_monthly:
        return []
    df = pd.DataFrame(region_monthly)
    if not {"month", "region", "revenue"}.issubset(df.columns):
        return []
    df = df.sort_values(["region", "month"])
    df["prev_revenue"] = df.groupby("region")["revenue"].shift(1)
    df["pct_change"] = (df["revenue"] - df["prev_revenue"]) / df["prev_revenue"]
    drops = df[df["pct_change"] <= -0.20].copy()
    drops = drops.sort_values("pct_change")
    return drops[["month", "region", "revenue", "prev_revenue", "pct_change"]].to_dict(orient="records")


def _recompute_from_csv(csv_path: str) -> tuple[list[dict], list[dict]]:
    """Deterministic fallback: if the LLM's output is incomplete, compute the
    same aggregates ourselves from the CSV. The demo can still continue."""
    df = pd.read_csv(csv_path)
    df["month"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m")
    monthly_totals = (
        df.groupby("month")["revenue"].sum().reset_index().to_dict(orient="records")
    )
    region_monthly = (
        df.groupby(["month", "region"])["revenue"].sum().reset_index().to_dict(orient="records")
    )
    return monthly_totals, region_monthly


def _records_have_keys(records: list, required: set[str]) -> bool:
    if not records:
        return False
    first = records[0]
    if not isinstance(first, dict):
        return False
    return required.issubset(first.keys())


def _handler(csv_path: str, csv_summary: dict, question: str) -> dict:
    task = (
        "Aggregate revenue from the CSV. Use the literal key name 'revenue' "
        "(not 'total_revenue', 'sum', or anything else). Output exactly:\n"
        "  - monthly_totals: list of {'month': 'YYYY-MM', 'revenue': <float>} "
        "(month is derived from the date column).\n"
        "  - region_monthly: list of {'month': 'YYYY-MM', 'region': <str>, 'revenue': <float>}.\n\n"
        "Concretely: read csv with pandas, df['month'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m'), "
        "then two groupbys, then save_result({'monthly_totals': ..., 'region_monthly': ...})."
    )
    out = run_coding_agent(
        task=task,
        csv_path=csv_path,
        csv_summary=csv_summary,
        expected_keys=["monthly_totals", "region_monthly"],
    )
    result = out.get("result") or {}
    monthly_totals = result.get("monthly_totals") or []
    region_monthly = result.get("region_monthly") or []

    # If the LLM-generated code never produced a usable artifact (or produced
    # nested records missing required keys), fall back to deterministic compute
    # so the rest of the workflow has clean inputs.
    needs_fallback = (
        not _records_have_keys(monthly_totals, {"month", "revenue"})
        or not _records_have_keys(region_monthly, {"month", "region", "revenue"})
    )
    if needs_fallback:
        monthly_totals_dt, region_monthly_dt = _recompute_from_csv(csv_path)
        if not _records_have_keys(monthly_totals, {"month", "revenue"}):
            monthly_totals = monthly_totals_dt
        if not _records_have_keys(region_monthly, {"month", "region", "revenue"}):
            region_monthly = region_monthly_dt

    drops = _detect_drops(region_monthly)
    return {
        "monthly_totals": monthly_totals,
        "region_monthly": region_monthly,
        "drop_months": drops,
        "_agent_trace": {
            "attempts": out.get("attempts", []),
            "compaction_event": out.get("compaction_event"),
            "lesson_recorded": out.get("lesson_recorded"),
        },
    }


SKILL = Skill(
    name="trend_analysis",
    description="Aggregates revenue trends and identifies drop periods. Uses the coding agent with sandbox + self-correction.",
    handler=_handler,
    expects=["csv_path", "csv_summary", "question"],
    produces="trends",
)


SKILL = Skill(
    name="trend_analysis",
    description="Aggregates revenue trends and identifies drop periods. Uses the coding agent with sandbox + self-correction.",
    handler=_handler,
    expects=["csv_path", "csv_summary", "question"],
    produces="trends",
)
