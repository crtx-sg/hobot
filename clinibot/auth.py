"""API key authentication middleware."""

import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("clinibot.auth")

_EXEMPT_PATHS = {"/health", "/docs", "/openapi.json", "/metrics"}

# Keys loaded from env: API_KEYS=tg-bot:secretA,webchat:secretB
_KEYS: dict[str, str] = {}  # key -> client_id


def load_api_keys() -> None:
    """Parse API_KEYS env var into lookup dict."""
    global _KEYS
    raw = os.environ.get("API_KEYS", "")
    if not raw:
        logger.warning("API_KEYS not set — auth middleware disabled")
        return
    _KEYS.clear()
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" not in pair:
            continue
        client_id, key = pair.split(":", 1)
        _KEYS[key] = client_id
    logger.info("Loaded %d API keys", len(_KEYS))


class AuthMiddleware(BaseHTTPMiddleware):
    """Reject requests without a valid Bearer token."""

    async def dispatch(self, request: Request, call_next):
        if not _KEYS:
            # Auth disabled (no keys configured)
            request.state.client_id = "anonymous"
            return await call_next(request)

        if request.url.path in _EXEMPT_PATHS or request.url.path.startswith("/metrics"):
            request.state.client_id = "anonymous"
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse(status_code=401, content={"error": "Missing or invalid Authorization header"})

        token = auth[7:]
        client_id = _KEYS.get(token)
        if client_id is None:
            return JSONResponse(status_code=401, content={"error": "Invalid API key"})

        request.state.client_id = client_id
        return await call_next(request)
