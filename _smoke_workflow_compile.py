"""Confirm the workflow class is well-formed under Temporal's sandbox checker."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.workflow import RevenueAnalysisWorkflow

cls = RevenueAnalysisWorkflow

print("class:", cls.__name__)
print("has _gate_approval:", hasattr(cls, "_gate_approval"))
print("has approve_step signal:", hasattr(cls, "approve_step"))
print("has deny_step signal:", hasattr(cls, "deny_step"))
print("has status query:", hasattr(cls, "status"))

# Build an instance just to exercise __init__ (Temporal allows this outside a worker).
inst = cls()
print("init state:")
print("  current_step:", inst._current_step)
print("  approvals:", inst._approvals)
print("  denials:", inst._denials)
print("  hitl_enabled:", inst._hitl_enabled)
print("  pending_approval:", inst._pending_approval)
print("  replan_count:", inst._replan_count)

print("status query result:")
for k, v in inst.status().items():
    print(f"  {k}: {v}")

import temporalio
print("temporalio version:", temporalio.__version__ if hasattr(temporalio, "__version__") else "?")
print("OK")
