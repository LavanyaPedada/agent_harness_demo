"""report_generator skill — synthesises the final insight DETERMINISTICALLY
from the evidence already produced by trend_analysis + weather_fetch +
correlation_analysis. By this stage every number we need is computed; the
LLM was only re-wording them. A template produces the same answer in <1s
instead of ~80s on CPU."""
from __future__ import annotations

from src.skills import Skill


def _fmt_pct(x) -> str:
    if x is None:
        return "n/a"
    try:
        return f"{float(x) * 100:+.1f}%"
    except Exception:
        return "n/a"


def _fmt_r(x) -> str:
    if x is None:
        return "n/a"
    try:
        return f"{float(x):+.2f}"
    except Exception:
        return "n/a"


def _classify_corr(r) -> str:
    if r is None:
        return "no signal"
    a = abs(float(r))
    if a >= 0.6:
        return "strong"
    if a >= 0.3:
        return "moderate"
    return "weak"


def _handler(question: str, trends: dict, weather: dict, correlation: dict) -> dict:
    drops = (trends or {}).get("drop_months", []) or []
    correlations = (correlation or {}).get("correlations", []) or []
    regions_in_data = sorted({(r.get("region") or "?") for r in (trends or {}).get("region_monthly", []) or []})

    sentences: list[str] = []

    # 1. Conclusion: biggest drop
    if drops:
        worst = drops[0]  # already sorted by pct_change ascending in trend_analysis
        sentences.append(
            f"The biggest revenue drop in the period was in {worst.get('region', '?')} during {worst.get('month', '?')} "
            f"({_fmt_pct(worst.get('pct_change'))} vs. prior month, {worst.get('revenue', 0):,.0f} "
            f"down from {worst.get('prev_revenue', 0):,.0f})."
        )
    else:
        sentences.append("No month-over-month drops of >20% were detected in the data.")

    # 2. Other notable drops
    others = drops[1:4] if len(drops) > 1 else []
    if others:
        items = ", ".join(f"{d.get('region', '?')} {d.get('month', '?')} ({_fmt_pct(d.get('pct_change'))})" for d in others)
        sentences.append(f"Other notable drops: {items}.")

    # 3. Correlation findings
    if correlations:
        # Strongest absolute correlation (positive or negative)
        strongest = max(correlations, key=lambda c: abs((c.get("pearson_r") or 0)))
        r = strongest.get("pearson_r")
        kind = _classify_corr(r)
        direction = "negative" if (r is not None and r < 0) else "positive" if r is not None else "indeterminate"
        sentences.append(
            f"Correlation with rainfall (Open-Meteo) was {kind} and {direction} in "
            f"{strongest.get('region', '?')} (Pearson r = {_fmt_r(r)}, n = {strongest.get('n_months', 0)} months)."
        )
        # Show the rest at a glance
        if len(correlations) > 1:
            rest = ", ".join(f"{c.get('region', '?')} r={_fmt_r(c.get('pearson_r'))}" for c in correlations if c is not strongest)
            sentences.append(f"Other regions: {rest}.")
    else:
        sentences.append("Rainfall correlation could not be computed — insufficient overlapping monthly data.")

    # 4. Coverage / qualifier
    if regions_in_data:
        sentences.append(f"Coverage: {len(regions_in_data)} region(s) in the input — {', '.join(regions_in_data)}.")

    insight = " ".join(sentences)

    evidence = {
        "question": question,
        "drop_months": drops[:10],
        "correlations": correlations[:6],
        "regions_in_data": regions_in_data,
    }
    return {"insight": insight, "evidence": evidence, "source": "deterministic-template"}


SKILL = Skill(
    name="report_generator",
    description="Synthesises the final executive insight from trends + weather + correlation.",
    handler=_handler,
    expects=["question", "trends", "weather", "correlation"],
    produces="report",
)
