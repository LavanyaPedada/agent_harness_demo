"""Skill registry. Each skill is a callable + metadata.

Skills are loaded dynamically by name. The orchestrator emits a plan that names
skills; we resolve the skill objects at execution time rather than hardcoding
the workflow logic.

Discovery is filesystem-driven: any module under src/skills/ that exports a
top-level `SKILL` (a Skill instance) is picked up. That makes hot-adding a
skill on stage trivial:
    1. Drop a new file, e.g. src/skills/anomaly_detection.py
    2. Click "Reload Skills" (the in-process worker stops + starts; sub-second)
    3. The planner sees it via list_skills() and can use it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Any
import importlib
import pkgutil
from pathlib import Path


class ToolValidationError(ValueError):
    """Raised by a skill (or its validate() hook) when inputs are bad in a way
    the planner could potentially fix by replanning. The workflow catches this
    and routes the failure into the replan path."""

    def __init__(self, message: str, *, missing: list[str] | None = None, skill: str | None = None):
        super().__init__(message)
        self.missing = missing or []
        self.skill = skill


@dataclass
class Skill:
    name: str
    description: str
    handler: Callable[..., Any]
    expects: list[str]   # named inputs from workflow state
    produces: str        # name of output key it writes to workflow state
    requires_approval: bool = False  # if True, workflow blocks on a HITL signal before running
    # Optional input validation. Receives the kwargs about to be passed to handler;
    # raise ToolValidationError to trigger the replan path.
    validate: Callable[..., None] | None = None


_SKILL_DIR = Path(__file__).resolve().parent


def _discover() -> dict[str, str]:
    """Scan src/skills/ for any module that exports a top-level SKILL object."""
    found: dict[str, str] = {}
    for info in pkgutil.iter_modules([str(_SKILL_DIR)]):
        if info.ispkg or info.name.startswith("_"):
            continue
        modname = f"src.skills.{info.name}"
        try:
            mod = importlib.import_module(modname)
        except Exception:
            # Don't let one broken skill take down discovery — silently skip.
            continue
        skill = getattr(mod, "SKILL", None)
        if isinstance(skill, Skill):
            found[skill.name] = modname
    return found


# Recomputed at every module import. The Temporal worker registers a per-skill
# activity for each entry here at startup. To pick up a NEW skill file dropped
# in at runtime, restart the worker (Stop+Start in the UI).
REGISTRY: dict[str, str] = _discover()


def list_skills() -> list[str]:
    # Re-scan on every call so the planner always sees the current set.
    REGISTRY.clear()
    REGISTRY.update(_discover())
    return sorted(REGISTRY.keys())


def load_skill(name: str) -> Skill:
    if name not in REGISTRY:
        list_skills()
    if name not in REGISTRY:
        raise KeyError(f"Unknown skill: {name}. Available: {sorted(REGISTRY.keys())}")
    mod = importlib.import_module(REGISTRY[name])
    skill = getattr(mod, "SKILL", None)
    if skill is None:
        raise AttributeError(f"Module {REGISTRY[name]} has no SKILL export")
    return skill


def skill_meta() -> list[dict]:
    """Compact dict per skill — used by /api/skills and the planner prompt."""
    out = []
    for name in list_skills():
        sk = load_skill(name)
        out.append({
            "name": sk.name,
            "description": sk.description,
            "expects": list(sk.expects),
            "produces": sk.produces,
            "requires_approval": bool(sk.requires_approval),
        })
    return out
