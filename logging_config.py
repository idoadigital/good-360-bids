"""Optional JSON-structured logging.

Use from any script:

    from logging_config import setup_logging
    log = setup_logging("monitor")
    log.info("scan complete", extra={"trucks": 3, "elapsed_ms": 412})

Output is one JSON object per line — trivially shippable to any log service
(Cloudwatch, Datadog, Loki) without further parsing.

Falls back to plain text when LOG_FORMAT=text (default) to avoid churning
existing log consumers until they're ready to migrate.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

UTC = timezone.utc


class JsonFormatter(logging.Formatter):
    RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Any extra={} fields the caller passed.
        for k, v in record.__dict__.items():
            if k not in self.RESERVED and not k.startswith("_"):
                payload[k] = v
        return json.dumps(payload, default=str)


def setup_logging(name: str = "good360") -> logging.Logger:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    fmt = os.environ.get("LOG_FORMAT", "text").lower()

    root = logging.getLogger()
    # idempotent — remove handlers from prior calls so tests/CLI re-runs are clean
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        ))
    root.addHandler(handler)
    root.setLevel(level)
    return logging.getLogger(name)
