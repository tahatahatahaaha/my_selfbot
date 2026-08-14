"""
AI layer for the `.ai` command only — plain conversational chat via
OpenRouter. Configurable API key / model / base URL / timeout, so
switching models is a config change, never a code change.

Agent Mode (natural-language -> command routing) has been removed
entirely; this file no longer does any JSON classification.
"""

import asyncio
import time

import httpx

from config import (
    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
    OPENROUTER_BASE_URL,
    OPENROUTER_TIMEOUT,
    OPENROUTER_FALLBACK_MODELS,
    OPENROUTER_MAX_CALLS_PER_MINUTE,
)
from logger import log

MAX_RETRIES = 2
RETRY_BASE_DELAY = 1.0


class _RateLimiter:
    """Simple client-side sliding-window limiter — protects the account's
    own OpenRouter quota/budget from a runaway loop. Independent of
    OpenRouter's own 429 handling, which _call_one_model already retries
    with backoff; this caps calls BEFORE they're sent at all. 0 = unlimited."""

    def __init__(self, max_per_minute: int):
        self.max_per_minute = max_per_minute
        self._timestamps = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        if self.max_per_minute <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            self._timestamps = [t for t in self._timestamps if now - t < 60]
            if len(self._timestamps) >= self.max_per_minute:
                wait = 60 - (now - self._timestamps[0])
                if wait > 0:
                    log.warn(f"ai_agent: client-side rate limit hit, waiting {wait:.1f}s")
                    await asyncio.sleep(wait)
                now = time.monotonic()
                self._timestamps = [t for t in self._timestamps if now - t < 60]
            self._timestamps.append(now)


_rate_limiter = _RateLimiter(OPENROUTER_MAX_CALLS_PER_MINUTE)

# Model switching: mutable at runtime via `.مدل`, starts at the configured default.
_active_model = OPENROUTER_MODEL


def get_model() -> str:
    return _active_model


def set_model(model: str):
    global _active_model
    _active_model = model.strip()
    log.ok(f"ai_agent: active model switched to {_active_model}")


def _candidate_models() -> list:
    """Primary model first, then configured fallbacks — a request only
    reaches model N+1 if model N exhausted its own retries."""
    return [_active_model] + [m for m in OPENROUTER_FALLBACK_MODELS if m != _active_model]


async def _call_one_model(model: str, messages, temperature: float, http: httpx.AsyncClient):
    """Runs MAX_RETRIES attempts against a single model. Raises on total
    failure so the caller (_call) can move on to the next fallback model."""
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = await http.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"model": model, "messages": messages, "temperature": temperature},
            )
            resp.raise_for_status()
            body = resp.json()
            return body["choices"][0]["message"]["content"].strip()
        except httpx.HTTPStatusError as e:
            last_exc = e
            if e.response.status_code in (429, 502, 503) and attempt < MAX_RETRIES - 1:
                log.warn(f"ai_agent: {model} -> {e.response.status_code}, retrying...")
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                continue
            break
        except httpx.TransportError as e:
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                continue
            break
    raise last_exc


async def _call(messages, temperature=0.3):
    """Tries the active model, then each configured fallback in order, each
    with its own retry+backoff. Only raises once every candidate model has
    been exhausted."""
    await _rate_limiter.acquire()
    async with httpx.AsyncClient(timeout=OPENROUTER_TIMEOUT) as http:
        last_exc = None
        for model in _candidate_models():
            try:
                return await _call_one_model(model, messages, temperature, http)
            except Exception as e:
                last_exc = e
                log.warn(f"ai_agent: model {model} exhausted retries ({e}), trying next fallback if any")
                continue
        raise last_exc


async def health_check() -> dict:
    """Cheap connectivity probe — one tiny request. Never raises; returns
    ok=False with the error instead, so a bad key/network never crashes."""
    if not OPENROUTER_API_KEY:
        return {"ok": False, "model": _active_model, "error": "OPENROUTER_API_KEY not set"}
    start = time.monotonic()
    try:
        await _call([{"role": "user", "content": "ping"}], temperature=0)
        return {"ok": True, "model": _active_model, "latency_ms": round((time.monotonic() - start) * 1000, 1)}
    except Exception as e:
        return {"ok": False, "model": _active_model, "error": str(e)}


def format_error(e: Exception) -> str:
    """User-facing error text. For a 429, OpenRouter's response body usually
    says whether it's the per-minute or daily free-tier cap and sometimes
    when it resets — surface that instead of just 'Too Many Requests'."""
    if isinstance(e, httpx.HTTPStatusError):
        if e.response.status_code == 429:
            try:
                detail = e.response.json().get("error", {}).get("message", "")
            except Exception:
                detail = e.response.text[:200]
            return (
                "به سقف رایگان OpenRouter خوردیم (429). "
                + (detail or "جزئیات بیشتری تو پاسخ نبود.")
                + "\n\nمعمولاً یا باید چند دقیقه/تا فردا صبر کنی، یا با شارژ حداقلی "
                "($۱۰) تو openrouter.ai سقف روزانه از ۵۰ به ۱۰۰۰ میره."
            )
        return f"OpenRouter خطای {e.response.status_code} داد: {e.response.text[:200]}"
    return str(e)


async def chat(question: str) -> str:
    """Plain answer for the `.ai <question>` command."""
    return await _call([{"role": "user", "content": question}])
