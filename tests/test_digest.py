"""Tests for the weekly call-intake digest.

`compute_digest` is a pure function over a list of `CallRow`s, so the heavy
test surface lives here. Sheets-side I/O (`SheetsWriter.read_all_rows`,
`append_digest_row`) is exercised by the live smoke path, not unit tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta


from tools.llm_proxy.digest import (
    CallRow,
    compute_digest,
    parse_rows,
    render_digest_row,
    render_digest_text,
)


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _row(
    *,
    ts: str,
    name: str = "",
    last4: str = "",
    pest: str = "",
    urgency: str = "routine",
    out_of_scope: bool = False,
    notes: str = "",
    confidence: str = "high",
    address: str = "",
) -> CallRow:
    return CallRow(
        timestamp=_utc(ts),
        call_id="CA" + name + last4,
        caller_name=name,
        callback_last4=last4,
        service_address=address,
        identified_pest=pest,
        urgency=urgency,
        out_of_scope=out_of_scope,
        needs_followup_notes=notes,
        ai_confidence=confidence,
    )


NOW = _utc("2026-05-02T12:00:00Z")


def test_compute_digest_filters_rows_outside_window() -> None:
    rows = [
        _row(ts="2026-05-01T10:00:00Z", name="Alex", last4="0123", pest="rat"),
        _row(ts="2026-04-20T10:00:00Z", name="Bo", last4="4444", pest="ant"),  # >7d ago
        _row(ts="2026-05-02T11:30:00Z", name="Cy", last4="5555", pest="wasp"),
    ]
    digest = compute_digest(rows, now=NOW, days=7)
    assert digest.total_calls == 2
    assert digest.period_end == NOW
    assert digest.period_start == NOW - timedelta(days=7)


def test_compute_digest_urgency_breakdown() -> None:
    rows = [
        _row(ts="2026-05-01T10:00:00Z", urgency="routine"),
        _row(ts="2026-05-01T11:00:00Z", urgency="routine"),
        _row(ts="2026-05-01T12:00:00Z", urgency="soon"),
        _row(ts="2026-05-01T13:00:00Z", urgency="emergency"),
        _row(ts="2026-05-01T14:00:00Z", urgency="emergency"),
    ]
    digest = compute_digest(rows, now=NOW, days=7)
    assert digest.by_urgency == {"routine": 2, "soon": 1, "emergency": 2}


def test_compute_digest_top_pests_excludes_blank_pests() -> None:
    rows = [
        _row(ts="2026-05-01T10:00:00Z", pest="cockroach"),
        _row(ts="2026-05-01T11:00:00Z", pest="cockroach"),
        _row(ts="2026-05-01T12:00:00Z", pest="rat"),
        _row(ts="2026-05-01T13:00:00Z", pest=""),  # not identified
    ]
    digest = compute_digest(rows, now=NOW, days=7)
    pests = dict(digest.by_pest)
    assert pests == {"cockroach": 2, "rat": 1}


def test_compute_digest_lists_emergency_callers_with_address() -> None:
    rows = [
        _row(
            ts="2026-05-01T10:00:00Z", name="Alex Doe", last4="0123",
            urgency="emergency", pest="norway rat", address="100 Main St",
        ),
        _row(ts="2026-05-01T11:00:00Z", name="Bo", last4="4444", urgency="routine"),
    ]
    digest = compute_digest(rows, now=NOW, days=7)
    assert len(digest.emergencies) == 1
    em = digest.emergencies[0]
    assert "Alex Doe" in em
    assert "0123" in em
    assert "100 Main St" in em
    assert "norway rat" in em


def test_compute_digest_unique_callers_dedupes_by_name_and_last4() -> None:
    rows = [
        _row(ts="2026-05-01T10:00:00Z", name="Alex", last4="0123"),
        _row(ts="2026-05-01T11:00:00Z", name="Alex", last4="0123"),  # same caller, called twice
        _row(ts="2026-05-01T12:00:00Z", name="Alex", last4="9999"),  # different Alex
    ]
    digest = compute_digest(rows, now=NOW, days=7)
    assert digest.total_calls == 3
    assert digest.unique_callers == 2


def test_compute_digest_counts_out_of_scope_and_low_confidence_and_followup() -> None:
    rows = [
        _row(ts="2026-05-01T10:00:00Z", out_of_scope=True),
        _row(ts="2026-05-01T11:00:00Z", confidence="low"),
        _row(ts="2026-05-01T12:00:00Z", notes="caller mentioned mold, refer to specialist"),
        _row(ts="2026-05-01T13:00:00Z"),  # baseline: nothing flagged
    ]
    digest = compute_digest(rows, now=NOW, days=7)
    assert digest.out_of_scope == 1
    assert digest.low_confidence == 1
    assert digest.needs_followup == 1


def test_compute_digest_handles_empty_input() -> None:
    digest = compute_digest([], now=NOW, days=7)
    assert digest.total_calls == 0
    assert digest.by_urgency == {"routine": 0, "soon": 0, "emergency": 0}
    assert digest.by_pest == []
    assert digest.unique_callers == 0
    assert digest.emergencies == []


def test_parse_rows_maps_columns_by_header() -> None:
    sheet_values = [
        [
            "timestamp", "vapi_call_id", "caller_name", "callback_number_last4",
            "service_address", "identified_pest", "pest_description",
            "urgency", "best_callback_time", "out_of_scope",
            "needs_followup_notes", "ai_confidence",
        ],
        [
            "2026-05-01T10:00:00Z", "CA1", "Alex", "0123",
            "100 Main", "norway rat", "big rat",
            "emergency", "now", "FALSE",
            "", "high",
        ],
    ]
    parsed = parse_rows(sheet_values)
    assert len(parsed) == 1
    r = parsed[0]
    assert r.caller_name == "Alex"
    assert r.callback_last4 == "0123"
    assert r.identified_pest == "norway rat"
    assert r.urgency == "emergency"
    assert r.out_of_scope is False
    assert r.timestamp == _utc("2026-05-01T10:00:00Z")


def test_parse_rows_tolerates_ragged_rows_and_unparseable_timestamps() -> None:
    sheet_values = [
        ["timestamp", "vapi_call_id", "caller_name", "urgency"],
        ["2026-05-01T10:00:00Z", "CA1", "Alex", "routine"],
        ["", "CA2", "Skip", "routine"],   # no timestamp → skipped
        ["2026-05-01T11:00:00Z", "CA3"],   # short row → still ok
    ]
    parsed = parse_rows(sheet_values)
    assert [r.call_id for r in parsed] == ["CA1", "CA3"]


def test_parse_rows_treats_truthy_strings_as_out_of_scope() -> None:
    sheet_values = [
        ["timestamp", "out_of_scope"],
        ["2026-05-01T10:00:00Z", "TRUE"],
        ["2026-05-01T11:00:00Z", "true"],
        ["2026-05-01T12:00:00Z", "FALSE"],
        ["2026-05-01T13:00:00Z", ""],
    ]
    parsed = parse_rows(sheet_values)
    assert [r.out_of_scope for r in parsed] == [True, True, False, False]


def test_render_digest_text_includes_key_metrics() -> None:
    rows = [
        _row(ts="2026-05-01T10:00:00Z", name="Alex", last4="0123",
             pest="norway rat", urgency="emergency", address="100 Main"),
        _row(ts="2026-05-01T11:00:00Z", name="Bo", last4="4444",
             pest="cockroach", urgency="routine"),
    ]
    digest = compute_digest(rows, now=NOW, days=7)
    text = render_digest_text(digest)
    assert "Clear Slope" in text
    assert "2" in text  # total calls
    assert "emergency" in text.lower()
    assert "Alex Doe" in text or "Alex" in text
    assert "100 Main" in text
    assert "cockroach" in text


def test_render_digest_row_columns_match_expected_schema() -> None:
    rows = [
        _row(ts="2026-05-01T10:00:00Z", urgency="routine", pest="ant"),
        _row(ts="2026-05-01T11:00:00Z", urgency="emergency", pest="rat"),
    ]
    digest = compute_digest(rows, now=NOW, days=7)
    cells = render_digest_row(digest)
    # Schema: period_start, period_end, total, routine, soon, emergency,
    #         top_pest, out_of_scope, follow_up, low_confidence, unique_callers
    assert len(cells) == 11
    # period_start + period_end as ISO strings
    assert cells[0].startswith("2026-04-25")
    assert cells[1].startswith("2026-05-02")
    assert cells[2] == 2  # total
    assert cells[3] == 1  # routine
    assert cells[5] == 1  # emergency
