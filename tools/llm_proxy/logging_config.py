"""JSON-structured logging for the receptionist app.

Replaces the default text formatter so Fly's log stream emits one JSON
object per line. Makes call_sid greppable as a real field downstream
(any future log aggregator, plain `jq`, etc.). The existing logs all
include `call_sid=<sid>` inside the message string, which is preserved
intact in the `msg` field and also extracted as a top-level `call_sid`
field when present.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

_CALL_SID_RE = re.compile(r"call_sid=([A-Za-z0-9_]+)")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": msg,
        }
        # Extract call_sid from the message body so it's queryable as a field
        # even though the existing log call sites embed it in the string.
        match = _CALL_SID_RE.search(msg)
        if match:
            payload["call_sid"] = match.group(1)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging() -> None:
    """Configure the root logger to emit JSON to stdout.

    Honors LOG_LEVEL (default INFO) and LOG_FORMAT (json|text, default json).
    `text` falls back to the legacy human-readable format for local dev.
    """
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    fmt = os.environ.get("LOG_FORMAT", "json").lower()

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "text":
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    else:
        handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    # Replace any handlers basicConfig may have installed.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
