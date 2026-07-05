"""Temporal worker. Run this in a separate terminal:

    D:\\analytics-demo\\dev_env\\Scripts\\python.exe D:\\analytics-demo\\worker.py

Connects to localhost:7233 (the dev server) and registers the workflow +
activities. Kill it with Ctrl+C; restart it to demo durable resume.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

# Make `src` importable
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.workflow import RevenueAnalysisWorkflow
from src.activities import (
    plan_activity,
    replan_activity,
    run_skill_activity,
    SKILL_ACTIVITIES,
    refresh_skill_activities,
)

TASK_QUEUE = "analytics-demo"
TEMPORAL_TARGET = os.environ.get("TEMPORAL_TARGET", "localhost:7233")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [worker] %(message)s")
    logging.info(f"Connecting to Temporal at {TEMPORAL_TARGET} ...")
    client = await Client.connect(TEMPORAL_TARGET)
    logging.info(f"Connected. Starting worker on task queue '{TASK_QUEUE}' ...")

    # Pick up any skill files added since this process started.
    registered = refresh_skill_activities()
    logging.info(f"Skills registered: {registered}")

    # Activities run in a thread pool — they're sync (LangGraph is sync) and may
    # block on subprocess + LLM calls.
    activity_executor = ThreadPoolExecutor(max_workers=4)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[RevenueAnalysisWorkflow],
        activities=[
            plan_activity,
            replan_activity,
            run_skill_activity,
            *SKILL_ACTIVITIES.values(),
        ],
        activity_executor=activity_executor,
    )

    logging.info("Worker started. Ctrl+C to stop (workflow will resume on restart).")
    await worker.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[worker] killed by user. Workflow state is preserved in Temporal — restart me to resume.")
