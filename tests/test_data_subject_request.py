"""Tests for the DSAR (data-subject request) tool.

Focus: a right-to-erasure (delete) request must scrub matching transcripts
from the durable SQLite store, not just the booking-sheet rows (audit S3).
The Google Sheets API is mocked out, these tests don't touch the network.
"""
from __future__ import annotations

import json

import tools.data_subject_request as dsr
from tools.llm_proxy import transcript_store
from tools.llm_proxy.sheets_writer import HEADER_ROW


class _FakeWriter:
    _sheet_id = "SID"

    def __init__(self, rows):
        self._rows = rows

    def read_all_rows(self):
        return self._rows


def _row(last4: str, call_id: str) -> list:
    r = [""] * len(HEADER_ROW)
    r[HEADER_ROW.index("callback_number_last4")] = last4
    r[HEADER_ROW.index("vapi_call_id")] = call_id
    return r


def test_dsar_delete_also_erases_matching_transcripts(monkeypatch, capsys) -> None:
    rows = [HEADER_ROW, _row("1234", "CA1"), _row("9999", "CA2"), _row("1234", "CA3")]
    monkeypatch.setattr(dsr, "_build_writer", lambda cid: (_FakeWriter(rows), "PREFIX"))
    monkeypatch.setattr(dsr, "_delete_rows", lambda writer, idx: None)  # skip Sheets API

    seen = {}

    def _fake_delete(last4, **kwargs):
        seen["last4"] = last4
        return 2

    monkeypatch.setattr(transcript_store, "delete_by_caller_last4", _fake_delete)

    rc = dsr.main(
        ["--client", "clearslope", "--phone", "+15551231234",
         "--action", "delete", "--confirm"]
    )
    receipt = json.loads(capsys.readouterr().out)

    # The transcript store was erased for the requester's last-4.
    assert seen["last4"] == "1234"
    # The receipt proves transcript erasure, not just sheet-row deletion.
    assert receipt["result"]["transcripts_deleted"] == 2
    assert receipt["result"]["applied"] is True
    assert rc == 0


def test_dsar_delete_without_confirm_does_not_touch_transcripts(monkeypatch, capsys) -> None:
    rows = [HEADER_ROW, _row("1234", "CA1")]
    monkeypatch.setattr(dsr, "_build_writer", lambda cid: (_FakeWriter(rows), "PREFIX"))

    called = False

    def _fake_delete(last4, **kwargs):
        nonlocal called
        called = True
        return 1

    monkeypatch.setattr(transcript_store, "delete_by_caller_last4", _fake_delete)

    dsr.main(["--client", "clearslope", "--phone", "+15551231234", "--action", "delete"])
    receipt = json.loads(capsys.readouterr().out)

    assert called is False, "no --confirm must not delete anything"
    assert receipt["result"]["transcripts_deleted"] == 0
