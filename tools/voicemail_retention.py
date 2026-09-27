"""Voicemail retention, daily prune of expired audio + sheet rows.

Runs as a Fly scheduled task. Mirrors the safety posture of
`tools/retention_enforcer.py`: dry-run by default, JSON-per-action stdout
logging, --apply required to actually delete.

Two passes:
  1. Twilio recordings older than --audio-days (default 1; daily cron
     yields a practical 24-48h retention window).
  2. Sheet rows in the Voicemails tab older than --metadata-days (default 90).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import IO

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient

from tools.llm_proxy.sheets_writer import writer_from_env


def prune_twilio_recordings(
    *,
    client,
    max_age_days: int,
    apply: bool,
    out: IO[str] = sys.stdout,
) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    recordings = client.recordings.list(date_created_before=cutoff, limit=1000)
    would_delete = 0
    deleted = 0
    already_gone = 0
    errors = 0
    for rec in recordings:
        age_days = (datetime.now(timezone.utc) - rec.date_created).days
        event = {
            "action": "delete" if apply else "would-delete",
            "sid": rec.sid,
            "age_days": age_days,
        }
        if not apply:
            would_delete += 1
            print(json.dumps(event, sort_keys=True), file=out, flush=True)
            continue
        try:
            client.recordings(rec.sid).delete()
        except TwilioRestException as exc:
            if exc.status == 404:
                already_gone += 1
                event["action"] = "already-gone"
            else:
                errors += 1
                event["action"] = "error"
                event["error"] = f"twilio:{exc.status}:{exc.msg}"
        else:
            deleted += 1
        print(json.dumps(event, sort_keys=True), file=out, flush=True)
    return {
        "would_delete": would_delete,
        "deleted": deleted,
        "already_gone": already_gone,
        "errors": errors,
    }


def prune_voicemail_rows(
    *,
    writer,
    max_age_days: int,
    apply: bool,
    out: IO[str] = sys.stdout,
) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    rows = writer.read_voicemail_rows()
    if not rows:
        return {"would_delete": 0, "deleted": 0, "errors": 0}
    would_delete = 0
    deleted = 0
    errors = 0
    indexed = list(enumerate(rows[1:], start=2))  # row 2 = first data row
    for row_index, row in reversed(indexed):
        created_at = row[0] if row else ""
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except Exception:
            continue
        if created >= cutoff:
            continue
        event = {
            "action": "delete-row" if apply else "would-delete-row",
            "row_index": row_index,
            "created_at": created_at,
            "recording_sid": row[2] if len(row) > 2 else "",
        }
        if not apply:
            would_delete += 1
            print(json.dumps(event, sort_keys=True), file=out, flush=True)
            continue
        try:
            writer.delete_voicemail_row(row_index)
        except Exception as exc:
            errors += 1
            event["action"] = "error"
            event["error"] = str(exc)
        else:
            deleted += 1
        print(json.dumps(event, sort_keys=True), file=out, flush=True)
    return {"would_delete": would_delete, "deleted": deleted, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Voicemail retention. Dry-run by default.")
    p.add_argument("--apply", action="store_true", help="Actually delete.")
    p.add_argument("--audio-days", type=int, default=1, help="Days to retain audio (default: %(default)d)")
    p.add_argument("--metadata-days", type=int, default=90, help="Days to retain metadata (default: %(default)d)")
    p.add_argument("--prefix", default="ALLGREEN", help="Client env prefix for Sheet wiring.")
    args = p.parse_args(argv)

    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token = os.environ["TWILIO_AUTH_TOKEN"]
    client = TwilioClient(account_sid, auth_token)

    audio_summary = prune_twilio_recordings(
        client=client, max_age_days=args.audio_days, apply=args.apply
    )

    writer = writer_from_env(prefix=args.prefix)
    if writer is None:
        meta_summary = {"skipped": "no sheets writer"}
    else:
        meta_summary = prune_voicemail_rows(
            writer=writer, max_age_days=args.metadata_days, apply=args.apply
        )

    print(json.dumps({
        "summary": {
            "mode": "apply" if args.apply else "dry-run",
            "audio": audio_summary,
            "metadata": meta_summary,
        }
    }, sort_keys=True), flush=True)
    audio_errors = audio_summary.get("errors", 0)
    meta_errors = meta_summary.get("errors", 0)
    if audio_errors > 0 or meta_errors > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
