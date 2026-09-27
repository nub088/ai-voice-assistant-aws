"""Integration test for the digest CLI's `run()` flow.

Stubs the sheets client (read + ensure_tab + append_to_tab) so the full
read → compute → publish pipeline runs without touching Google Sheets.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from tools.llm_proxy.digest import DIGEST_HEADER_ROW, DIGEST_TAB_NAME
from tools.llm_proxy.digest_cli import run
from tools.llm_proxy.sheets_writer import HEADER_ROW


NOW = datetime.fromisoformat("2026-05-02T12:00:00+00:00")


class FakeSheetsClient:
    def __init__(self, values: list[list[Any]]) -> None:
        self._values = values
        self.tabs_ensured: list[tuple[str, list[Any]]] = []
        self.tab_appends: list[tuple[str, list[Any]]] = []

    def read_all_rows(self) -> list[list[Any]]:
        return self._values

    def ensure_tab(self, tab_name: str, header: list[Any]) -> None:
        self.tabs_ensured.append((tab_name, header))

    def append_to_tab(self, tab_name: str, row: list[Any]) -> None:
        self.tab_appends.append((tab_name, row))


def _intake_row(
    *,
    ts: str = "2026-05-01T10:00:00Z",
    name: str = "Alex",
    last4: str = "0123",
    address: str = "100 Main",
    pest: str = "norway rat",
    description: str = "big rat",
    urgency: str = "emergency",
    callback: str = "now",
    out_of_scope: str = "FALSE",
    notes: str = "",
    confidence: str = "high",
    call_id: str = "CA1",
) -> list[Any]:
    return [
        ts, call_id, name, last4, address, pest, description,
        urgency, callback, out_of_scope, notes, confidence,
    ]


def test_run_publishes_digest_row_and_returns_text() -> None:
    client = FakeSheetsClient(values=[
        list(HEADER_ROW),
        _intake_row(name="Alex Doe", urgency="emergency", pest="norway rat"),
        _intake_row(call_id="CA2", name="Bo", last4="4444",
                    pest="cockroach", urgency="routine"),
    ])

    text = run(client, now=NOW, days=7)

    # Tab is ensured exactly once with the digest schema.
    assert client.tabs_ensured == [(DIGEST_TAB_NAME, list(DIGEST_HEADER_ROW))]
    # One digest row appended.
    assert len(client.tab_appends) == 1
    tab, row = client.tab_appends[0]
    assert tab == DIGEST_TAB_NAME
    assert len(row) == len(DIGEST_HEADER_ROW)
    assert row[2] == 2  # total_calls

    # Rendered text covers what a human cares about.
    assert "Total calls: 2" in text
    assert "Alex Doe" in text
    assert "norway rat" in text
    assert "cockroach" in text


def test_run_dry_run_skips_publish() -> None:
    client = FakeSheetsClient(values=[list(HEADER_ROW), _intake_row()])
    text = run(client, now=NOW, days=7, dry_run=True)
    assert "Total calls: 1" in text
    assert client.tabs_ensured == []
    assert client.tab_appends == []


def test_run_handles_empty_sheet_gracefully() -> None:
    client = FakeSheetsClient(values=[])
    text = run(client, now=NOW, days=7)
    assert "Total calls: 0" in text
    assert len(client.tab_appends) == 1
    _, row = client.tab_appends[0]
    assert row[2] == 0  # total_calls


def test_run_filters_out_of_window_rows() -> None:
    client = FakeSheetsClient(values=[
        list(HEADER_ROW),
        _intake_row(ts="2026-05-01T10:00:00Z"),       # in window
        _intake_row(ts="2026-04-15T10:00:00Z", call_id="CA_old"),  # out
    ])
    text = run(client, now=NOW, days=7)
    assert "Total calls: 1" in text
    _, row = client.tab_appends[0]
    assert row[2] == 1
