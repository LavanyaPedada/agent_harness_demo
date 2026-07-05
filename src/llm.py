"""LLM wrapper. Uses Groq (gpt-oss-120b) when GROQ_API_KEY is set;
falls back to local Ollama otherwise. Tracks per-call token usage so the UI
can show a live counter.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("DEMO_MODEL", "qwen2.5:3b")
DEFAULT_MAX_TOKENS = int(os.environ.get("DEMO_MAX_TOKENS", "256"))

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

USE_GROQ = bool(GROQ_API_KEY)

_usage_lock = threading.Lock()
_usage = {
    "provider": "groq" if USE_GROQ else "ollama",
    "model": GROQ_MODEL if USE_GROQ else OLLAMA_MODEL,
    "calls": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "total_latency_ms": 0,
    "last_call": [],  # rolling list of last 10 calls
}


def usage_snapshot() -> dict:
    with _usage_lock:
        return {
            "provider": _usage["provider"],
            "model": _usage["model"],
            "calls": _usage["calls"],
            "prompt_tokens": _usage["prompt_tokens"],
            "completion_tokens": _usage["completion_tokens"],
            "total_tokens": _usage["total_tokens"],
            "total_latency_ms": _usage["total_latency_ms"],
            "last_call": list(_usage["last_call"]),
        }


def _record(prompt_tokens: int, completion_tokens: int, latency_ms: int):
    with _usage_lock:
        _usage["calls"] += 1
        _usage["prompt_tokens"] += prompt_tokens
        _usage["completion_tokens"] += completion_tokens
        _usage["total_tokens"] += prompt_tokens + completion_tokens
        _usage["total_latency_ms"] += latency_ms
        _usage["last_call"].insert(0, {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "latency_ms": latency_ms,
            "ts": time.time(),
        })
        del _usage["last_call"][10:]


_groq_client = None


def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        _groq_client = Groq(api_key=GROQ_API_KEY)
    return _groq_client


def _chat_groq(system: str, user: str, temperature: float, max_tokens: int) -> str:
    client = _get_groq_client()
    t0 = time.time()
    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    latency_ms = int((time.time() - t0) * 1000)
    usage = getattr(resp, "usage", None)
    pt = getattr(usage, "prompt_tokens", 0) if usage else 0
    ct = getattr(usage, "completion_tokens", 0) if usage else 0
    _record(pt, ct, latency_ms)
    return resp.choices[0].message.content or ""


def _chat_ollama(system: str, user: str, temperature: float, num_predict: int) -> str:
    from langchain_ollama import ChatOllama
    llm = ChatOllama(
        model=OLLAMA_MODEL,
        base_url=OLLAMA_HOST,
        temperature=temperature,
        num_ctx=4096,
        num_predict=num_predict,
    )
    t0 = time.time()
    messages: list[BaseMessage] = [SystemMessage(content=system), HumanMessage(content=user)]
    resp = llm.invoke(messages)
    latency_ms = int((time.time() - t0) * 1000)
    # Ollama via langchain doesn't always expose token counts cleanly; record 0/0.
    _record(0, 0, latency_ms)
    return resp.content if isinstance(resp.content, str) else str(resp.content)


def chat(system: str, user: str, temperature: float = 0.1, num_predict: int | None = None) -> str:
    max_tokens = num_predict if num_predict is not None else DEFAULT_MAX_TOKENS
    if USE_GROQ:
        return _chat_groq(system, user, temperature, max_tokens)
    return _chat_ollama(system, user, temperature, max_tokens)


def chat_messages(messages: list[BaseMessage], temperature: float = 0.1, num_predict: int | None = None) -> str:
    """Used by compaction. Convert to system/user split for the simple wrappers."""
    system_parts: list[str] = []
    user_parts: list[str] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            system_parts.append(str(m.content))
        else:
            user_parts.append(str(m.content))
    return chat("\n\n".join(system_parts), "\n\n".join(user_parts), temperature=temperature, num_predict=num_predict)
