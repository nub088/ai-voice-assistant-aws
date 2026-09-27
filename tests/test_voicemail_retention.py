"""Tests for the voicemail-retention scheduled job."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from io import StringIO

import pytest

from tools.voicemail_retention import (
    prune_twilio_recordings,
    prune_voicemail_rows,
)


class _FakeRecording:
    def __init__(self, sid: str, days_old: int) -> None:
        self.sid = sid
        self.date_created = datetime.now(timezone.utc) - timedelta(days=days_old)


class _FakeRecordingsResource:
    def __init__(self, *, all_recordings):
        self._all = all_recordings
        self.deleted: list[str] = []

    def list(self, date_created_before=None, limit=None):
        if date_created_before is None:
            return list(self._all)
        return [r for r in self._all if r.date_created < date_created_before]

    def __call__(self, sid):
        outer = self
        class _Per:
            def delete(self):
                outer.deleted.append(sid)
        return _Per()


class _FakeTwilioClient:
    def __init__(self, recordings):
        self.recordings = recordings


def test_prune_twilio_recordings_dry_run_does_not_delete() -> None:
    fake_rec = _FakeRecordingsResource(all_recordings=[
        _FakeRecording("RE_old", 35),
        _FakeRecording("RE_new", 5),
    ])
    client = _FakeTwilioClient(fake_rec)
    out = StringIO()
    summary = prune_twilio_recordings(client=client, max_age_days=30, apply=False, out=out)
    assert summary["would_delete"] == 1
    assert summary["deleted"] == 0
    assert fake_rec.deleted == []
    lines = [json.loads(ln) for ln in out.getvalue().strip().splitlines() if ln]
    assert any(ln["action"] == "would-delete" and ln["sid"] == "RE_old" for ln in lines)


def test_prune_twilio_recordings_apply_deletes_only_old() -> None:
    fake_rec = _FakeRecordingsResource(all_recordings=[
        _FakeRecording("RE_old", 35),
        _FakeRecording("RE_borderline", 31),
        _FakeRecording("RE_new", 5),
    ])
    client = _FakeTwilioClient(fake_rec)
    out = StringIO()
    summary = prune_twilio_recordings(client=client, max_age_days=30, apply=True, out=out)
    assert summary["deleted"] == 2
    assert sorted(fake_rec.deleted) == ["RE_borderline", "RE_old"]


def test_prune_twilio_recordings_treats_404_as_already_deleted() -> None:
    from twilio.base.exceptions import TwilioRestException

    class _RaisingPer:
        def delete(self):
            raise TwilioRestException(status=404, uri="x", msg="not found")

    class _RaisingRec:
        deleted = []
        def list(self, date_created_before=None, limit=None):
            return [_FakeRecording("RE_old", 35)]
        def __call__(self, sid):
            return _RaisingPer()

    client = _FakeTwilioClient(_RaisingRec())
    out = StringIO()
    summary = prune_twilio_recordings(client=client, max_age_days=30, apply=True, out=out)
    assert summary["already_gone"] == 1
    assert summary["errors"] == 0


def test_prune_voicemail_rows_dry_run_removes_nothing() -> None:
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=100)).isoformat().replace("+00:00", "Z")
    fresh_ts = (now - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    rows_state = [
        ["created_at", "call_sid", "recording_sid", "caller_number_last4",
         "duration_seconds", "transcript", "playback_token_expires_at", "sms_sent_at"],
        [old_ts, "CA_old", "RE_old", "9999", 30, "", "", ""],
        [fresh_ts, "CA_new", "RE_new", "1111", 12, "hi", "", ""],
    ]
    deletes: list[int] = []

    class _FakeWriter:
        def read_voicemail_rows(self): return list(rows_state)
        def delete_voicemail_row(self, row_index): deletes.append(row_index)

    out = StringIO()
    summary = prune_voicemail_rows(
        writer=_FakeWriter(), max_age_days=90, apply=False, out=out
    )
    assert summary["would_delete"] == 1
    assert deletes == []


def test_prune_voicemail_rows_apply_deletes_old_only() -> None:
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=100)).isoformat().replace("+00:00", "Z")
    fresh_ts = (now - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    rows_state = [
        ["created_at", "call_sid", "recording_sid", "caller_number_last4",
         "duration_seconds", "transcript", "playback_token_expires_at", "sms_sent_at"],
        [old_ts, "CA_old", "RE_old", "9999", 30, "", "", ""],
        [fresh_ts, "CA_new", "RE_new", "1111", 12, "hi", "", ""],
    ]
    deletes: list[int] = []

    class _FakeWriter:
        def read_voicemail_rows(self): return list(rows_state)
        def delete_voicemail_row(self, row_index): deletes.append(row_index)

    out = StringIO()
    summary = prune_voicemail_rows(
        writer=_FakeWriter(), max_age_days=90, apply=True, out=out
    )
    assert summary["deleted"] == 1
    assert deletes == [2]


def test_audio_days_default_is_one_in_real_parser(monkeypatch, capsys) -> None:
    """Audio retention default is 1 day, not the historical 30 days.

    Drives the real argparse parser via --help and inspects the rendered
    default, so this test fails if anyone reverts the constant in
    voicemail_retention.py. The Fly scheduled retention machine runs
    daily, so in practice recordings get pruned 24-48 hours after
    creation. Design decision is in
    docs/superpowers/specs/2026-05-22-transcript-store-and-voicemail-retention-design.md
    """
    import sys

    from tools import voicemail_retention

    monkeypatch.setattr(sys, "argv", ["voicemail_retention", "--help"])
    with pytest.raises(SystemExit):
        voicemail_retention.main()
    out = capsys.readouterr().out
    # argparse renders defaults like: --audio-days AUDIO_DAYS  ... (default: 1)
    assert "(default: 1)" in out
    # Sanity: metadata default unchanged
    assert "(default: 90)" in out
