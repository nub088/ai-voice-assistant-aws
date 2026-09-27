"""
Append rows to the Clear Slope booking-intake Google Sheet.

Uses a service account JSON (path in `GOOGLE_SERVICE_ACCOUNT_FILE`) and writes
to the sheet whose ID is in `GOOGLE_SHEET_ID`. The sheet must be shared with
the service account email or this will fail with a permissions error.

Sheet layout (A→M). The first row is the header; we append below it.
The header is created on first write if the sheet is empty.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger("sheets_writer")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Header order matches the booking-intake schema. The proxy / webhook code
# converts a tool-call args dict into a row using `format_row()`.
HEADER_ROW = [
    "timestamp",
    "vapi_call_id",
    "caller_name",
    "callback_number_last4",
    "service_address",
    "identified_pest",
    "pest_description",
    "urgency",
    "best_callback_time",
    "out_of_scope",
    "needs_followup_notes",
    "ai_confidence",
    "transcript_retain",
]

DEFAULT_SHEET_RANGE = "Sheet1!A:M"

VOICEMAIL_TAB_NAME = "Voicemails"

VOICEMAIL_HEADER = [
    "created_at",
    "call_sid",
    "recording_sid",
    "caller_number_last4",
    "duration_seconds",
    "transcript",
    "playback_token_expires_at",
    "sms_sent_at",
]


def _get(args: dict[str, Any], *keys: str, default: str = "") -> str:
    """Return the first non-empty value found under any of the given keys."""
    for k in keys:
        v = args.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return default


def format_row(
    args: dict[str, Any],
    *,
    vapi_call_id: str | None = None,
    transcript_retain: bool = True,
) -> list[Any]:
    """Convert a tool-call args dict into a row matching `HEADER_ROW`.

    `transcript_retain` reflects the caller's recording-consent preference:
    True (default) means the redacted transcript may be retained for QA.
    False means the caller said 'no transcript' and the transcript log line
    must be suppressed at end-of-call. See PIPA s. 14 (right to withdraw).
    """
    full_number = _get(args, "callback_number", "callback_phone", "callerNumber")
    digits = "".join(c for c in full_number if c.isdigit())
    last4 = digits[-4:] if len(digits) >= 4 else digits

    return [
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        vapi_call_id or "",
        _get(args, "caller_name", "callerName"),
        last4,
        _get(args, "service_address", "address"),
        _get(args, "identified_pest", "identifiedPest"),
        _get(args, "pest_description", "reported_pest", "reportedPest"),
        _get(args, "urgency"),
        _get(args, "best_callback_time", "preferred_time_window", "preferredCallbackTime", "preferredTimeWindow"),
        bool(args.get("out_of_scope") or False),
        _get(args, "needs_followup_notes", "notes"),
        _get(args, "ai_confidence"),
        bool(transcript_retain),
    ]


class SheetsWriter:
    def __init__(
        self,
        service_account_file: str,
        sheet_id: str,
        sheet_range: str = DEFAULT_SHEET_RANGE,
    ):
        if not Path(service_account_file).exists():
            raise FileNotFoundError(
                f"GOOGLE_SERVICE_ACCOUNT_FILE not found: {service_account_file}"
            )
        if not sheet_id:
            raise ValueError("GOOGLE_SHEET_ID is empty")
        self._sheet_id = sheet_id
        self._range = sheet_range
        self._voicemail_sheet_id: int | None = None
        creds = Credentials.from_service_account_file(service_account_file, scopes=SCOPES)
        self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    def ensure_header(self) -> None:
        """Write the header row if the sheet is empty. Idempotent."""
        try:
            existing = (
                self._service.spreadsheets()
                .values()
                .get(spreadsheetId=self._sheet_id, range="Sheet1!A1:M1")
                .execute()
            )
        except HttpError as exc:
            raise RuntimeError(f"could not read sheet header (check share + ID): {exc}") from exc

        if existing.get("values"):
            return  # header already there

        try:
            self._service.spreadsheets().values().update(
                spreadsheetId=self._sheet_id,
                range="Sheet1!A1:M1",
                valueInputOption="RAW",
                body={"values": [HEADER_ROW]},
            ).execute()
        except HttpError as exc:
            raise RuntimeError(f"could not write sheet header (check Editor permission): {exc}") from exc
        logger.info("wrote header row to sheet %s", self._sheet_id)

    def append_row(self, row: list[Any]) -> None:
        if len(row) != len(HEADER_ROW):
            raise ValueError(
                f"row length mismatch: got {len(row)}, expected {len(HEADER_ROW)} ({HEADER_ROW})"
            )
        try:
            self._service.spreadsheets().values().append(
                spreadsheetId=self._sheet_id,
                range=self._range,
                # RAW (not USER_ENTERED): caller-controlled fields must be stored
                # as literal text, never evaluated as formulas (S5, a caller
                # name like `=IMPORTRANGE(...)` would otherwise exfiltrate data).
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            ).execute()
        except HttpError as exc:
            raise RuntimeError(f"sheets append failed: {exc}") from exc

    def read_all_rows(self) -> list[list[Any]]:
        """Return raw cell values from the intake tab, header included.

        Used by the weekly digest. Empty cells come back missing from each
        sub-list (Sheets API truncates trailing empties), so callers must
        be tolerant of ragged rows.
        """
        try:
            result = (
                self._service.spreadsheets()
                .values()
                .get(spreadsheetId=self._sheet_id, range=self._range)
                .execute()
            )
        except HttpError as exc:
            raise RuntimeError(f"sheets read failed: {exc}") from exc
        return result.get("values", [])

    def ensure_tab(self, tab_name: str, header: list[Any]) -> None:
        """Create `tab_name` if missing; write `header` to row 1 if the tab is empty.

        Idempotent, safe to call before every digest run.
        """
        try:
            meta = (
                self._service.spreadsheets()
                .get(spreadsheetId=self._sheet_id, fields="sheets(properties(title))")
                .execute()
            )
        except HttpError as exc:
            raise RuntimeError(f"could not read spreadsheet metadata: {exc}") from exc

        existing_titles = {
            s.get("properties", {}).get("title")
            for s in meta.get("sheets", [])
        }
        if tab_name not in existing_titles:
            try:
                self._service.spreadsheets().batchUpdate(
                    spreadsheetId=self._sheet_id,
                    body={
                        "requests": [
                            {"addSheet": {"properties": {"title": tab_name}}}
                        ]
                    },
                ).execute()
            except HttpError as exc:
                raise RuntimeError(f"could not create tab {tab_name!r}: {exc}") from exc
            logger.info("created sheet tab %r", tab_name)

        header_range = f"{tab_name}!A1"
        try:
            existing = (
                self._service.spreadsheets()
                .values()
                .get(spreadsheetId=self._sheet_id, range=header_range)
                .execute()
            )
        except HttpError as exc:
            raise RuntimeError(
                f"could not read header on tab {tab_name!r}: {exc}"
            ) from exc

        if not existing.get("values"):
            try:
                self._service.spreadsheets().values().update(
                    spreadsheetId=self._sheet_id,
                    range=header_range,
                    valueInputOption="RAW",
                    body={"values": [header]},
                ).execute()
            except HttpError as exc:
                raise RuntimeError(
                    f"could not write header on tab {tab_name!r}: {exc}"
                ) from exc
            logger.info("wrote header to tab %r", tab_name)

    def append_to_tab(self, tab_name: str, row: list[Any]) -> None:
        """Append a row to an arbitrary tab (e.g. the weekly digest tab)."""
        try:
            self._service.spreadsheets().values().append(
                spreadsheetId=self._sheet_id,
                range=f"{tab_name}!A:Z",
                # RAW: voicemail transcripts and digest rows are data, never
                # formulas, store literally to block formula injection (S5).
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            ).execute()
        except HttpError as exc:
            raise RuntimeError(
                f"sheets append to tab {tab_name!r} failed: {exc}"
            ) from exc

    def read_voicemail_rows(self) -> list[list[Any]]:
        """Return raw cells from the Voicemails tab, header included.

        Trailing empty cells are truncated by the Sheets API; callers must be
        tolerant of ragged rows.
        """
        try:
            result = (
                self._service.spreadsheets()
                .values()
                .get(spreadsheetId=self._sheet_id, range=f"{VOICEMAIL_TAB_NAME}!A:H")
                .execute()
            )
        except HttpError as exc:
            raise RuntimeError(f"voicemail sheet read failed: {exc}") from exc
        return result.get("values", [])

    def delete_voicemail_row(self, row_index: int) -> None:
        """Delete a single row from the Voicemails tab. `row_index` is 1-indexed
        in spreadsheet terms (row 1 = header, so first data row is 2)."""
        if row_index < 2:
            raise ValueError("refusing to delete header row")
        if self._voicemail_sheet_id is None:
            try:
                meta = (
                    self._service.spreadsheets()
                    .get(spreadsheetId=self._sheet_id, fields="sheets(properties(sheetId,title))")
                    .execute()
                )
            except HttpError as exc:
                raise RuntimeError(f"could not read spreadsheet metadata: {exc}") from exc
            for s in meta.get("sheets", []):
                if s.get("properties", {}).get("title") == VOICEMAIL_TAB_NAME:
                    self._voicemail_sheet_id = s["properties"]["sheetId"]
                    break
            if self._voicemail_sheet_id is None:
                raise RuntimeError(f"tab {VOICEMAIL_TAB_NAME!r} not found")
        try:
            self._service.spreadsheets().batchUpdate(
                spreadsheetId=self._sheet_id,
                body={"requests": [{
                    "deleteDimension": {
                        "range": {
                            "sheetId": self._voicemail_sheet_id,
                            "dimension": "ROWS",
                            "startIndex": row_index - 1,
                            "endIndex": row_index,
                        }
                    }
                }]},
            ).execute()
        except HttpError as exc:
            raise RuntimeError(f"voicemail row delete failed: {exc}") from exc

    def update_voicemail_transcript(self, recording_sid: str, transcript: str) -> bool:
        """Find the Voicemails row by recording_sid and overwrite its transcript cell.

        Returns True if a row was found and updated, False if no match exists
        (e.g. the row was already pruned by the retention job). Idempotent,         re-running on an already-written transcript just rewrites the same cell.
        """
        if not recording_sid:
            return False
        tab = VOICEMAIL_TAB_NAME
        try:
            result = (
                self._service.spreadsheets()
                .values()
                .get(spreadsheetId=self._sheet_id, range=f"{tab}!A:H")
                .execute()
            )
        except HttpError as exc:
            raise RuntimeError(f"sheets read failed: {exc}") from exc
        rows = result.get("values", [])
        if not rows:
            return False
        try:
            sid_col = VOICEMAIL_HEADER.index("recording_sid")
            transcript_col = VOICEMAIL_HEADER.index("transcript")
        except ValueError as exc:
            raise RuntimeError("VOICEMAIL_HEADER missing required columns") from exc
        for offset, row in enumerate(rows[1:], start=2):  # row 1 is header
            if len(row) > sid_col and row[sid_col] == recording_sid:
                cell = chr(ord("A") + transcript_col) + str(offset)
                try:
                    self._service.spreadsheets().values().update(
                        spreadsheetId=self._sheet_id,
                        range=f"{tab}!{cell}",
                        valueInputOption="RAW",
                        body={"values": [[transcript]]},
                    ).execute()
                except HttpError as exc:
                    raise RuntimeError(f"sheets transcript update failed: {exc}") from exc
                logger.info(
                    "voicemail transcript updated sid=%s row=%d", recording_sid, offset
                )
                return True
        return False


def writer_from_env(prefix: str) -> SheetsWriter | None:
    """Construct a SheetsWriter for one client from environment variables.

    Reads {prefix}_GOOGLE_SHEET_ID for the per-client sheet (D-06).
    GOOGLE_SERVICE_ACCOUNT_FILE is a single global credential (D-05).
    Returns None if required env vars are unset.
    """
    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    sheet_id = os.environ.get(f"{prefix}_GOOGLE_SHEET_ID", "").strip()
    if not sa or not sheet_id:
        return None
    try:
        writer = SheetsWriter(service_account_file=sa, sheet_id=sheet_id)
        writer.ensure_header()
        return writer
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        logger.warning("SheetsWriter unavailable for prefix=%s: %s", prefix, exc)
        return None
