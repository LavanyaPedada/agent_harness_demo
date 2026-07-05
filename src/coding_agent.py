"""LangGraph coding agent.

State graph:
  consult_memory -> generate -> execute -> evaluate
                                              |--ok--> finalize -> END
                                              `--fail-> analyze -> execute (loop)

Highlights for the talk:
  * `consult_memory` reads memory/learned_patterns.json BEFORE generation, so
    lessons learned in past runs steer the LLM away from prior mistakes.
  * `execute` runs the snippet in a real subprocess sandbox; stderr is fed
    back to the LLM verbatim.
  * `analyze` asks the LLM to (a) name the root cause, then (b) regenerate.
    This is reasoning, not retry.
  * `finalize` records the lesson via memory.remember() if a correction was
    needed — that updates AGENT.md and learned_patterns.json on disk.
"""
from __future__ import annotations

import json
import re
import textwrap
from typing import TypedDict, Annotated, Any

from langgraph.graph import StateGraph, END

from src.llm import chat
from src.sandbox import run_python, load_artifact, SandboxResult
from src.compaction import Conversation
from src import memory as mem
from src import events


MAX_ATTEMPTS = 2


class CodingState(TypedDict, total=False):
    task: str
    csv_path: str
    csv_summary: dict
    expected_keys: list[str]
    attempts: list[dict]
    final_result: dict | None
    conversation_snapshot: dict
    compaction_event: dict | None
    lesson_recorded: dict | None
    force_first_bug: bool


def _extract_code(text: str) -> str:
    """Pull the first python/code block out of an LLM response.

    Smaller models sometimes forget the ```python fence and emit prose like
    'Here is the corrected code:' before the actual snippet, which then breaks
    the sandbox with a SyntaxError. Be defensive:
      1. Try fenced ```python / ```py / ``` blocks first.
      2. If no fence, strip everything before the first python-looking line
         (import / from / def / class / a comment / pd.read_csv / save_result).
      3. Strip trailing prose after the last python-looking line.
    """
    # 1. fenced block (most common case)
    m = re.search(r"```(?:python|py)?\s*\n?(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # 2. no fence — find the first line that looks like Python
    code_starters = (
        "import ", "from ", "def ", "class ", "#",
        "pd.", "save_result(", "print(",
    )
    lines = text.splitlines()
    start_idx = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if any(stripped.startswith(s) for s in code_starters):
            start_idx = i
            break
    if start_idx is None:
        # Last resort: return as-is, sandbox will fail loudly.
        return text.strip()

    # 3. drop trailing pure-prose lines (lines with no code-like punctuation)
    end_idx = len(lines)
    for i in range(len(lines) - 1, start_idx, -1):
        s = lines[i].strip()
        if not s:
            continue
        # Heuristic: real code usually has one of these
        if any(ch in s for ch in "()=:[]{}"):
            end_idx = i + 1
            break

    return "\n".join(lines[start_idx:end_idx]).strip()


def _consult_memory(state: CodingState) -> CodingState:
    convo = Conversation()
    patterns = mem.list_patterns()
    lessons = mem.patterns_for_prompt()
    events.emit(
        "memory.read",
        f"{len(patterns)} prior lesson(s) loaded from AGENT.md",
        {"count": len(patterns), "patterns": [p.get("pattern") for p in patterns]},
        src="coding_agent.py:_consult_memory",
    )
    convo.add(
        "system",
        "Coding agent starting. Prior lessons from memory:\n" + lessons,
    )
    state["conversation_snapshot"] = convo.snapshot()
    state.setdefault("attempts", [])
    return state


def _build_generate_prompt(state: CodingState, lessons: str) -> tuple[str, str]:
    sys = (
        "You are a Python data-analysis coding agent. Reply with EXACTLY one "
        "fenced ```python code block AND NOTHING ELSE — no greeting, no "
        "explanation, no text before or after the fence. Your code runs in a "
        "fresh subprocess; pandas is importable; the helper `save_result(obj)` "
        "is already defined in the runtime — DO NOT redefine or import it, just "
        "call it. You MUST end your snippet with exactly one call: "
        "`save_result(<dict>)` where <dict> has these top-level keys: "
        "{keys}. Without that call the run is considered a failure.\n\n"
        "Skeleton you must follow:\n"
        "```\n"
        "import pandas as pd\n"
        "df = pd.read_csv(r'<csv_path>')\n"
        "df['month'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m')\n"
        "monthly_totals = df.groupby('month')['revenue'].sum().reset_index().to_dict(orient='records')\n"
        "region_monthly = df.groupby(['month','region'])['revenue'].sum().reset_index().to_dict(orient='records')\n"
        "save_result({{'monthly_totals': monthly_totals, 'region_monthly': region_monthly}})\n"
        "```\n\n"
        "HARD RULES (violations cause failure):\n"
        "- DO NOT use Series.first() or NDFrame.first() — those need an offset arg.\n"
        "- DO NOT use df.transform(lambda x: x.first()) or any .first() call.\n"
        "- DO NOT redefine save_result. DO NOT print. End with one save_result(...) call.\n\n"
        "Lessons from memory (apply these BEFORE writing code):\n{lessons}"
    ).format(keys=", ".join(state["expected_keys"]), lessons=lessons)

    schema = json.dumps(
        {
            "csv_path": state["csv_path"],
            "row_count": state["csv_summary"].get("row_count"),
            "columns": state["csv_summary"].get("columns"),
            "dtypes": state["csv_summary"].get("dtypes"),
            "head": state["csv_summary"].get("head"),
        },
        indent=2,
        default=str,
    )
    user = f"TASK:\n{state['task']}\n\nCSV SCHEMA:\n{schema}\n\nWrite the code."
    return sys, user


def _generate(state: CodingState) -> CodingState:
    lessons = mem.patterns_for_prompt()

    # Deterministic first-bug for demo: skip the LLM and hand back code that
    # references a non-existent 'month' column. This guarantees the audience
    # sees the self-correction loop fire on stage. Subsequent attempts go
    # through the real LLM.
    is_first = len(state.get("attempts", [])) == 0
    if state.get("force_first_bug") and is_first and not mem.list_patterns():
        buggy = textwrap.dedent(
            """
            import pandas as pd
            df = pd.read_csv(r"{csv_path}")
            monthly_totals = (
                df.groupby('month')['revenue'].sum().reset_index()
                  .rename(columns={{'revenue': 'revenue'}})
                  .to_dict(orient='records')
            )
            region_monthly = (
                df.groupby(['month', 'region'])['revenue'].sum().reset_index()
                  .to_dict(orient='records')
            )
            save_result({{
                'monthly_totals': monthly_totals,
                'region_monthly': region_monthly,
                'drop_months': [],
            }})
            """
        ).strip().format(csv_path=state["csv_path"])
        state.setdefault("attempts", []).append({"code": buggy, "source": "scripted-first-bug"})
        events.emit(
            "agent.codegen",
            "attempt #1: scripted-first-bug (references missing 'month' column)",
            {"attempt": 1, "source": "scripted-first-bug", "code": buggy},
            src="coding_agent.py:_generate",
        )
        return state

    sys, user = _build_generate_prompt(state, lessons)
    from src.llm import GROQ_MODEL, OLLAMA_MODEL, USE_GROQ
    label = GROQ_MODEL if USE_GROQ else OLLAMA_MODEL
    events.emit("llm.call", f"coding agent generating code ({label})", {"attempt": len(state.get("attempts", [])) + 1, "model": label}, src="coding_agent.py:_generate")
    resp = chat(sys, user, temperature=0.1)
    code = _extract_code(resp)
    state.setdefault("attempts", []).append({"code": code, "source": "llm"})
    events.emit(
        "agent.codegen",
        f"attempt #{len(state['attempts'])}: code from LLM",
        {"attempt": len(state["attempts"]), "source": "llm", "code": code},
        src="coding_agent.py:_generate",
    )
    return state


def _execute(state: CodingState) -> CodingState:
    attempt = state["attempts"][-1]
    events.emit("sandbox.exec", f"attempt #{len(state['attempts'])} running in subprocess", {"attempt": len(state["attempts"])}, src="coding_agent.py:_execute")
    res: SandboxResult = run_python(attempt["code"], timeout=45)
    artifact = load_artifact(res) if res.ok else None
    attempt.update(
        exit_code=res.exit_code,
        stdout=res.stdout,
        stderr=res.stderr,
        ok=res.ok,
        artifact=artifact,
    )
    if res.ok:
        events.emit(
            "sandbox.ok",
            f"attempt #{len(state['attempts'])} exit=0 ✓",
            {"attempt": len(state["attempts"]), "exit_code": res.exit_code, "has_artifact": artifact is not None},
            src="coding_agent.py:_execute",
        )
    else:
        events.emit(
            "sandbox.fail",
            f"attempt #{len(state['attempts'])} exit={res.exit_code} ✗",
            {"attempt": len(state["attempts"]), "exit_code": res.exit_code, "stderr": (res.stderr or "")[-1000:]},
            src="coding_agent.py:_execute",
        )

    convo_snap = state.get("conversation_snapshot") or {"entries": [], "summaries": []}
    convo = Conversation()
    convo.entries = list(convo_snap.get("entries", []))
    convo.summaries = list(convo_snap.get("summaries", []))
    convo.add(
        "agent",
        f"Attempt #{len(state['attempts'])} code:\n{attempt['code'][:600]}",
    )
    convo.add(
        "sandbox",
        f"exit={res.exit_code}\nstdout={res.stdout[:400]}\nstderr={res.stderr[:600]}",
    )
    if convo.needs_compaction():
        event = convo.compact()
        state["compaction_event"] = event
    state["conversation_snapshot"] = convo.snapshot()
    return state


def _evaluate(state: CodingState) -> str:
    last = state["attempts"][-1]
    sandbox_ok = last.get("ok")
    artifact = last.get("artifact")

    if sandbox_ok and artifact is None:
        # Snippet ran but never called save_result(). Tell the LLM explicitly.
        last["ok"] = False
        last["stderr"] = (
            (last.get("stderr") or "")
            + "\nValidator: snippet completed but did NOT call save_result(...). "
              "You MUST end the snippet with exactly one save_result(<dict>) call."
        )
    elif sandbox_ok and artifact is not None:
        missing = [k for k in state["expected_keys"] if k not in artifact]
        if missing:
            last["ok"] = False
            last["stderr"] = (
                (last.get("stderr") or "")
                + f"\nValidator: save_result was called but is missing keys: {missing}"
            )
        else:
            return "finalize"

    if len(state["attempts"]) >= MAX_ATTEMPTS:
        return "finalize"
    return "analyze"


def _analyze(state: CodingState) -> CodingState:
    last = state["attempts"][-1]
    sys = (
        "You are a Python debugging agent. The previous attempt failed inside a "
        "subprocess sandbox. Reply with EXACTLY one fenced ```python code block "
        "AND NOTHING ELSE — no diagnosis sentence, no greeting, no text before "
        "or after the fence. The runtime already defines `save_result(obj)` — do "
        "NOT redefine or import it. Your corrected snippet MUST end with exactly "
        "one call: `save_result(<dict>)` where <dict> has these keys: {keys}. "
        "Do not print results — call save_result instead."
    ).format(keys=", ".join(state["expected_keys"]))
    schema = json.dumps(
        {
            "csv_path": state["csv_path"],
            "columns": state["csv_summary"].get("columns"),
            "dtypes": state["csv_summary"].get("dtypes"),
            "head": state["csv_summary"].get("head"),
        },
        indent=2,
        default=str,
    )
    user = (
        f"TASK:\n{state['task']}\n\n"
        f"CSV SCHEMA:\n{schema}\n\n"
        f"FAILED CODE:\n```python\n{last['code']}\n```\n\n"
        f"STDERR:\n{last.get('stderr','')[:1500]}\n\n"
        "Diagnose then output the corrected code."
    )
    from src.llm import GROQ_MODEL, OLLAMA_MODEL, USE_GROQ
    label = GROQ_MODEL if USE_GROQ else OLLAMA_MODEL
    events.emit("llm.diagnose", f"LLM analysing failure ({label})", {"attempt": len(state["attempts"]), "model": label}, src="coding_agent.py:_analyze")
    resp = chat(sys, user, temperature=0.1)
    code = _extract_code(resp)
    state["attempts"].append({"code": code, "source": "llm-correction", "diagnosis": resp[:400]})
    diagnosis_line = (resp.strip().splitlines() or [""])[0][:200]
    events.emit(
        "agent.codegen",
        f"attempt #{len(state['attempts'])}: corrected code from LLM",
        {"attempt": len(state["attempts"]), "source": "llm-correction", "code": code, "diagnosis": diagnosis_line},
        src="coding_agent.py:_analyze",
    )
    return state


def _finalize(state: CodingState) -> CodingState:
    last = state["attempts"][-1]
    state["final_result"] = last.get("artifact")

    # If we needed >1 attempt and we eventually produced a valid artifact, record the lesson.
    if len(state["attempts"]) > 1 and last.get("ok") and last.get("artifact") is not None:
        first_err = state["attempts"][0].get("stderr", "")
        # Try to pull a tidy pattern out of the stderr.
        m = re.search(r"KeyError:\s*'?([^'\n]+)'?", first_err)
        if m:
            pattern = f"KeyError on column '{m.group(1)}' when grouping/sorting"
            fix = (
                f"Verify the column exists in df.columns before use. If '{m.group(1)}' is "
                "a derived value (e.g. month from date), derive it explicitly with "
                "pd.to_datetime(df['date']).dt.to_period('M') before grouping."
            )
        else:
            pattern = "Sandbox execution failed on first attempt"
            fix = "Inspect stderr, derive missing fields explicitly, re-run."
        entry = mem.remember(
            pattern=pattern,
            fix=fix,
            evidence=first_err.strip().splitlines()[-1] if first_err.strip() else "",
            task_kind="trend_analysis",
        )
        state["lesson_recorded"] = entry
    return state


def build_graph():
    g = StateGraph(CodingState)
    g.add_node("consult_memory", _consult_memory)
    g.add_node("generate", _generate)
    g.add_node("execute", _execute)
    g.add_node("analyze", _analyze)
    g.add_node("finalize", _finalize)
    g.set_entry_point("consult_memory")
    g.add_edge("consult_memory", "generate")
    g.add_edge("generate", "execute")
    g.add_conditional_edges("execute", _evaluate, {"analyze": "analyze", "finalize": "finalize"})
    g.add_edge("analyze", "execute")
    g.add_edge("finalize", END)
    return g.compile()


_GRAPH = None


def _graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


def run_coding_agent(
    *,
    task: str,
    csv_path: str,
    csv_summary: dict,
    expected_keys: list[str],
    force_first_bug: bool = True,
) -> dict:
    """Public entry — invoked by skills that need code generated + run."""
    initial: CodingState = {
        "task": task,
        "csv_path": csv_path,
        "csv_summary": csv_summary,
        "expected_keys": expected_keys,
        "attempts": [],
        "force_first_bug": force_first_bug,
    }
    final_state = _graph().invoke(initial)
    return {
        "result": final_state.get("final_result"),
        "attempts": final_state.get("attempts", []),
        "compaction_event": final_state.get("compaction_event"),
        "lesson_recorded": final_state.get("lesson_recorded"),
        "conversation_snapshot": final_state.get("conversation_snapshot"),
    }
