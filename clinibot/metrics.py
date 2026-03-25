"""Prometheus metrics — graceful no-op when prometheus_client not installed."""

try:
    from prometheus_client import Counter, Gauge, Histogram

    REQUESTS = Counter("hobot_requests_total", "Total requests", ["endpoint", "status"])
    TOOL_CALLS = Counter("hobot_tool_calls_total", "Tool calls", ["tool_name", "status"])
    LLM_CALLS = Counter("hobot_llm_calls_total", "LLM calls", ["provider", "status"])
    REQUEST_DURATION = Histogram("hobot_request_duration_seconds", "Request latency", ["endpoint"])
    TOOL_DURATION = Histogram("hobot_tool_call_duration_seconds", "Tool latency", ["tool_name"])
    LLM_DURATION = Histogram("hobot_llm_call_duration_seconds", "LLM latency", ["provider"])
    ACTIVE_SESSIONS = Gauge("hobot_active_sessions", "Active sessions")
    enabled = True
except ImportError:
    enabled = False

    class _Noop:
        """No-op stub for all metric operations."""
        def labels(self, *a, **kw):
            return self
        def inc(self, *a, **kw):
            pass
        def dec(self, *a, **kw):
            pass
        def observe(self, *a, **kw):
            pass
        def set(self, *a, **kw):
            pass

    _noop = _Noop()
    REQUESTS = _noop
    TOOL_CALLS = _noop
    LLM_CALLS = _noop
    REQUEST_DURATION = _noop
    TOOL_DURATION = _noop
    LLM_DURATION = _noop
    ACTIVE_SESSIONS = _noop
