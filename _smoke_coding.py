"""Smoke test: end-to-end coding agent with real Ollama + sandbox + memory."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import memory as mem
mem.reset()
from src.skills.csv_loader import SKILL as CSV
from src.coding_agent import run_coding_agent

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sales.csv")
summary = CSV.handler(CSV_PATH)

out = run_coding_agent(
    task="Aggregate revenue by month and by region. Find drop_months where revenue dropped >20% MoM.",
    csv_path=CSV_PATH,
    csv_summary=summary,
    expected_keys=["monthly_totals", "region_monthly", "drop_months"],
)

print("attempts:", len(out["attempts"]))
for i, a in enumerate(out["attempts"], 1):
    tail = (a.get("stderr", "").strip().splitlines() or ["(none)"])[-1]
    print(f"  #{i} source={a.get('source')} ok={a.get('ok')} has_artifact={a.get('artifact') is not None}")
    print(f"      stderr_tail: {tail[:160]}")
print("lesson recorded:", out.get("lesson_recorded") is not None)
print("result keys:", list((out.get("result") or {}).keys()))
res = out.get("result") or {}
print("drop_months:", res.get("drop_months"))
print("monthly_totals (first 6):", (res.get("monthly_totals") or [])[:6])
