"""Unit tests for sheets_writer.format_row (Sheets API itself isn't tested
here, that's covered by the live smoke test once a service-account file is
in .env)."""
from __future__ import annotations

from typing import Any

from tools.llm_proxy.sheets_writer import HEADER_ROW, format_row


def test_format_row_matches_header_length() -> None:
    row = format_row({}, vapi_call_id="call_x")
    assert len(row) == len(HEADER_ROW)


def test_format_row_truncates_phone_to_last4() -> None:
    args = {"caller_name": "Alex", "callback_number": "604-555-0123", "service_address": "100 Main St"}
    row = format_row(args, vapi_call_id="call_1")
    # callback_number_last4 is column index 3
    last4_idx = HEADER_ROW.index("callback_number_last4")
    assert row[last4_idx] == "0123"
    # full number should not appear anywhere in the row
    assert all("604" not in str(cell) for cell in row), row


def test_format_row_handles_short_or_missing_phone() -> None:
    assert format_row({"callback_number": "12"}, vapi_call_id="x")[HEADER_ROW.index("callback_number_last4")] == "12"
    assert format_row({"callback_number": ""}, vapi_call_id="x")[HEADER_ROW.index("callback_number_last4")] == ""
    assert format_row({}, vapi_call_id="x")[HEADER_ROW.index("callback_number_last4")] == ""


def test_format_row_includes_vapi_call_id() -> None:
    row = format_row({"caller_name": "Alex"}, vapi_call_id="call_xyz")
    assert row[HEADER_ROW.index("vapi_call_id")] == "call_xyz"


def test_format_row_coerces_out_of_scope_to_bool() -> None:
    truthy = format_row({"out_of_scope": "yes"}, vapi_call_id="x")
    falsy = format_row({"out_of_scope": False}, vapi_call_id="x")
    missing = format_row({}, vapi_call_id="x")
    idx = HEADER_ROW.index("out_of_scope")
    assert truthy[idx] is True
    assert falsy[idx] is False
    assert missing[idx] is False


def test_format_row_timestamp_is_iso_utc() -> None:
    row = format_row({}, vapi_call_id="x")
    ts = row[HEADER_ROW.index("timestamp")]
    assert isinstance(ts, str)
    assert ts.endswith("Z")
    assert "T" in ts  # ISO 8601 separator


def test_format_row_includes_transcript_retain_default_true() -> None:
    """Default to retain when the caller didn't opt out (PIPA implied-consent default)."""
    row = format_row({"caller_name": "Alex"}, vapi_call_id="x")
    assert len(row) == len(HEADER_ROW)
    idx = HEADER_ROW.index("transcript_retain")
    assert row[idx] is True


def test_format_row_respects_transcript_retain_kwarg() -> None:
    row = format_row({"caller_name": "Alex"}, vapi_call_id="x", transcript_retain=False)
    assert row[HEADER_ROW.index("transcript_retain")] is False


def test_header_row_extends_to_thirteen_columns() -> None:
    assert len(HEADER_ROW) == 13
    assert HEADER_ROW[-1] == "transcript_retain"


# --- Voicemail tab tests ---------------------------------------------------
#
# These tests mock the inner Google Sheets API surface that SheetsWriter
# talks to (`self._service.spreadsheets().values().get / .update`).
# The chained .spreadsheets().values().get(...).execute() pattern is recorded
# by a tiny inline fake so we can assert on what update() was called with.


class _FakeExecutable:
    """Records the chained call (range, body) and returns a canned result."""

    def __init__(self, recorder: list[dict], call: dict, result: Any) -> None:
        self._recorder = recorder
        self._call = call
        self._result = result

    def execute(self) -> Any:
        self._recorder.append(self._call)
        return self._result


class _FakeValues:
    def __init__(self, get_result: dict, update_recorder: list[dict]) -> None:
        self._get_result = get_result
        self._update_recorder = update_recorder
        self.get_calls: list[dict] = []

    def get(self, *, spreadsheetId: str, range: str) -> _FakeExecutable:
        call = {"spreadsheetId": spreadsheetId, "range": range}
        return _FakeExecutable(self.get_calls, call, self._get_result)

    def update(
        self, *, spreadsheetId: str, range: str, valueInputOption: str, body: dict
    ) -> _FakeExecutable:
        call = {
            "spreadsheetId": spreadsheetId,
            "range": range,
            "valueInputOption": valueInputOption,
            "body": body,
        }
        return _FakeExecutable(self._update_recorder, call, {})


class _FakeSpreadsheets:
    def __init__(self, values: _FakeValues) -> None:
        self._values = values

    def values(self) -> _FakeValues:
        return self._values


class _FakeService:
    def __init__(self, get_result: dict) -> None:
        self.update_calls: list[dict] = []
        self._values = _FakeValues(get_result, self.update_calls)
        self._spreadsheets = _FakeSpreadsheets(self._values)

    def spreadsheets(self) -> _FakeSpreadsheets:
        return self._spreadsheets


def _make_writer_with_fake(get_result: dict) -> tuple[Any, _FakeService]:
    """Construct a SheetsWriter without invoking __init__ (which needs creds)
    and inject a fake Google API service."""
    from tools.llm_proxy.sheets_writer import SheetsWriter

    writer = SheetsWriter.__new__(SheetsWriter)
    writer._sheet_id = "SHEET_ID_TEST"
    writer._range = "Sheet1!A:M"
    fake = _FakeService(get_result)
    writer._service = fake
    return writer, fake


def test_voicemail_header_constant_present() -> None:
    from tools.llm_proxy.sheets_writer import VOICEMAIL_HEADER, VOICEMAIL_TAB_NAME
    assert VOICEMAIL_TAB_NAME == "Voicemails"
    assert VOICEMAIL_HEADER == [
        "created_at",
        "call_sid",
        "recording_sid",
        "caller_number_last4",
        "duration_seconds",
        "transcript",
        "playback_token_expires_at",
        "sms_sent_at",
    ]


def test_update_voicemail_transcript_finds_and_updates_row() -> None:
    """Late transcription arrival should rewrite the transcript cell in place."""
    from tools.llm_proxy.sheets_writer import VOICEMAIL_HEADER

    header = list(VOICEMAIL_HEADER)
    data_row = [
        "2026-05-22T10:00:00Z",
        "CAcall1",
        "REabc",
        "0123",
        "12",
        "transcript pending",
        "2026-05-23T10:00:00Z",
        "",
    ]
    writer, fake = _make_writer_with_fake({"values": [header, data_row]})

    ok = writer.update_voicemail_transcript("REabc", "the actual words")

    assert ok is True
    # Read happened against the full Voicemails range A:H.
    assert fake._values.get_calls == [
        {"spreadsheetId": "SHEET_ID_TEST", "range": "Voicemails!A:H"}
    ]
    # Update went to the transcript cell (column F, row 2) with the new text.
    assert len(fake.update_calls) == 1
    call = fake.update_calls[0]
    assert call["spreadsheetId"] == "SHEET_ID_TEST"
    assert call["range"] == "Voicemails!F2"
    assert call["valueInputOption"] == "RAW"
    assert call["body"] == {"values": [["the actual words"]]}


def test_update_voicemail_transcript_missing_sid_is_noop() -> None:
    """If the recording_sid isn't in the sheet, return False (no exception)."""
    from tools.llm_proxy.sheets_writer import VOICEMAIL_HEADER

    header = list(VOICEMAIL_HEADER)
    data_row = [
        "2026-05-22T10:00:00Z",
        "CAcall1",
        "REdifferent",
        "0123",
        "12",
        "transcript pending",
        "2026-05-23T10:00:00Z",
        "",
    ]
    writer, fake = _make_writer_with_fake({"values": [header, data_row]})

    ok = writer.update_voicemail_transcript("REabc", "the actual words")

    assert ok is False
    assert fake.update_calls == []


# --- Formula-injection defense (audit finding S5) --------------------------

def _writer_with_mock_service():
    """A SheetsWriter wired to a mock Google client (no creds / network)."""
    from unittest.mock import MagicMock

    from tools.llm_proxy.sheets_writer import SheetsWriter

    w = object.__new__(SheetsWriter)
    w._sheet_id = "SID"
    w._range = "Sheet1!A:M"
    w._voicemail_sheet_id = None
    w._service = MagicMock()
    return w


def test_append_row_uses_raw_value_input_to_block_formula_injection() -> None:
    """Caller-controlled fields (name/address/notes) must be stored literally,
    never evaluated as Sheets formulas (=IMPORTRANGE/HYPERLINK exfiltration)."""
    w = _writer_with_mock_service()
    append = w._service.spreadsheets.return_value.values.return_value.append
    append.return_value.execute.return_value = {}

    row = ['=IMPORTRANGE("evil","A1")'] + [""] * (len(HEADER_ROW) - 1)
    w.append_row(row)

    _, kwargs = append.call_args
    assert kwargs["valueInputOption"] == "RAW", "append_row must use RAW, not USER_ENTERED"
    assert kwargs["body"]["values"] == [row]


def test_append_to_tab_uses_raw_value_input() -> None:
    w = _writer_with_mock_service()
    append = w._service.spreadsheets.return_value.values.return_value.append
    append.return_value.execute.return_value = {}

    w.append_to_tab("Voicemails", ['=HYPERLINK("http://evil")', "x"])

    _, kwargs = append.call_args
    assert kwargs["valueInputOption"] == "RAW", "append_to_tab must use RAW, not USER_ENTERED"
