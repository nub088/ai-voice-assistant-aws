"""Weekly call-intake digest.

Reads booking-intake rows from the Google Sheet, computes a rolling-window
summary (default: last 7 days), renders human-readable text + a single
"WeeklyDigest" tab row. The CLI lives in `digest_cli.py`; everything in this
module is pure so it can be exercised in tests without Sheets credentials.

Why not reuse `analyze_calls.py`? That tool reads the historical AllGreen
JSON transcript archive (one-shot retrospective). The digest reads the live
Sheet that the receptionist writes into, so the two have different inputs
and different output shapes.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .sheets_writer import HEADER_ROW

# Columns we surface in the WeeklyDigest sheet tab. Keep this aligned with
# `render_digest_row`; tests pin the length.
DIGEST_HEADER_ROW: list[str] = [
    "period_start",
    "period_end",
    "total_calls",
    "routine",
    "soon",
    "emergency",
    "top_pest",
    "out_of_scope",
    "needs_followup",
    "low_confidence",
    "unique_callers",
]

DIGEST_TAB_NAME = "WeeklyDigest"

# Cap how many emergency rows we list out by name in the rendered text.
_MAX_EMERGENCY_LINES = 10
# Top-N pests to surface in the rendered text.
_TOP_PEST_LIMIT = 5


@dataclass
class CallRow:
    timestamp: datetime
    call_id: str
    caller_name: str
    callback_last4: str
    service_address: str
    identified_pest: str
    urgency: str
    out_of_scope: bool
    needs_followup_notes: str
    ai_confidence: str


@dataclass
class Digest:
    period_start: datetime
    period_end: datetime
    total_calls: int
    by_urgency: dict[str, int]
    by_pest: list[tuple[str, int]]
    out_of_scope: int
    needs_followup: int
    low_confidence: int
    unique_callers: int
    emergencies: list[str] = field(default_factory=list)


def _parse_timestamp(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _truthy(s: Any) -> bool:
    if isinstance(s, bool):
        return s
    return str(s).strip().lower() in {"true", "yes", "1"}


def parse_rows(values: list[list[Any]]) -> list[CallRow]:
    """Parse the Sheets API response (header row + data rows) into CallRow.

    Tolerates ragged rows (short trailing cells) and skips rows whose
    timestamp can't be parsed, a row without a usable timestamp can't be
    placed in the digest window anyway.
    """
    if not values or len(values) < 2:
        return []
    header = [str(c) for c in values[0]]
    idx = {col: i for i, col in enumerate(header)}

    def get(raw: list[Any], col: str) -> str:
        i = idx.get(col)
        if i is None or i >= len(raw):
            return ""
        v = raw[i]
        return "" if v is None else str(v)

    out: list[CallRow] = []
    for raw in values[1:]:
        ts = _parse_timestamp(get(raw, "timestamp"))
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append(
            CallRow(
                timestamp=ts,
                call_id=get(raw, "vapi_call_id"),
                caller_name=get(raw, "caller_name"),
                callback_last4=get(raw, "callback_number_last4"),
                service_address=get(raw, "service_address"),
                identified_pest=get(raw, "identified_pest"),
                urgency=get(raw, "urgency").lower(),
                out_of_scope=_truthy(get(raw, "out_of_scope")),
                needs_followup_notes=get(raw, "needs_followup_notes"),
                ai_confidence=get(raw, "ai_confidence").lower(),
            )
        )
    return out


def compute_digest(rows: list[CallRow], *, now: datetime, days: int = 7) -> Digest:
    """Build a digest summarizing the last `days` of intake rows."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    period_end = now
    period_start = now - timedelta(days=days)

    in_window = [r for r in rows if period_start <= r.timestamp <= period_end]

    urgency_counts = {"routine": 0, "soon": 0, "emergency": 0}
    pest_counter: Counter[str] = Counter()
    out_of_scope = 0
    needs_followup = 0
    low_confidence = 0
    callers: set[tuple[str, str]] = set()
    emergencies: list[str] = []

    for r in in_window:
        u = r.urgency or "routine"
        if u in urgency_counts:
            urgency_counts[u] += 1
        if r.identified_pest.strip():
            pest_counter[r.identified_pest.strip().lower()] += 1
        if r.out_of_scope:
            out_of_scope += 1
        if r.needs_followup_notes.strip():
            needs_followup += 1
        if r.ai_confidence.strip().lower() == "low":
            low_confidence += 1
        callers.add((r.caller_name.strip().lower(), r.callback_last4.strip()))
        if u == "emergency":
            emergencies.append(_format_emergency(r))

    return Digest(
        period_start=period_start,
        period_end=period_end,
        total_calls=len(in_window),
        by_urgency=urgency_counts,
        by_pest=pest_counter.most_common(_TOP_PEST_LIMIT),
        out_of_scope=out_of_scope,
        needs_followup=needs_followup,
        low_confidence=low_confidence,
        unique_callers=len(callers),
        emergencies=emergencies,
    )


def _format_emergency(r: CallRow) -> str:
    name = r.caller_name.strip() or "(unknown)"
    last4 = r.callback_last4.strip()
    pest = r.identified_pest.strip() or "(pest unspecified)"
    address = r.service_address.strip() or "(address unspecified)"
    last4_part = f" (***{last4})" if last4 else ""
    return f"{name}{last4_part}, {pest} at {address}"


def render_digest_text(digest: Digest) -> str:
    """Plain-text rendering, what gets printed to stdout / cron logs."""
    lines: list[str] = []
    fmt = "%Y-%m-%d"
    lines.append("Clear Slope, Weekly Digest")
    lines.append(
        f"{digest.period_start.strftime(fmt)} → {digest.period_end.strftime(fmt)}"
    )
    lines.append("")
    lines.append(f"Total calls: {digest.total_calls}")
    lines.append(f"  routine:   {digest.by_urgency.get('routine', 0)}")
    lines.append(f"  soon:      {digest.by_urgency.get('soon', 0)}")
    lines.append(f"  emergency: {digest.by_urgency.get('emergency', 0)}")
    lines.append("")
    lines.append(f"Unique callers:   {digest.unique_callers}")
    lines.append(f"Out of scope:     {digest.out_of_scope}")
    lines.append(f"Needs follow-up:  {digest.needs_followup}")
    lines.append(f"Low confidence:   {digest.low_confidence}")
    if digest.by_pest:
        lines.append("")
        lines.append("Top pests:")
        for pest, count in digest.by_pest:
            lines.append(f"  {pest:20} {count}")
    if digest.emergencies:
        lines.append("")
        lines.append("Emergencies:")
        for line in digest.emergencies[:_MAX_EMERGENCY_LINES]:
            lines.append(f"  • {line}")
        if len(digest.emergencies) > _MAX_EMERGENCY_LINES:
            extra = len(digest.emergencies) - _MAX_EMERGENCY_LINES
            lines.append(f"  ... +{extra} more")
    return "\n".join(lines)


def render_digest_row(digest: Digest) -> list[Any]:
    """One row to append to the WeeklyDigest sheet tab.

    Schema is `DIGEST_HEADER_ROW`. Any change here must keep the two in sync
    or the digest tab's columns drift out of order.
    """
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    top_pest = digest.by_pest[0][0] if digest.by_pest else ""
    return [
        digest.period_start.astimezone(timezone.utc).strftime(fmt),
        digest.period_end.astimezone(timezone.utc).strftime(fmt),
        digest.total_calls,
        digest.by_urgency.get("routine", 0),
        digest.by_urgency.get("soon", 0),
        digest.by_urgency.get("emergency", 0),
        top_pest,
        digest.out_of_scope,
        digest.needs_followup,
        digest.low_confidence,
        digest.unique_callers,
    ]


# Sanity: keep the row schema and header row aligned.
assert len(DIGEST_HEADER_ROW) == len(render_digest_row(
    Digest(
        period_start=datetime(1970, 1, 1, tzinfo=timezone.utc),
        period_end=datetime(1970, 1, 1, tzinfo=timezone.utc),
        total_calls=0,
        by_urgency={"routine": 0, "soon": 0, "emergency": 0},
        by_pest=[],
        out_of_scope=0,
        needs_followup=0,
        low_confidence=0,
        unique_callers=0,
    )
))


# Re-export so callers don't need to import from sheets_writer for the header.
__all__ = [
    "CallRow",
    "Digest",
    "DIGEST_HEADER_ROW",
    "DIGEST_TAB_NAME",
    "HEADER_ROW",
    "compute_digest",
    "parse_rows",
    "render_digest_row",
    "render_digest_text",
]
