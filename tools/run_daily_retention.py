"""Daily retention runner, invoked by the Fly scheduled machine.

Runs three passes with --apply:
  1. retention_enforcer.py, filesystem prune (path-based, account-wide).
  2. Twilio recording prune, account-wide, runs once.
  3. Per-client voicemail sheet row prune, looped over config/clients/*.json.

Exits non-zero if any pass reports errors so Fly's scheduled-machine
UI / logs surface the failure.

Requires RETENTION_ALLOW_APPLY=1 in the environment. The Fly retention
process sets this in infra/fly/entrypoint.sh; a developer running this
locally with production credentials in .env will exit 2 without touching
data.

See infra/fly/fly.toml [processes].retention.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from twilio.rest import Client as TwilioClient

from tools import retention_enforcer
from tools.llm_proxy.sheets_writer import writer_from_env
from tools.voicemail_retention import prune_twilio_recordings, prune_voicemail_rows

CLIENTS_DIR = Path(__file__).resolve().parent.parent / "config" / "clients"
AUDIO_RETENTION_DAYS = 30
METADATA_RETENTION_DAYS = 90


def _client_prefixes() -> list[str]:
    prefixes: list[str] = []
    for path in sorted(CLIENTS_DIR.glob("*.json")):
        with path.open() as f:
            data = json.load(f)
        prefix = data.get("env_prefix")
        if prefix:
            prefixes.append(prefix)
    return prefixes


def main() -> int:
    if os.environ.get("RETENTION_ALLOW_APPLY") != "1":
        print(
            "run_daily_retention: refusing to --apply without "
            "RETENTION_ALLOW_APPLY=1 set in the environment. "
            "(The Fly retention process sets this; local runs must opt in.)",
            file=sys.stderr,
            flush=True,
        )
        return 2

    overall_rc = 0

    print("=== retention_enforcer.py ===", flush=True)
    rc = retention_enforcer.main(["--apply"])
    if rc:
        overall_rc = rc

    print("=== voicemail_retention: Twilio audio prune ===", flush=True)
    twilio = TwilioClient(
        os.environ["TWILIO_ACCOUNT_SID"],
        os.environ["TWILIO_AUTH_TOKEN"],
    )
    audio_summary = prune_twilio_recordings(
        client=twilio, max_age_days=AUDIO_RETENTION_DAYS, apply=True
    )
    print(json.dumps({"audio_summary": audio_summary}, sort_keys=True), flush=True)
    if audio_summary.get("errors", 0) > 0:
        overall_rc = 1

    for prefix in _client_prefixes():
        print(f"=== voicemail_retention: sheet prune ({prefix}) ===", flush=True)
        writer = writer_from_env(prefix=prefix)
        if writer is None:
            print(
                json.dumps(
                    {"prefix": prefix, "skipped": "no sheets writer"},
                    sort_keys=True,
                ),
                flush=True,
            )
            continue
        meta_summary = prune_voicemail_rows(
            writer=writer, max_age_days=METADATA_RETENTION_DAYS, apply=True
        )
        print(
            json.dumps(
                {"prefix": prefix, "metadata_summary": meta_summary},
                sort_keys=True,
            ),
            flush=True,
        )
        if meta_summary.get("errors", 0) > 0:
            overall_rc = 1

    return overall_rc


if __name__ == "__main__":
    sys.exit(main())
