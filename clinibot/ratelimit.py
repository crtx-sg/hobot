"""Sliding-window rate limiter middleware for FastAPI."""

import json
import time
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class SlidingWindowLimiter:
    """In-memory sliding window rate limiter."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str) -> tuple[bool, float]:
        """Check if request is allowed. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        # Prune old entries
        timestamps = self._hits[key]
        self._hits[key] = timestamps = [t for t in timestamps if t > cutoff]
        if len(timestamps) >= self.max_requests:
            retry_after = timestamps[0] - cutoff
            return False, max(retry_after, 0.1)
        timestamps.append(now)
        return True, 0.0


_RATE_LIMITED_PATHS = {"/chat", "/chat/stream"}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-user + per-client rate limiting on chat endpoints.

    Uses authenticated client_id (from AuthMiddleware) as primary key.
    Falls back to body user_id for per-user granularity within a shared client.
    """

    def __init__(self, app, user_limiter: SlidingWindowLimiter, tenant_limiter: SlidingWindowLimiter):
        super().__init__(app)
        self.user_limiter = user_limiter
        self.tenant_limiter = tenant_limiter

    async def dispatch(self, request: Request, call_next):
        if request.url.path not in _RATE_LIMITED_PATHS or request.method != "POST":
            return await call_next(request)

        # Primary key: authenticated client identity (non-bypassable)
        client_id = getattr(request.state, "client_id", "") or (request.client.host if request.client else "unknown")

        # Secondary key: body user_id for per-user granularity within a shared client
        user_id = ""
        try:
            body_bytes = await request.body()
            body = json.loads(body_bytes)
            user_id = body.get("user_id", "")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

        # Per-user limit: keyed on client_id + user_id so distinct users behind
        # the same API key (e.g. tg-bot) get independent rate limits
        user_key = f"user:{client_id}:{user_id}" if user_id else f"user:{client_id}"
        allowed, retry_after = self.user_limiter.allow(user_key)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded (per-user)"},
                headers={"Retry-After": str(int(retry_after) + 1)},
            )

        # Per-client limit: aggregate cap across all users of this client
        allowed, retry_after = self.tenant_limiter.allow(f"client:{client_id}")
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded (per-client)"},
                headers={"Retry-After": str(int(retry_after) + 1)},
            )

        return await call_next(request)
