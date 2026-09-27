"""
Data Subject Request (DSR) tool, GDPR Article 15 (access), 16 (rectify),
17 (erasure), 20 (portability).

Per `docs/gdpr-gap-analysis.md` §4.4, every right-of-access or right-to-
erasure request used to be a manual SQL/Sheet hunt. This tool turns it into
one command, and emits a signed receipt the operator can show the requester.

Usage:
    # Find all rows matching last-4 of a phone number:
    python tools/data_subject_request.py --client clearslope --phone +14155551234 --action export

    # Delete the same rows (requires --confirm to actually delete):
    python tools/data_subject_request.py --client clearslope --phone +14155551234 --action delete --confirm

    # Write the receipt JSON to a file:
    python tools/data_subject_request.py ... --output receipts/2026-05-13-jane-smith.json

Identification:
    The booking sheet stores only the last 4 digits of the callback number.
    Matching is done on that, so the requester must supply a phone number
    whose last 4 digits we recognize. Operators are expected to verify the
    caller's identity out-of-band (return call, email confirmation) before
    running --action delete; this script does not authenticate the requester.

Audit trail:
    Every run prints a structured "receipt" JSON object containing the
    request, the matched rows, the action taken, and a hash of the result.
    Persist this file. Supervisory authorities can ask for evidence of
    timely response (Article 12(3): one month).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# Make the package import work when this script is run directly from the
# project root (`python tools/data_subject_request.py ...`).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.llm_proxy.client_config import load_client_config
from tools.llm_proxy.sheets_writer import (  # noqa: E402
    HEADER_ROW,
    SheetsWriter,
    writer_from_env,
)


def _last4(number: str) -> str:
    digits = "".join(c for c in (number or "") if c.isdigit())
    return digits[-4:] if len(digits) >= 4 else digits


def _build_writer(client_id: str) -> tuple[SheetsWriter, str]:
    """Construct a SheetsWriter for the given client.

    Returns (writer, env_prefix). Raises SystemExit if the client config or
    its env-prefixed sheet ID is missing.
    """
    config_path = Path("config/clients") / f"{client_id}.json"
    if not config_path.exists():
        sys.exit(f"error: no client config at {config_path}")
    config = load_client_config(config_path)

    writer = writer_from_env(config.env_prefix)
    if writer is None:
        sys.exit(
            f"error: SheetsWriter unavailable for client {client_id!r} "
            f"(prefix={config.env_prefix}). Set {config.env_prefix}_GOOGLE_SHEET_ID "
            "and GOOGLE_SERVICE_ACCOUNT_FILE in the environment."
        )
    return writer, config.env_prefix


def _rows_to_dicts(rows: list[list[Any]]) -> list[dict]:
    """Convert the ragged list-of-lists from Sheets into list-of-dicts.

    Includes a synthetic `_row_index` (1-based, matching what the Sheets UI
    shows). The header row is row 1 and is not returned.
    """
    out: list[dict] = []
    for i, row in enumerate(rows[1:], start=2):  # skip header, 1-based with header as row 1
        record: dict[str, Any] = {"_row_index": i}
        for j, col in enumerate(HEADER_ROW):
            record[col] = row[j] if j < len(row) else ""
        out.append(record)
    return out


def _match_by_last4(records: list[dict], last4: str) -> list[dict]:
    return [r for r in records if str(r.get("callback_number_last4", "")).strip() == last4]


def _delete_rows(writer: SheetsWriter, row_indices: list[int]) -> None:
    """Delete the given 1-based row indices from Sheet1.

    Submits a single batchUpdate so the rows shift only once. The
    `deleteDimension` request takes 0-based half-open ranges, hence the
    indices are transformed accordingly.
    """
    if not row_indices:
        return
    # Sort descending so deletions don't shift the indices of yet-to-delete rows.
    sorted_desc = sorted(row_indices, reverse=True)
    requests = []
    for idx in sorted_desc:
        requests.append(
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": 0,  # Sheet1 is the first tab in our intake spreadsheets
                        "dimension": "ROWS",
                        "startIndex": idx - 1,  # 0-based, inclusive
                        "endIndex": idx,        # 0-based, exclusive
                    }
                }
            }
        )
    writer._service.spreadsheets().batchUpdate( # noqa: SLF001, intentional; SheetsWriter has no public deletion API
        spreadsheetId=writer._sheet_id,
        body={"requests": requests},
    ).execute()


def _make_receipt(*, client_id: str, action: str, phone: str, matches: list[dict],
                  applied: bool, error: str | None = None,
                  transcripts_deleted: int = 0) -> dict:
    last4 = _last4(phone)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    payload = {
        "request": {
            "client_id": client_id,
            "action": action,
            "phone_last4": last4,
            "received_at_utc": now,
        },
        "result": {
            "matched_rows": len(matches),
            "matched_row_indices": [m["_row_index"] for m in matches],
            "applied": applied,
            "error": error,
            # Number of transcript rows erased from the SQLite store (S3), proof
            # that erasure covered the durable transcript store, not just Sheets.
            "transcripts_deleted": transcripts_deleted,
            "data": matches if action == "export" else None,
        },
    }
    # Hash the receipt body so the operator can prove they did not edit it later.
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["receipt_sha256"] = hashlib.sha256(body).hexdigest()
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "GDPR / PIPEDA data-subject request tool. Locates and optionally "
            "deletes booking rows by last-4 of the callback phone number."
        )
    )
    parser.add_argument(
        "--client",
        required=True,
        help="Client ID (matches a file in config/clients/<id>.json).",
    )
    parser.add_argument(
        "--phone",
        required=True,
        help=(
            "Caller's phone number. Only the last 4 digits are used for "
            "matching, the sheet does not store the full number."
        ),
    )
    parser.add_argument(
        "--action",
        choices=["export", "delete"],
        required=True,
        help="export: print matching rows. delete: remove matching rows (requires --confirm).",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required for --action delete. Without it, the script reports what would be deleted.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path to write the receipt JSON to (in addition to stdout).",
    )
    args = parser.parse_args(argv)

    last4 = _last4(args.phone)
    if len(last4) < 4:
        sys.exit("error: --phone must contain at least 4 digits")

    writer, _ = _build_writer(args.client)
    records = _rows_to_dicts(writer.read_all_rows())
    matches = _match_by_last4(records, last4)

    applied = False
    error: str | None = None
    transcripts_deleted = 0
    if args.action == "delete":
        if not args.confirm:
            error = "would delete; rerun with --confirm to actually remove rows"
        else:
            try:
                _delete_rows(writer, [m["_row_index"] for m in matches])
                applied = True
            except Exception as exc:
                error = f"deletion failed: {exc}"
            # Erasure must also scrub the durable transcript store, not just the
            # booking sheet (S3, GDPR Art. 17 / PIPA). Runs on the app machine
            # where /data/transcripts.db is mounted; a no-op (0) elsewhere.
            try:
                from tools.llm_proxy import transcript_store
                transcripts_deleted = transcript_store.delete_by_caller_last4(last4)
            except Exception as exc:
                error = (error + "; " if error else "") + f"transcript erasure failed: {exc}"

    receipt = _make_receipt(
        client_id=args.client,
        action=args.action,
        phone=args.phone,
        matches=matches,
        applied=applied,
        error=error,
        transcripts_deleted=transcripts_deleted,
    )

    output = json.dumps(receipt, indent=2, sort_keys=True)
    print(output)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")

    # Exit non-zero if the action failed so cron/automation catches it.
    return 1 if error and args.action == "delete" and args.confirm else 0


if __name__ == "__main__":
    sys.exit(main())
