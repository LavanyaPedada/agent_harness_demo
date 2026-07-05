"""FastAPI server for the analytics chat UI.

Endpoints:
  GET  /                    -> static index.html
  POST /api/chat            -> {question, mode?, workflow_id?, csv_path?}
                               If mode == 'warm' AND workflow_id is given,
                               answer from cached state via a single LLM call.
                               Otherwise spin up a new Temporal workflow and
                               return its workflow_id.
  GET  /api/events/{wf_id}  -> SSE stream of harness events
  GET  /api/status/{wf_id}  -> snapshot of workflow status (current_step,
                               plan, completed_steps, execution_status)
  GET  /api/result/{wf_id}  -> blocks until workflow done, returns final
                               state (used after SSE shows step=done)
  GET  /api/health          -> {temporal: bool, ollama: bool, memory_count}
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Make src importable when uvicorn launches us
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import httpx
from temporalio.client import Client

from app import chat_session
from app.worker_mgr import manager as worker_manager
from src import events
from src import memory as mem

STATIC_DIR = Path(__file__).resolve().parent / "static"
TEMPORAL_TARGET = os.environ.get("TEMPORAL_TARGET", "localhost:7233")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

app = FastAPI(title="Analytics Agent UI")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    """Disable browser caching for all our HTML/JS/CSS so the user always gets
    the latest code during demo iteration. Without this, browsers happily
    serve stale app.js even after server-side edits."""
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict:
    temporal_ok = False
    try:
        await asyncio.wait_for(Client.connect(TEMPORAL_TARGET), timeout=2.0)
        temporal_ok = True
    except Exception:
        pass

    ollama_ok = False
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{OLLAMA_HOST}/api/tags")
            ollama_ok = r.status_code == 200
    except Exception:
        pass

    return {
        "temporal": temporal_ok,
        "ollama": ollama_ok,
        "memory_count": len(mem.list_patterns()),
        "temporal_ui": "http://localhost:8233",
    }


@app.post("/api/chat")
async def chat_endpoint(req: Request) -> JSONResponse:
    body = await req.json()
    question = (body.get("question") or "").strip()
    mode = body.get("mode") or "auto"  # auto | cold | warm
    wf_id = body.get("workflow_id")
    csv_path = body.get("csv_path")
    hitl_enabled = bool(body.get("hitl", False))
    failure_mode = body.get("failure_mode") or ""

    if not question:
        return JSONResponse({"error": "question is required"}, status_code=400)

    if mode == "warm" and wf_id:
        out = chat_session.warm_answer(question, wf_id)
        return JSONResponse({"mode": "warm", "workflow_id": wf_id, **out})

    new_wf = await chat_session.start_cold_workflow(
        question, csv_path, hitl_enabled=hitl_enabled, failure_mode=failure_mode,
    )
    return JSONResponse({
        "mode": "cold",
        "workflow_id": new_wf,
        "hitl": hitl_enabled,
        "failure_mode": failure_mode,
        "temporal_ui": f"http://localhost:8233/namespaces/default/workflows/{new_wf}",
    })


@app.get("/api/status/{wf_id}")
async def status_endpoint(wf_id: str) -> JSONResponse:
    s = await chat_session.workflow_status(wf_id)
    return JSONResponse(s)


@app.get("/api/result/{wf_id}")
async def result_endpoint(wf_id: str) -> JSONResponse:
    try:
        result = await chat_session.fetch_workflow_result(wf_id)
        return JSONResponse({"result": result})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/events/{wf_id}")
async def events_stream(wf_id: str, request: Request, since: int = 0) -> StreamingResponse:
    """Server-Sent Events. Client opens this once per workflow_id and keeps
    the connection open; we yield each new event as soon as it's written."""

    async def event_gen():
        loop = asyncio.get_event_loop()

        # Yield events from the JSONL file. Run the blocking iterator in a thread
        # so we don't block the event loop.
        def blocking_iter(start_offset: int):
            yield from events.tail_events(wf_id, from_offset=start_offset)

        offset = since
        # We can't `async for` over a sync generator across threads cleanly, so
        # poll instead — every 250ms read everything new.
        path = events.log_path(wf_id)
        last_heartbeat = asyncio.get_event_loop().time()
        while True:
            if await request.is_disconnected():
                break
            new_events: list[tuple[int, dict]] = []
            if path.exists():
                try:
                    def _read():
                        out = []
                        nonlocal_offset = offset
                        with path.open("r", encoding="utf-8") as f:
                            f.seek(nonlocal_offset)
                            chunk = f.read()
                        if chunk:
                            consumed = 0
                            for raw in chunk.splitlines(keepends=True):
                                line = raw.strip()
                                consumed += len(raw.encode("utf-8"))
                                if not line:
                                    continue
                                try:
                                    out.append((nonlocal_offset + consumed, json.loads(line)))
                                except Exception:
                                    continue
                        return out

                    new_events = await loop.run_in_executor(None, _read)
                except Exception:
                    new_events = []

            for new_offset, evt in new_events:
                offset = new_offset
                evt["offset"] = offset
                yield f"data: {json.dumps(evt)}\n\n"

            now = asyncio.get_event_loop().time()
            if now - last_heartbeat > 8:
                yield f": heartbeat\n\n"
                last_heartbeat = now

            await asyncio.sleep(0.1)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/task-queue")
async def task_queue_endpoint() -> JSONResponse:
    """Reports whether a worker is currently polling the analytics-demo task
    queue. Used by the UI to distinguish 'long LLM call' from 'worker is gone'.
    Polls Temporal directly — this is the authoritative signal."""
    import time as _time
    try:
        client = await asyncio.wait_for(Client.connect(TEMPORAL_TARGET), timeout=2.0)
    except Exception as e:
        return JSONResponse({"reachable": False, "error": str(e), "workflow_pollers": 0, "activity_pollers": 0})

    from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
    from temporalio.api.taskqueue.v1 import TaskQueue
    from temporalio.api.enums.v1 import TaskQueueType

    async def _count(tq_type) -> tuple[int, float | None]:
        req = DescribeTaskQueueRequest(
            namespace="default",
            task_queue=TaskQueue(name="analytics-demo"),
            task_queue_type=tq_type,
        )
        try:
            resp = await client.workflow_service.describe_task_queue(req)
        except Exception:
            return 0, None
        pollers = resp.pollers
        if not pollers:
            return 0, None
        # Most recent poll across all pollers
        latest = max(p.last_access_time.seconds + p.last_access_time.nanos / 1e9 for p in pollers)
        return len(pollers), latest

    wf_count, wf_last = await _count(TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW)
    act_count, act_last = await _count(TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY)

    now = _time.time()
    last = max([t for t in (wf_last, act_last) if t is not None] or [0])
    seconds_since_last_poll = (now - last) if last else None

    # "alive" if there's at least one poller AND it polled recently. We use a
    # generous 45s threshold because heavy CPU work (qwen2.5:3b inference)
    # temporarily delays polling, which would otherwise cause false-positive
    # "worker disconnected" flips during normal LLM calls. The manual Stop
    # button locks the badge immediately, so demo kill-detection is instant
    # regardless of this threshold.
    alive = (wf_count + act_count) > 0 and (seconds_since_last_poll is not None and seconds_since_last_poll < 45)

    return JSONResponse({
        "reachable": True,
        "alive": alive,
        "workflow_pollers": wf_count,
        "activity_pollers": act_count,
        "seconds_since_last_poll": seconds_since_last_poll,
    })


@app.get("/api/memory")
async def memory_endpoint() -> JSONResponse:
    return JSONResponse({
        "patterns": mem.list_patterns(),
        "agent_md_path": str(mem.AGENT_MD_PATH),
        "agent_md_exists": mem.AGENT_MD_PATH.exists(),
        "agent_md_content": mem.AGENT_MD_PATH.read_text(encoding="utf-8") if mem.AGENT_MD_PATH.exists() else "",
    })


@app.post("/api/reset-memory")
async def reset_memory_endpoint() -> JSONResponse:
    mem.reset()
    return JSONResponse({"ok": True, "patterns": mem.list_patterns()})


@app.get("/api/usage")
async def usage_endpoint() -> JSONResponse:
    from src.llm import usage_snapshot
    return JSONResponse(usage_snapshot())


# Tracks active auto-stop watchers so a second arm replaces the first.
_auto_stop_tasks: dict[str, asyncio.Task] = {}


@app.post("/api/worker/auto-stop")
async def worker_auto_stop_endpoint(req: Request) -> JSONResponse:
    """Arm an auto-stop: when the named skill starts on the given workflow,
    kill the worker. Used so the demo can show 'click Send → workflow runs →
    worker dies right at weather_fetch → click Start to resume'."""
    body = await req.json()
    wf_id = body.get("workflow_id")
    skill = body.get("skill", "weather_fetch")
    if not wf_id:
        return JSONResponse({"ok": False, "error": "workflow_id required"}, status_code=400)

    # Cancel any prior watcher for this wf
    prior = _auto_stop_tasks.pop(wf_id, None)
    if prior and not prior.done():
        prior.cancel()

    async def _watch():
        log = events.log_path(wf_id)
        offset = 0
        deadline = asyncio.get_event_loop().time() + 600  # 10 min cap
        target = (skill or "").strip()
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
            if not log.exists():
                continue
            try:
                with log.open("r", encoding="utf-8") as f:
                    f.seek(offset)
                    chunk = f.read()
                    offset = f.tell()
            except Exception:
                continue
            for raw in chunk.splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    evt = json.loads(raw)
                except Exception:
                    continue
                if evt.get("kind") == "skill.start" and evt.get("msg") == target:
                    # Found it — stop the worker
                    try:
                        await worker_stop_endpoint()
                    except Exception:
                        pass
                    return

    task = asyncio.create_task(_watch())
    _auto_stop_tasks[wf_id] = task
    return JSONResponse({"ok": True, "armed": True, "wf": wf_id, "skill": skill})


@app.post("/api/worker/stop")
async def worker_stop_endpoint() -> JSONResponse:
    """Gracefully stop the in-process Temporal Worker. Sub-second."""
    try:
        result = await worker_manager.stop()
        return JSONResponse({"ok": True, **result})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/worker/start")
async def worker_start_endpoint() -> JSONResponse:
    """Start the in-process Temporal Worker. No subprocess spawn — sub-second."""
    try:
        result = await worker_manager.start()
        return JSONResponse({"ok": True, **result})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/skills")
async def skills_endpoint() -> JSONResponse:
    """Live list of skills discovered in src/skills/. Updates immediately
    when a new file is dropped in (the planner's view) — but the worker
    still needs Reload Skills to register new activities."""
    from src.skills import skill_meta
    return JSONResponse({"skills": skill_meta()})


@app.post("/api/reload-skills")
async def reload_skills_endpoint() -> JSONResponse:
    """Sub-second reload: stop the in-process worker, start it again. The
    new worker calls refresh_skill_activities() and picks up any new files."""
    try:
        await worker_manager.stop()
        await worker_manager.start()
        from src.skills import skill_meta
        return JSONResponse({"ok": True, "skills": skill_meta()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/approve/{wf_id}")
async def approve_endpoint(wf_id: str, req: Request) -> JSONResponse:
    """Send the approve_step signal for a HITL-gated skill."""
    body = await req.json()
    skill = body.get("skill") or ""
    if not skill:
        return JSONResponse({"ok": False, "error": "skill required"}, status_code=400)
    client = await Client.connect(TEMPORAL_TARGET)
    handle = client.get_workflow_handle(wf_id)
    await handle.signal("approve_step", skill)
    return JSONResponse({"ok": True, "wf": wf_id, "skill": skill, "decision": "approve"})


@app.post("/api/deny/{wf_id}")
async def deny_endpoint(wf_id: str, req: Request) -> JSONResponse:
    body = await req.json()
    skill = body.get("skill") or ""
    if not skill:
        return JSONResponse({"ok": False, "error": "skill required"}, status_code=400)
    client = await Client.connect(TEMPORAL_TARGET)
    handle = client.get_workflow_handle(wf_id)
    await handle.signal("deny_step", skill)
    return JSONResponse({"ok": True, "wf": wf_id, "skill": skill, "decision": "deny"})


@app.get("/api/provenance/{wf_id}")
async def provenance_endpoint(wf_id: str) -> JSONResponse:
    """Per-output provenance for a finished workflow (skill that produced it,
    timestamp, elapsed, agent attempts, etc.)."""
    state = chat_session.load_state(wf_id)
    if not state:
        return JSONResponse({"per_key": {}, "note": "no cached state for this workflow"})
    inner = state.get("state", {}) or {}
    per_key = inner.get("_provenance") or {}
    return JSONResponse({
        "per_key": per_key,
        "failures": state.get("failures", []),
        "replan_count": state.get("replan_count", 0),
        "completed_steps": state.get("completed_steps", []),
        "plan": state.get("plan", []),
    })


@app.on_event("startup")
async def _autostart_worker():
    """On uvicorn startup, automatically launch the worker so the user doesn't
    have to click Start before the first question."""
    try:
        await worker_manager.start()
    except Exception as e:
        # Non-fatal: user can click Start later if Temporal isn't up yet.
        import logging
        logging.getLogger("uvicorn").warning(f"auto-start worker failed: {e}")


@app.on_event("shutdown")
async def _shutdown_worker():
    try:
        await worker_manager.stop()
    except Exception:
        pass
