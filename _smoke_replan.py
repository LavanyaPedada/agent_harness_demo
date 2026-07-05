"""Smoke test the deterministic replan remedy without spinning up Temporal."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.orchestrator import _deterministic_remedy

prior_plan = [
    {"step": 1, "skill": "csv_loader", "reason": ""},
    {"step": 2, "skill": "trend_analysis", "reason": ""},
    {"step": 3, "skill": "correlation_analysis", "reason": ""},
    {"step": 4, "skill": "report_generator", "reason": ""},
]
completed = ["csv_loader", "trend_analysis"]
new_plan = _deterministic_remedy(
    prior_plan,
    completed,
    failed_skill="correlation_analysis",
    failure_kind="ToolValidationError",
    failure_message="correlation_analysis cannot run without ['weather.regions']",
)
print("remedied plan:")
for s in new_plan or []:
    print(" ", s)
