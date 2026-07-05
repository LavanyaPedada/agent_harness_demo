"""weather_fetch skill — calls Open-Meteo historical archive (no API key).

Fetches monthly rainfall totals for one representative coordinate per region
across the date range covered by the input data. Open-Meteo's archive endpoint
is free and does not require auth — perfect for a live demo.
"""
from __future__ import annotations

import datetime as dt
import time
import httpx
import pandas as pd

from src.skills import Skill
from src import events

REGION_COORDS = {
    "North": (28.61, 77.21),   # Delhi
    "South": (12.97, 77.59),   # Bangalore
    "East":  (22.57, 88.36),   # Kolkata
    "West":  (19.07, 72.88),   # Mumbai
}

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def _fetch_one(lat: float, lon: float, start: str, end: str) -> pd.DataFrame:
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start,
        "end_date": end,
        "daily": "precipitation_sum",
        "timezone": "auto",
    }
    t0 = time.time()
    with httpx.Client(timeout=30.0) as client:
        r = client.get(ARCHIVE_URL, params=params)
        r.raise_for_status()
        data = r.json()
    elapsed_ms = int((time.time() - t0) * 1000)
    events.emit(
        "http.request",
        f"GET archive-api.open-meteo.com → {r.status_code} ({elapsed_ms}ms)",
        {"url": ARCHIVE_URL, "params": params, "status": r.status_code, "elapsed_ms": elapsed_ms},
        src="weather_fetch.py:_fetch_one",
    )
    daily = data.get("daily", {})
    return pd.DataFrame({
        "date": daily.get("time", []),
        "precipitation_mm": daily.get("precipitation_sum", []),
    })


def _handler(csv_path: str, csv_summary: dict) -> dict:
    df = pd.read_csv(csv_path)
    df["date"] = pd.to_datetime(df["date"])
    start = df["date"].min().strftime("%Y-%m-%d")
    end = df["date"].max().strftime("%Y-%m-%d")

    regions = sorted(df["region"].unique().tolist())
    out: dict = {"window": {"start": start, "end": end}, "regions": {}}
    for region in regions:
        if region not in REGION_COORDS:
            continue
        lat, lon = REGION_COORDS[region]
        wdf = _fetch_one(lat, lon, start, end)
        wdf["date"] = pd.to_datetime(wdf["date"])
        wdf["month"] = wdf["date"].dt.to_period("M").astype(str)
        monthly = wdf.groupby("month")["precipitation_mm"].sum().reset_index()
        out["regions"][region] = {
            "lat": lat,
            "lon": lon,
            "monthly_precip_mm": monthly.to_dict(orient="records"),
        }
    return out


SKILL = Skill(
    name="weather_fetch",
    description="Fetches monthly rainfall per region from Open-Meteo historical archive (no API key required).",
    handler=_handler,
    expects=["csv_path", "csv_summary"],
    produces="weather",
    # External HTTP egress — gated behind a human-in-the-loop approval when
    # HITL is enabled on the workflow. The UI shows an "Approve" button.
    requires_approval=True,
)
