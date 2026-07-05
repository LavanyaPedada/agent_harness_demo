"""Subprocess sandbox for executing agent-generated Python code.

Runs code in an isolated working directory under a fresh Python process so
import-time errors and runtime errors are captured cleanly. Returns the
stdout / stderr / exit_code triple that the coding agent inspects to decide
whether to self-correct.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path

SANDBOX_ROOT = Path(__file__).resolve().parent.parent / "sandbox_workdir"
SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    artifact_path: str | None = None  # path to result.json the snippet writes
    code: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def to_dict(self) -> dict:
        return asdict(self)


def run_python(code: str, timeout: int = 60, extra_env: dict | None = None) -> SandboxResult:
    """Run a python snippet in a fresh subprocess.

    The snippet runs with cwd set to a per-call sandbox directory and has the
    project root on PYTHONPATH (so it can import skills if asked to).
    """
    run_id = uuid.uuid4().hex[:8]
    work = SANDBOX_ROOT / run_id
    work.mkdir(parents=True, exist_ok=True)

    script = work / "snippet.py"
    # Inject a helper for snippets to write a structured result and a hint about cwd.
    preamble = textwrap.dedent(
        f"""
        import json as _json, os as _os, sys as _sys
        _ARTIFACT = _os.path.join(_os.getcwd(), 'result.json')
        def save_result(obj):
            with open(_ARTIFACT, 'w', encoding='utf-8') as _f:
                _json.dump(obj, _f, default=str, indent=2)
        # --- agent code below ---
        """
    ).strip() + "\n"
    script.write_text(preamble + code, encoding="utf-8")

    env = os.environ.copy()
    project_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = project_root + os.pathsep + env.get("PYTHONPATH", "")
    if extra_env:
        env.update(extra_env)

    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(work),
            env=env,
        )
        artifact = work / "result.json"
        return SandboxResult(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            artifact_path=str(artifact) if artifact.exists() else None,
            code=code,
        )
    except subprocess.TimeoutExpired as e:
        return SandboxResult(
            exit_code=-1,
            stdout=e.stdout or "",
            stderr=f"TIMEOUT after {timeout}s\n{e.stderr or ''}",
            code=code,
        )


def load_artifact(result: SandboxResult) -> dict | None:
    if not result.artifact_path:
        return None
    p = Path(result.artifact_path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
