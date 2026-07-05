"""anomaly_detection skill — DEMO ASSET, not auto-loaded.

This file lives outside src/skills/ on purpose. To demo "hot-add a new skill":
    1. Copy this file into src/skills/anomaly_detection.py
    2. In the UI topbar, click "Reload Skills" (the in-process worker stops
       and starts; sub-second).
    3. Ask a question like "any anomalies in monthly revenue?". The planner
       sees anomaly_detection in the catalog and uses it.

The skill flags any month whose revenue is more than 1.5 standard deviations
below the mean across all months — a deterministic z-score check, no LLM.
"""
from __future__ import annotations

import statistics
from typing import Any

from src.skills import Skill


def _handler(trends: dict) -> dict:
    monthly = (trends or {}).get("monthly_totals") or []
    if len(monthly) < 3:
        return {"anomalies": [], "note": "need >=3 months of data"}

    revenues = [float(m.get("revenue", 0)) for m in monthly]
    mu = statistics.fmean(revenues)
    sigma = statistics.pstdev(revenues) or 1.0

    anomalies: list[dict] = []
    for m in monthly:
        r = float(m.get("revenue", 0))
        z = (r - mu) / sigma
        if z <= -1.5:
            anomalies.append({
                "month": m.get("month"),
                "revenue": r,
                "z_score": round(z, 2),
                "mean": round(mu, 2),
                "stdev": round(sigma, 2),
            })
    anomalies.sort(key=lambda a: a["z_score"])
    return {"anomalies": anomalies, "mean": round(mu, 2), "stdev": round(sigma, 2)}


def _validate(trends: dict) -> None:
    from src.skills import ToolValidationError
    if not isinstance(trends, dict) or not trends.get("monthly_totals"):
        raise ToolValidationError(
            "anomaly_detection requires trends.monthly_totals; run trend_analysis first.",
            missing=["trends.monthly_totals"],
            skill="anomaly_detection",
        )


SKILL = Skill(
    name="anomaly_detection",
    description="Flags monthly revenue points >1.5 stdev below the mean (z-score).",
    handler=_handler,
    expects=["trends"],
    produces="anomalies",
    validate=_validate,
)
