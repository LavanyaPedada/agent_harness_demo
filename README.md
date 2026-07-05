# Autonomous Business Analyst Agent

A resilient, self-healing analytics agent that turns a plain-English question about
a sales CSV into an executive answer — and survives having its worker killed
mid-run. Built to showcase modern agent-harness patterns end to end.

Built on:

- **Temporal** — durable workflow orchestration. Kill the worker mid-run, restart it, and the workflow resumes from the next un-completed step (no work repeated).
- **LangGraph** — the agent state graphs (a planner agent and a coding agent).
- **Ollama** — local LLM (`qwen2.5:3b` by default). Runs fully offline; no cloud key required.
- **Open-Meteo** — free historical weather API (no key, no signup) for the enrichment step.

It demonstrates: CSV ingestion → planner agent → coding agent with sandboxed
execution → self-correction after a failure → external API enrichment → durable
resume after a kill → context compaction → operational memory that makes the
agent avoid a mistake it made on a previous run.

---

## What you need

| Requirement | Notes |
|---|---|
| **Python 3.10+** | 3.11–3.13 recommended. |
| **Ollama** | Install from <https://ollama.com>, then pull the model: `ollama pull qwen2.5:3b`. Keep `ollama` running (it serves on `http://localhost:11434`). |
| **Temporal CLI** | Install from <https://docs.temporal.io/cli>. Make sure `temporal` is on your PATH (or set `$env:TEMPORAL_EXE` to its full path). |
| **OS** | The `*.ps1` launch scripts are for Windows PowerShell. macOS/Linux users can run the equivalent commands shown in [Manual commands](#manual-commands-macoslinux). |

> **No cloud LLM key is required.** By default every LLM call runs locally on
> Ollama. There is an optional hosted path (Groq / `gpt-oss-120b`) — see
> [Optional: use Groq](#optional-use-groq-instead-of-ollama). The key, if you use
> it, is read only from the `GROQ_API_KEY` environment variable and is never
> stored in this repo.

---

## Setup (once)

```powershell
# 1. Clone and enter the repo
git clone <your-fork-url> harness_demo
cd harness_demo

# 2. Create a virtual environment named .venv (the scripts look for it here)
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # macOS/Linux: source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Pull the local model (if you haven't already)
ollama pull qwen2.5:3b
```

The launch scripts auto-detect `.venv\Scripts\python.exe`; if it's missing they
fall back to whatever `python` is on your PATH.

---

## Run it — web UI (simplest)

The web app runs a Temporal worker **in-process**, so you only need two things
running: the Temporal server and the app.

```powershell
# Terminal 1 — Temporal dev server (Web UI at http://localhost:8233)
.\start_temporal.ps1

# Terminal 2 — the chat app (starts its own worker automatically)
.\start_app.ps1
```

Then open **<http://localhost:8001>** and ask a question, e.g.
*"Which region had the biggest revenue drop, and did weather play a role?"*

Optional: run `.\start_warmup.ps1` once first to pre-load the model into Ollama
so the first answer isn't slowed by the ~10–15s model-load tax.

---

## Run it — CLI (for the kill-and-resume demo)

Use this flow when you want to kill the worker by hand to show durable resume.
Here the worker is a **separate process** you can `Ctrl+C`.

```powershell
# Terminal 1 — Temporal dev server
.\start_temporal.ps1

# Terminal 2 — the worker (this is the process you'll kill mid-run)
.\start_worker.ps1

# Terminal 3 — kick off a run
.\run.ps1 -ResetMemory        # first run: wipes memory so the "lesson" moment fires
.\run.ps1                     # second run: memory now primes the agent to skip the bug
```

### Kill-and-resume

1. Start a run (Terminal 3).
2. While a step is running, **`Ctrl+C` the worker** (Terminal 2).
3. Terminal 3 keeps polling the same `current_step` — the worker is gone.
4. Restart it: `.\start_worker.ps1`.
5. The workflow resumes from the next un-completed activity. Nothing re-runs.

The Temporal Web UI (<http://localhost:8233>) shows the event history visually.

### Memory persistence

1. `.\run.ps1 -ResetMemory` → the coding agent's first attempt fails, it
   self-corrects, and the lesson is written to `memory/AGENT.md`.
2. Open `memory/AGENT.md` — show the appended lesson.
3. `.\run.ps1` (no reset) → the agent reads the lesson up front and fixes the bug
   on the first attempt.

---

## Optional: use Groq instead of Ollama

To route LLM calls to Groq's hosted `gpt-oss-120b` instead of local Ollama, set
the key in your shell **before** launching (no code change needed):

```powershell
$env:GROQ_API_KEY = "gsk_your_key_here"     # macOS/Linux: export GROQ_API_KEY=...
.\start_app.ps1
```

When `GROQ_API_KEY` is unset (the default), everything runs on Ollama. See
[.env.example](.env.example) for all supported environment knobs.

---

## Manual commands (macOS/Linux)

The `.ps1` files are thin wrappers. The underlying commands are:

```bash
# Temporal dev server
temporal server start-dev --ui-port 8233 --port 7233 --log-level warn

# Web app (starts an in-process worker)
DEMO_FAST=1 DEMO_MODEL=qwen2.5:3b \
  python -m uvicorn app.server:app --host 127.0.0.1 --port 8001

# Standalone worker
DEMO_FAST=1 DEMO_MODEL=qwen2.5:3b python worker.py

# CLI run
python run_demo.py --reset-memory
```

`DEMO_FAST=1` makes the planner use a deterministic plan (skipping one LLM call)
so a first run finishes in ~30–50s; the self-correction and executive-insight
calls stay real.

---

## Demo moments — where to look

| Moment | What you see | Where it lives |
|---|---|---|
| CSV ingestion | Schema + head printed | [src/skills/csv_loader.py](src/skills/csv_loader.py) |
| Planner agent | LLM emits an ordered plan | [src/orchestrator.py](src/orchestrator.py) |
| Coding agent: first attempt fails | `KeyError: 'month'` in stderr | [src/coding_agent.py](src/coding_agent.py) (scripted first bug) |
| Self-correction (real LLM) | LLM diagnoses + rewrites the code | `_analyze` node in `coding_agent.py` |
| Sandbox | Subprocess execution with stderr capture | [src/sandbox.py](src/sandbox.py) |
| External API call | Open-Meteo rainfall per region | [src/skills/weather_fetch.py](src/skills/weather_fetch.py) |
| Kill + resume | `Ctrl+C` worker → restart → resumes | Temporal event history |
| Context compaction | Before/after token counts | [src/compaction.py](src/compaction.py) |
| Skill loading | Plan references skills dynamically | [src/skills/__init__.py](src/skills/__init__.py) |
| Memory update | New entry appended to `memory/AGENT.md` | [src/memory.py](src/memory.py) |
| Second run = no repeat mistake | Agent reads AGENT.md and fixes up front | `_consult_memory` + `_generate` |

---

## Project layout

```
harness_demo/
├── data/sales.csv            # 6 months x 4 regions, with a Q2 drop in South
├── memory/                   # Operational memory (runtime state; git-ignored)
│   ├── AGENT.md              # Human-readable lessons, auto-appended on each new lesson
│   └── learned_patterns.json # Machine-readable lessons used at prompt time
├── src/
│   ├── llm.py                # LLM wrapper (Ollama by default, optional Groq)
│   ├── sandbox.py            # Subprocess sandbox for generated code
│   ├── compaction.py         # Context compaction with a diff event
│   ├── memory.py             # Lesson recorder + AGENT.md updater
│   ├── orchestrator.py       # LangGraph planner agent
│   ├── coding_agent.py       # LangGraph code + sandbox + self-correct loop
│   ├── workflow.py           # Temporal workflow definition
│   ├── activities.py         # Temporal activities (one per skill)
│   ├── events.py             # JSONL event log the UI streams over SSE
│   └── skills/               # Pluggable skills, registered by name
│       ├── csv_loader.py
│       ├── trend_analysis.py
│       ├── weather_fetch.py
│       ├── correlation.py
│       └── report.py
├── app/                      # FastAPI chat UI (server + in-process worker + static frontend)
├── worker.py                 # Standalone Temporal worker entry point
├── run_demo.py               # CLI client — starts/attaches a workflow
└── *.ps1                     # Windows launch scripts
```

---

## What's real vs. scripted

Nothing is mocked:

- Real Temporal server (dev mode, in-memory persistence — durable across worker restarts).
- Real LangGraph state graphs for both the planner and the coding agent.
- Real LLM calls (Ollama, or Groq if configured) for the plan, code correction, executive insight, and compaction summary.
- Real subprocess sandbox executing real generated code with real pandas.
- Real Open-Meteo HTTP call for rainfall data.

The **one** intentional pre-scripting: the coding agent's *first* attempt is
hard-coded buggy code (it references a non-existent `month` column) so the
self-correction loop fires reliably during a live demo. Every subsequent attempt
— including the fix — is a real LLM call. Once memory is populated (or on the
second run), the bug is skipped automatically.
