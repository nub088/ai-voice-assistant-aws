"""Pull recent Twilio call logs and Debugger notifications to diagnose intermittent
call-setup failures (e.g. the "3 beeps then disconnect" symptom).

Usage:
    python tools/twilio_call_diagnostics.py [--days N] [--all]

Reads TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN from .env. Never prints the
auth token. Caller phone numbers are masked.

Reports:
  1. Call status breakdown across the window.
  2. Calls that did NOT complete normally (failed, no-answer, busy, canceled).
  3. Twilio Debugger notifications (error codes 11200, 11750, 31920/31921, etc.)
     correlated to CallSid where possible.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    from twilio.rest import Client
except ImportError:
    print("ERROR: twilio SDK not installed. Run: pip install twilio", file=sys.stderr)
    sys.exit(2)


def mask_phone(num: str | None) -> str:
    if not num:
        return "<none>"
    if len(num) <= 4:
        return "***"
    return f"{num[:3]}***{num[-2:]}"


def short_sid(sid: str | None) -> str:
    if not sid:
        return "<none>"
    return f"{sid[:4]}…{sid[-4:]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7, help="look back N days (default 7)")
    ap.add_argument("--all", action="store_true", help="show all calls, not just failures")
    args = ap.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    env_path = project_root / ".env"
    if load_dotenv and env_path.exists():
        load_dotenv(env_path)

    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        print("ERROR: TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set in env", file=sys.stderr)
        return 2

    client = Client(sid, token)
    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    print(f"=== Twilio diagnostics, last {args.days} day(s) ===")
    print(f"Account: {short_sid(sid)}")
    print(f"Window:  since {since.isoformat()}")
    print()

    # --- Calls ---
    calls = list(client.calls.list(start_time_after=since, limit=500))
    print(f"Total calls fetched: {len(calls)}")
    status_counts = Counter(c.status for c in calls)
    for status, n in status_counts.most_common():
        print(f"  {status:>12s}: {n}")
    print()

    # Failure-mode calls (incl. completed-but-very-short, which can indicate setup drops)
    bad_states = {"failed", "no-answer", "busy", "canceled"}
    failed = [c for c in calls if c.status in bad_states]
    very_short = [
        c for c in calls
        if c.status == "completed"
        and c.duration is not None
        and str(c.duration).isdigit()
        and int(c.duration) <= 3
    ]

    def print_call(c):
        dur = c.duration if c.duration is not None else "?"
        start = c.start_time.isoformat() if c.start_time else "?"
        from_num = getattr(c, "from_", None) or getattr(c, "_from", None) or getattr(c, "from_formatted", None)
        to_num = getattr(c, "to", None) or getattr(c, "to_formatted", None)
        direction = getattr(c, "direction", "?")
        print(
            f"  {start}  sid={short_sid(c.sid)}  status={c.status:<10s} "
            f"dur={dur}s  dir={direction:<10s} from={mask_phone(from_num)} to={mask_phone(to_num)}"
        )

    print(f"--- Failed / no-answer / busy / canceled ({len(failed)}) ---")
    for c in failed:
        print_call(c)
    print()
    print(f"--- Completed but ≤3s duration, possible setup drop ({len(very_short)}) ---")
    for c in very_short:
        print_call(c)
    print()

    if args.all:
        print(f"--- All {len(calls)} calls ---")
        for c in calls:
            print_call(c)
        print()

    # --- Monitor Alerts (modern Debugger feed) ---
    print("--- Twilio Monitor Alerts (modern Debugger) ---")
    try:
        alerts = list(client.monitor.v1.alerts.list(start_date=since, limit=500))
        print(f"Total alerts: {len(alerts)}")
        code_counts = Counter(a.error_code for a in alerts)
        for code, n in code_counts.most_common():
            print(f"  error_code {code}: {n}")
        print()

        for a in alerts[:80]:
            ts = a.date_generated.isoformat() if a.date_generated else "?"
            ref_sid = ""
            try:
                ref_sid = a.resource_sid or ""
            except Exception:
                pass
            print(
                f"  {ts}  level={a.log_level}  code={a.error_code}  "
                f"resource={short_sid(ref_sid) if ref_sid else '<none>'}"
            )
            if a.alert_text:
                print(f"      {a.alert_text[:240]}")
            if a.error_code in (11200, 11750, 31920, 31921):
                # These are the "smoking gun" codes for our symptom, surface more detail.
                if a.request_url:
                    print(f"      request_url={a.request_url}")
                if a.response_body:
                    print(f"      response_body={a.response_body[:240]}")
        if len(alerts) > 80:
            print(f"  … {len(alerts) - 80} more alerts truncated")
    except Exception as e:
        print(f"  (could not fetch alerts: {e})")
    print()

    # --- Per-call detail for the failed/short calls ---
    print("--- Per-call detail for ≤3s and failed calls ---")
    candidates = failed + very_short
    seen = set()
    for c in candidates:
        if c.sid in seen:
            continue
        seen.add(c.sid)
        try:
            d = client.calls(c.sid).fetch()
        except Exception as e:
            print(f"  {short_sid(c.sid)}: fetch failed: {e}")
            continue
        print(
            f"  {short_sid(d.sid)}  status={d.status}  dur={d.duration}s  "
            f"answered_by={getattr(d, 'answered_by', '?')}  "
            f"price={getattr(d, 'price', '?')}  "
            f"forwarded_from={getattr(d, 'forwarded_from', '?')}"
        )
        # Look for any notifications/events on this specific call via subresource.
        try:
            sub_alerts = list(client.monitor.v1.alerts.list(
                start_date=since, limit=50,
            ))
            related = [a for a in sub_alerts if getattr(a, "resource_sid", "") == d.sid]
            for a in related:
                print(f"      alert code={a.error_code}  {(a.alert_text or '')[:200]}")
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
