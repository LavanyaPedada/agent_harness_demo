"""In-process worker manager.

Start = create a Temporal Worker instance and run it as an asyncio task.
Stop  = call worker.shutdown() (graceful; in-flight activity gets a clean
        cancellation, Temporal reschedules it for whoever polls next).

Compared to subprocess kill/spawn this saves ~5-10s of Python import time
on every Start click — Start is now sub-second.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from src.workflow import RevenueAnalysisWorkflow
from src.activities import (
    plan_activity,
    replan_activity,
    run_skill_activity,
    SKILL_ACTIVITIES,
    refresh_skill_activities,
)
from src import events

TASK_QUEUE = "analytics-demo"

log = logging.getLogger("worker_mgr")


class WorkerManager:
    def __init__(self, target: str = "localhost:7233"):
        self.target = target
        self._worker: Worker | None = None
        self._task: asyncio.Task | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._client: Client | None = None  # cached across Stop/Start cycles
        self._lock = asyncio.Lock()

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _get_client(self) -> Client:
        if self._client is None:
            self._client = await Client.connect(self.target)
        return self._client

    async def prewarm(self) -> None:
        """Establish the Temporal Client connection at server startup so the
        first Start click doesn't pay the connect cost."""
        await self._get_client()

    async def start(self) -> dict:
        async with self._lock:
            if self.is_running():
                return {"status": "already running"}
            client = await self._get_client()
            self._executor = ThreadPoolExecutor(max_workers=4)

            # Re-scan src/skills/ before each (re)start so newly-dropped skill
            # files are picked up. This is the "hot-add skills" demo moment.
            registered = refresh_skill_activities()
            try:
                events.emit(
                    "skills.registered",
                    f"{len(registered)} skill(s) registered with worker",
                    {"skills": registered},
                    src="worker_mgr.py:start",
                )
            except Exception:
                pass

            self._worker = Worker(
                client,
                task_queue=TASK_QUEUE,
                workflows=[RevenueAnalysisWorkflow],
                activities=[
                    plan_activity,
                    replan_activity,
                    run_skill_activity,
                    *SKILL_ACTIVITIES.values(),
                ],
                activity_executor=self._executor,
            )

            async def _run():
                try:
                    await self._worker.run()
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    log.exception(f"worker died: {e}")

            self._task = asyncio.create_task(_run())
            return {"status": "started"}

    async def stop(self) -> dict:
        async with self._lock:
            if not self.is_running():
                return {"status": "not running"}
            try:
                if self._worker is not None:
                    await self._worker.shutdown()
            except Exception as e:
                log.warning(f"worker.shutdown raised: {e}")
            if self._task is not None:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            if self._executor is not None:
                self._executor.shutdown(wait=False)
            self._worker = None
            self._task = None
            self._executor = None
            return {"status": "stopped"}


# Module-level singleton — server.py uses this directly.
manager = WorkerManager()
