"""HMAC-signed playback URLs for voicemail recordings.

The URL embedded in the owner SMS must let the owner play the recording
in any browser without exposing Twilio credentials. We host a proxy route
at `/vm/{recording_sid}` and authenticate the request with a token in the
query string:

    https://<app>/vm/RE<sid>?exp=<epoch>&sig=<base64url-hmac>

`sig` = base64url( HMAC-SHA256(secret, f"{recording_sid}:{exp}") ).
Verification recomputes the HMAC and compares with constant-time
equality, then checks expiry. No DB lookup needed.

`secret` comes from the `VOICEMAIL_URL_SECRET` env var (random 32-byte
per deployment). The service refuses to start without it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time


class InvalidToken(Exception):
    """Raised when a playback token fails HMAC or expiry checks."""


def _signature(recording_sid: str, exp: int, secret: bytes) -> str:
    msg = f"{recording_sid}:{exp}".encode("utf-8")
    digest = hmac.new(secret, msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def sign_playback_url(
    *,
    base_url: str,
    recording_sid: str,
    expires_epoch: int,
    secret: bytes,
) -> str:
    """Build the absolute playback URL with `exp` + `sig` query params."""
    sig = _signature(recording_sid, expires_epoch, secret)
    base = base_url.rstrip("/")
    return f"{base}/vm/{recording_sid}?exp={expires_epoch}&sig={sig}"


def verify_playback_token(
    *,
    recording_sid: str,
    exp: int,
    sig: str,
    secret: bytes,
    now: int | None = None,
) -> None:
    """Validate a playback token. Raises `InvalidToken` on any failure.

    `now` is injectable for tests; defaults to `time.time()`.
    """
    expected = _signature(recording_sid, exp, secret)
    if not hmac.compare_digest(expected, sig):
        raise InvalidToken("bad signature")
    current = int(time.time() if now is None else now)
    if current > exp:
        raise InvalidToken("token expired")
