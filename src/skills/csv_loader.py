"""csv_loader skill: reads a CSV and returns a schema summary."""
from __future__ import annotations

from pathlib import Path
import pandas as pd

from src.skills import Skill


def _handler(csv_path: str) -> dict:
    p = Path(csv_path)
    if not p.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")
    df = pd.read_csv(p)
    return {
        "csv_path": str(p),
        "row_count": int(len(df)),
        "columns": list(df.columns),
        "dtypes": {c: str(df[c].dtype) for c in df.columns},
        "head": df.head(5).to_dict(orient="records"),
    }


SKILL = Skill(
    name="csv_loader",
    description="Reads a CSV file and returns row count, columns, dtypes, and a head sample.",
    handler=_handler,
    expects=["csv_path"],
    produces="csv_summary",
)
