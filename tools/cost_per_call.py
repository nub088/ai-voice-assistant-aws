"""Per-call (and per-period) operating-cost analyzer for the voice receptionist.

Reports the all-in cost of a call or a date range of calls, with an honest
provenance marker on every provider line so an estimate is never mistaken for
a measurement:

    ✅ measured   billed / metered number (Twilio call.price, Twilio SMS price,
                  or, when a call_usage row exists, Anthropic tokens / Cartesia
                  characters priced at the per-token / per-char rate)
    〜 metered     duration-proxied, or metered-but-attributed (Media Streams,
                  Deepgram STT, Twilio SMS attribution by number + time)
    ⚙ modeled     pure duration × per-minute estimate (Anthropic / Cartesia
                  before a call_usage row exists)

Data sources, in order of preference:
  - duration / client / time   : the transcripts table (local, no API call)
  - measured LLM/TTS usage      : the call_usage table, if present (Phase 2)
  - measured Twilio voice price : Twilio REST call.price (skipped with --no-twilio)
  - Twilio SMS                  : Twilio REST messages.list (single --call-sid mode)

Rates live in config/cost_rates.json (hand-maintained, with an effective_date).
This tool is READ-ONLY: it opens its own connection to the transcripts DB and
never writes. With --no-twilio it makes no network calls and costs $0.

Usage:
    python tools/cost_per_call.py --call-sid CA123...
    python tools/cost_per_call.py --since 2026-06-01 --until 2026-06-03 [--client-id clearslope]
    python tools/cost_per_call.py --call-sid CA123 --no-twilio --json

Reads TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN from .env (only when Twilio is used).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_DB_PATH = "/data/transcripts.db"
DEFAULT_RATES_PATH = "config/cost_rates.json"

MEASURED = "✅"
METERED = "〜"
MODELED = "⚙"


# --------------------------------------------------------------------------- #
# Data shapes
# --------------------------------------------------------------------------- #
@dataclass
class CallRow:
    call_sid: str
    client_id: str
    created_at: str  # ISO-8601 ...Z, the end-of-call write time
    duration_sec: float
    caller_number_last4: str | None


@dataclass
class Line:
    provider: str
    low: float
    high: float
    marker: str
    note: str


@dataclass
class CallCost:
    call_sid: str
    client_id: str
    created_at: str
    duration_sec: float
    lines: list[Line]

    @property
    def total_low(self) -> float:
        return sum(ln.low for ln in self.lines)

    @property
    def total_high(self) -> float:
        return sum(ln.high for ln in self.lines)


# --------------------------------------------------------------------------- #
# Rates
# --------------------------------------------------------------------------- #
def load_rates(path: str | Path) -> dict:
    """Load and minimally validate the rate file."""
    with open(path, encoding="utf-8") as fh:
        rates = json.load(fh)
    if "effective_date" not in rates:
        raise ValueError(f"rate file {path} is missing required 'effective_date'")
    if "per_minute" not in rates:
        raise ValueError(f"rate file {path} is missing required 'per_minute' block")
    return rates


def _band(entry: dict) -> tuple[float, float]:
    """A rate entry is either a point {'rate': x} or a band {'low': a, 'high': b}."""
    if "rate" in entry:
        return float(entry["rate"]), float(entry["rate"])
    return float(entry["low"]), float(entry["high"])


# --------------------------------------------------------------------------- #
# Reads (own read-only connection, never touches running-code modules)
# --------------------------------------------------------------------------- #
def read_calls(
    db_path: str,
    *,
    call_sid: str | None = None,
    since: str | None = None,
    until: str | None = None,
    client_id: str | None = None,
) -> list[CallRow]:
    """Read call rows from the transcripts table.

    `since`/`until` are YYYY-MM-DD dates compared lexicographically against the
    ISO-8601 ...Z `created_at` (works because the format is fixed-width and
    zero-padded). `until` is inclusive of the whole day.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conds: list[str] = []
        params: list[object] = []
        if call_sid:
            conds.append("call_sid = ?")
            params.append(call_sid)
        if client_id:
            conds.append("client_id = ?")
            params.append(client_id)
        if since:
            conds.append("created_at >= ?")
            params.append(f"{since}T00:00:00Z")
        if until:
            conds.append("created_at <= ?")
            params.append(f"{until}T23:59:59Z")
        q = (
            "SELECT call_sid, client_id, created_at, duration_sec, caller_number_last4 "
            "FROM transcripts"
        )
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY created_at"
        return [
            CallRow(
                call_sid=r["call_sid"],
                client_id=r["client_id"],
                created_at=r["created_at"],
                duration_sec=float(r["duration_sec"] or 0.0),
                caller_number_last4=r["caller_number_last4"],
            )
            for r in conn.execute(q, params).fetchall()
        ]
    finally:
        conn.close()


def read_usage(db_path: str, call_sids: list[str]) -> dict[str, dict]:
    """Read measured LLM/TTS usage from the call_usage table, keyed by call_sid.

    Returns {} if the table does not exist yet (Phase 1, before instrumentation
    ships), so the tool degrades gracefully to modeled Anthropic/Cartesia lines.
    """
    if not call_sids:
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='call_usage'"
        ).fetchone()
        if not exists:
            return {}
        placeholders = ",".join("?" for _ in call_sids)
        rows = conn.execute(
            "SELECT call_sid, llm_model, llm_prompt_tokens, llm_completion_tokens, "
            "llm_cache_read_tokens, llm_cache_creation_tokens, tts_model, tts_characters "
            f"FROM call_usage WHERE call_sid IN ({placeholders})",
            call_sids,
        ).fetchall()
        return {r["call_sid"]: dict(r) for r in rows}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Twilio (optional; injected client so it is unit-testable without a network)
# --------------------------------------------------------------------------- #
def fetch_voice_price(client, call_sid: str) -> float | None:
    """Fetch the measured Twilio Voice price for a call. None if unavailable.

    Twilio reports `price` as a negative string in the account currency, and it
    is None until the call finalizes. We return the absolute value.
    """
    try:
        d = client.calls(call_sid).fetch()
        price = getattr(d, "price", None)
        if price in (None, ""):
            return None
        return abs(float(price))
    except Exception:
        return None


def fetch_sms(
    client, call: CallRow, *, window_minutes: int = 10
) -> list[tuple[str, float]]:
    """Best-effort SMS attribution for a single call.

    Lists messages sent in [call_start, call_end + window) and keeps those whose
    recipient ends with the caller's last-4 (the post-call customer confirmation
    goes to the caller). Returns (message_sid, price) pairs. Attribution is
    inherently fuzzy, the caller is matched on last-4 + time, not a full number.
    """
    end = _parse_iso_z(call.created_at)
    if end is None:
        return []
    start = end - timedelta(seconds=call.duration_sec)
    after = start
    before = end + timedelta(minutes=window_minutes)
    out: list[tuple[str, float]] = []
    try:
        msgs = client.messages.list(date_sent_after=after, date_sent_before=before, limit=100)
    except Exception:
        return []
    for m in msgs:
        to = getattr(m, "to", "") or ""
        if call.caller_number_last4 and not to.endswith(call.caller_number_last4):
            continue
        price = getattr(m, "price", None)
        if price in (None, ""):
            continue
        out.append((getattr(m, "sid", "?"), abs(float(price))))
    return out


def _parse_iso_z(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------- #
# Cost model
# --------------------------------------------------------------------------- #
def _minutes_ceil(duration_sec: float) -> int:
    """Twilio bills voice/media per minute, rounded up."""
    return math.ceil(duration_sec / 60) if duration_sec > 0 else 0


def _minutes_frac(duration_sec: float) -> float:
    """Usage-based providers (STT/LLM/TTS), fractional minutes are closer."""
    return (duration_sec or 0.0) / 60.0


def _anthropic_measured(usage: dict, rates: dict) -> tuple[float, str]:
    """Measured Anthropic cost from the four per-token component counts.

    `total_tokens` is intentionally NOT used: Pipecat defines it as
    prompt+completion (it excludes cache tokens), so adding it to the
    cache-read/cache-write lines would double-count. Cost is built only from
    prompt, completion, cache-creation and cache-read at their per-Mtok rates.
    """
    pt = rates["per_token"]["anthropic_haiku_4_5"]
    cost = (
        usage.get("llm_prompt_tokens", 0) / 1e6 * pt["input_per_mtok"]
        + usage.get("llm_completion_tokens", 0) / 1e6 * pt["output_per_mtok"]
        + usage.get("llm_cache_creation_tokens", 0) / 1e6 * pt["cache_write_5m_per_mtok"]
        + usage.get("llm_cache_read_tokens", 0) / 1e6 * pt["cache_read_per_mtok"]
    )
    note = "measured tokens"
    if not pt.get("verified", False):
        note += " (rate est.)"
    return cost, note


def _cartesia_measured(usage: dict, rates: dict) -> tuple[float, str]:
    """Measured Cartesia cost = TTS characters spoken × per-character rate."""
    pc = rates["per_character"]["cartesia_sonic"]
    cost = usage.get("tts_characters", 0) * pc["rate_per_char"]
    note = "measured chars"
    if not pc.get("verified", False):
        note += " (rate est.)"
    return cost, note


def compute_call_cost(
    call: CallRow,
    rates: dict,
    *,
    usage: dict | None = None,
    voice_price: float | None = None,
    sms_items: list[tuple[str, float]] | None = None,
) -> CallCost:
    """Build the per-line cost breakdown for one call."""
    pm = rates["per_minute"]
    mins_ceil = _minutes_ceil(call.duration_sec)
    mins_frac = _minutes_frac(call.duration_sec)
    lines: list[Line] = []

    # Twilio Voice, measured price when available, else modeled (rounded-up min).
    if voice_price is not None:
        lines.append(Line("Twilio Voice", voice_price, voice_price, MEASURED, "call.price"))
    else:
        lo, hi = _band(pm["twilio_voice"])
        lines.append(
            Line("Twilio Voice", lo * mins_ceil, hi * mins_ceil, METERED, f"modeled {mins_ceil}min")
        )

    # Twilio Media Streams, modeled per-call (period cross-check via Usage Records is future).
    lo, hi = _band(pm["twilio_media_streams"])
    lines.append(Line("Twilio Media Streams", lo * mins_ceil, hi * mins_ceil, METERED, "modeled"))

    # Deepgram STT, duration-proxied.
    lo, hi = _band(pm["deepgram_stt"])
    lines.append(Line("Deepgram STT", lo * mins_frac, hi * mins_frac, METERED, "duration-proxied"))

    # Anthropic, measured tokens when a call_usage row exists, else modeled.
    if usage is not None:
        cost, note = _anthropic_measured(usage, rates)
        lines.append(Line("Anthropic Haiku", cost, cost, MEASURED, note))
    else:
        lo, hi = _band(pm["anthropic_haiku"])
        lines.append(Line("Anthropic Haiku", lo * mins_frac, hi * mins_frac, MODELED, "modeled"))

    # Cartesia TTS, measured chars when a call_usage row exists, else modeled.
    if usage is not None:
        cost, note = _cartesia_measured(usage, rates)
        lines.append(Line("Cartesia TTS", cost, cost, MEASURED, note))
    else:
        lo, hi = _band(pm["cartesia_tts"])
        lines.append(Line("Cartesia TTS", lo * mins_frac, hi * mins_frac, MODELED, "modeled"))

    # Twilio SMS, measured price, best-effort attribution (so marked 〜).
    if sms_items:
        total = sum(p for _, p in sms_items)
        sids = ", ".join(s for s, _ in sms_items)
        lines.append(
            Line("Twilio SMS", total, total, METERED, f"{len(sms_items)} msg [{sids}] attribution best-effort")
        )

    return CallCost(
        call_sid=call.call_sid,
        client_id=call.client_id,
        created_at=call.created_at,
        duration_sec=call.duration_sec,
        lines=lines,
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _fmt_money(low: float, high: float) -> str:
    if abs(high - low) < 1e-9:
        return f"${low:.4f}"
    return f"${low:.4f}–${high:.4f}"


def render_text(costs: list[CallCost], rates: dict) -> str:
    out: list[str] = []
    out.append(f"Rates effective {rates.get('effective_date', '?')} ({rates.get('currency', 'USD')})")
    out.append(f"Legend: {MEASURED} measured · {METERED} metered/proxied · {MODELED} modeled")
    out.append("")
    grand_low = grand_high = 0.0
    for c in costs:
        out.append(
            f"── call {c.call_sid}  client={c.client_id}  {c.created_at}  "
            f"dur={c.duration_sec:.1f}s ──"
        )
        for ln in c.lines:
            out.append(f"  {ln.marker} {ln.provider:<22s} {_fmt_money(ln.low, ln.high):>20s}  {ln.note}")
        out.append(f"  {'=':<2s} {'TOTAL':<22s} {_fmt_money(c.total_low, c.total_high):>20s}")
        out.append("")
        grand_low += c.total_low
        grand_high += c.total_high
    if len(costs) != 1:
        out.append(f"GRAND TOTAL over {len(costs)} call(s): {_fmt_money(grand_low, grand_high)}")
    return "\n".join(out)


def render_json(costs: list[CallCost], rates: dict) -> str:
    payload = {
        "effective_date": rates.get("effective_date"),
        "currency": rates.get("currency", "USD"),
        "calls": [
            {
                "call_sid": c.call_sid,
                "client_id": c.client_id,
                "created_at": c.created_at,
                "duration_sec": c.duration_sec,
                "total_low": round(c.total_low, 6),
                "total_high": round(c.total_high, 6),
                "lines": [
                    {**asdict(ln), "low": round(ln.low, 6), "high": round(ln.high, 6)}
                    for ln in c.lines
                ],
            }
            for c in costs
        ],
        "grand_total_low": round(sum(c.total_low for c in costs), 6),
        "grand_total_high": round(sum(c.total_high for c in costs), 6),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #
def _build_twilio_client(project_root: Path):
    """Return a Twilio Client, or None if creds/SDK are unavailable."""
    try:
        from dotenv import load_dotenv

        env_path = project_root / ".env"
        if env_path.exists():
            load_dotenv(env_path)
    except ImportError:
        pass
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        return None
    try:
        from twilio.rest import Client
    except ImportError:
        return None
    return Client(sid, token)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--call-sid", help="cost a single call by CallSid")
    ap.add_argument("--since", help="range start date YYYY-MM-DD (inclusive)")
    ap.add_argument("--until", help="range end date YYYY-MM-DD (inclusive)")
    ap.add_argument("--client-id", help="filter a range to one client")
    ap.add_argument("--db", default=os.environ.get("TRANSCRIPT_DB_PATH", DEFAULT_DB_PATH),
                    help="transcripts DB path (default: $TRANSCRIPT_DB_PATH or /data/transcripts.db)")
    ap.add_argument("--rates", default=DEFAULT_RATES_PATH, help="rate file (default: config/cost_rates.json)")
    ap.add_argument("--no-twilio", action="store_true", help="skip Twilio API (fully offline, $0)")
    ap.add_argument("--json", action="store_true", help="machine-readable JSON output")
    args = ap.parse_args(argv)

    if not args.call_sid and not (args.since and args.until):
        ap.error("provide either --call-sid or both --since and --until")

    try:
        rates = load_rates(args.rates)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: could not load rates: {e}", file=sys.stderr)
        return 2

    calls = read_calls(
        args.db, call_sid=args.call_sid, since=args.since, until=args.until, client_id=args.client_id
    )
    if not calls:
        print("No matching calls found.", file=sys.stderr)
        return 1

    usage_by_sid = read_usage(args.db, [c.call_sid for c in calls])

    client = None
    if not args.no_twilio:
        client = _build_twilio_client(Path(__file__).resolve().parent.parent)
        if client is None:
            print("(Twilio creds/SDK unavailable, voice line modeled, SMS skipped; "
                  "use --no-twilio to silence)", file=sys.stderr)

    costs: list[CallCost] = []
    for c in calls:
        voice_price = fetch_voice_price(client, c.call_sid) if client else None
        # SMS attribution is per-call and noisy in range mode; limit to single-call runs.
        sms_items = fetch_sms(client, c) if (client and args.call_sid) else None
        costs.append(
            compute_call_cost(
                c, rates, usage=usage_by_sid.get(c.call_sid), voice_price=voice_price, sms_items=sms_items
            )
        )

    print(render_json(costs, rates) if args.json else render_text(costs, rates))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
