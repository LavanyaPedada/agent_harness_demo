"""correlation_analysis skill — correlates monthly rainfall vs revenue per region."""
from __future__ import annotations

import pandas as pd

from src.skills import Skill, ToolValidationError


def _validate(trends: dict, weather: dict) -> None:
    """Strict input validation. Raises ToolValidationError with structured
    `missing` info so the workflow's replan path knows what to fix."""
    missing: list[str] = []
    if not isinstance(trends, dict) or not trends.get("region_monthly"):
        missing.append("trends.region_monthly")
    if not isinstance(weather, dict) or not weather.get("regions"):
        missing.append("weather.regions")
    if missing:
        raise ToolValidationError(
            f"correlation_analysis cannot run without {missing}. "
            "Replan to ensure trend_analysis and weather_fetch ran first.",
            missing=missing,
            skill="correlation_analysis",
        )


def _handler(trends: dict, weather: dict) -> dict:
    region_monthly = pd.DataFrame(trends.get("region_monthly", []))
    if region_monthly.empty:
        return {"correlations": [], "note": "no region_monthly data"}

    # Be defensive about the schema the upstream coding agent produced.
    # LLM-generated code occasionally drops the 'month' key from the records;
    # in that case we can't correlate, but we shouldn't crash the workflow.
    required = {"month", "region", "revenue"}
    missing = required - set(region_monthly.columns)
    if missing:
        return {
            "correlations": [],
            "note": f"region_monthly is missing columns {sorted(missing)} — cannot correlate",
        }

    correlations = []
    for region, payload in weather.get("regions", {}).items():
        wdf = pd.DataFrame(payload.get("monthly_precip_mm", []))
        if wdf.empty or "month" not in wdf.columns:
            continue
        rdf = region_monthly[region_monthly["region"] == region][["month", "revenue"]].copy()
        if rdf.empty:
            continue
        rdf["month"] = rdf["month"].astype(str)
        wdf["month"] = wdf["month"].astype(str)
        merged = rdf.merge(wdf, on="month", how="inner")
        if len(merged) < 3:
            continue
        corr = merged["revenue"].corr(merged["precipitation_mm"])
        correlations.append({
            "region": region,
            "pearson_r": float(corr) if pd.notna(corr) else None,
            "n_months": int(len(merged)),
            "merged_sample": merged.head(12).to_dict(orient="records"),
        })

    correlations.sort(key=lambda c: (c["pearson_r"] or 0))
    return {"correlations": correlations}


SKILL = Skill(
    name="correlation_analysis",
    description="Computes per-region Pearson correlation between monthly rainfall and revenue.",
    handler=_handler,
    expects=["trends", "weather"],
    produces="correlation",
    validate=_validate,
)
