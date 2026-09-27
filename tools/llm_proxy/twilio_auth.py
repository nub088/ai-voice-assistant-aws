"""Shared helpers for Twilio webhook authentication.

Centralizes the `ALLOW_UNSIGNED_TWILIO` dev escape hatch that was previously
duplicated (and inconsistently scoped) across main.py, voice_routes.py and
voicemail.py.
"""
from __future__ import annotations

import os

from fastapi import HTTPException, Request

_TRUTHY = {"1", "true", "yes"}


def signed_webhook_url(request: Request) -> str:
    """Use the configured public URL for Twilio signatures behind an ALB."""
    if os.environ.get("AWS_ALB_PROXY") != "1":
        return str(request.url)
    public = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not public:
        raise HTTPException(status_code=500, detail="PUBLIC_BASE_URL required")
    public = "https://" + public.removeprefix("https://").removeprefix("http://")
    url = public + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    return url


def _is_production() -> bool:
    """True when running on real infrastructure, where the unsigned-Twilio dev
    escape hatch must never apply.

    Fly injects FLY_APP_NAME / FLY_MACHINE_ID into every deployment, so their
    presence reliably marks a real deploy. An explicit ENVIRONMENT / APP_ENV /
    SENTRY_ENV of "production" also counts.
    """
    if os.environ.get("FLY_APP_NAME", "").strip() or os.environ.get("FLY_MACHINE_ID", "").strip():
        return True
    return any(
        os.environ.get(var, "").strip().lower() == "production"
        for var in ("ENVIRONMENT", "APP_ENV", "SENTRY_ENV")
    )


def unsigned_twilio_allowed() -> bool:
    """Whether ALLOW_UNSIGNED_TWILIO may skip Twilio signature validation.

    The escape hatch is dev-only: it is refused in production (S4) so a flag
    accidentally left set can't let forged Twilio webhooks bypass HMAC
    signature validation.
    """
    if _is_production():
        return False
    return os.environ.get("ALLOW_UNSIGNED_TWILIO", "").strip().lower() in _TRUTHY
