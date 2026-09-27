"""Tests for HMAC-signed voicemail playback URLs."""
from __future__ import annotations

import time

import pytest

from tools.llm_proxy.voicemail_urls import (
    InvalidToken,
    sign_playback_url,
    verify_playback_token,
)


SECRET = b"\x00" * 32


def test_sign_and_verify_roundtrip() -> None:
    expires = int(time.time()) + 3600
    url = sign_playback_url(
        base_url="https://example.fly.dev",
        recording_sid="REabc123",
        expires_epoch=expires,
        secret=SECRET,
    )
    assert url.startswith("https://example.fly.dev/vm/REabc123?")
    qs = url.split("?", 1)[1]
    params = dict(p.split("=", 1) for p in qs.split("&"))
    assert int(params["exp"]) == expires
    assert verify_playback_token(
        recording_sid="REabc123",
        exp=int(params["exp"]),
        sig=params["sig"],
        secret=SECRET,
    ) is None


def test_expired_token_rejected() -> None:
    past = int(time.time()) - 1
    url = sign_playback_url(
        base_url="https://example.fly.dev",
        recording_sid="REabc123",
        expires_epoch=past,
        secret=SECRET,
    )
    qs = url.split("?", 1)[1]
    params = dict(p.split("=", 1) for p in qs.split("&"))
    with pytest.raises(InvalidToken, match="expired"):
        verify_playback_token(
            recording_sid="REabc123",
            exp=int(params["exp"]),
            sig=params["sig"],
            secret=SECRET,
        )


def test_tampered_signature_rejected() -> None:
    expires = int(time.time()) + 3600
    url = sign_playback_url(
        base_url="https://example.fly.dev",
        recording_sid="REabc123",
        expires_epoch=expires,
        secret=SECRET,
    )
    qs = url.split("?", 1)[1]
    params = dict(p.split("=", 1) for p in qs.split("&"))
    bad_sig = params["sig"][:-2] + ("aa" if not params["sig"].endswith("aa") else "bb")
    with pytest.raises(InvalidToken, match="signature"):
        verify_playback_token(
            recording_sid="REabc123",
            exp=int(params["exp"]),
            sig=bad_sig,
            secret=SECRET,
        )


def test_signature_bound_to_recording_sid() -> None:
    expires = int(time.time()) + 3600
    url = sign_playback_url(
        base_url="https://example.fly.dev",
        recording_sid="REabc123",
        expires_epoch=expires,
        secret=SECRET,
    )
    qs = url.split("?", 1)[1]
    params = dict(p.split("=", 1) for p in qs.split("&"))
    with pytest.raises(InvalidToken, match="signature"):
        verify_playback_token(
            recording_sid="REother",
            exp=int(params["exp"]),
            sig=params["sig"],
            secret=SECRET,
        )


def test_signature_bound_to_secret() -> None:
    expires = int(time.time()) + 3600
    url = sign_playback_url(
        base_url="https://example.fly.dev",
        recording_sid="REabc123",
        expires_epoch=expires,
        secret=SECRET,
    )
    qs = url.split("?", 1)[1]
    params = dict(p.split("=", 1) for p in qs.split("&"))
    with pytest.raises(InvalidToken, match="signature"):
        verify_playback_token(
            recording_sid="REabc123",
            exp=int(params["exp"]),
            sig=params["sig"],
            secret=b"\x01" * 32,
        )
