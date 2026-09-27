"""Static checks for container hardening (audit finding S9).

These assert the Dockerfile / entrypoint are structured to run the app as a
non-root user with a runtime privilege drop. They do NOT build or run the
image, that requires a container build and MUST be validated on staging
before deploy (does the app user write /data/transcripts.db, does gosu exist
on the base image, do calls still connect).
"""
from __future__ import annotations

from pathlib import Path

DOCKERFILE = Path("infra/fly/Dockerfile")
ENTRYPOINT = Path("infra/fly/entrypoint.sh")


def test_dockerfile_creates_unprivileged_user() -> None:
    text = DOCKERFILE.read_text()
    assert "useradd" in text or "adduser" in text, "no unprivileged user created"


def test_dockerfile_installs_gosu_for_privilege_drop() -> None:
    text = DOCKERFILE.read_text()
    assert "gosu" in text, "gosu not installed (needed to drop privileges at runtime)"


def test_dockerfile_does_not_pin_user_root() -> None:
    text = DOCKERFILE.read_text()
    assert "USER root" not in text, "image must not run the app as root"


def test_entrypoint_drops_privileges_via_gosu() -> None:
    text = ENTRYPOINT.read_text()
    assert "gosu" in text, "entrypoint must drop privileges via gosu"


def test_entrypoint_chowns_data_volume_for_app_user() -> None:
    """The Fly volume at /data is created root-owned; the entrypoint must chown
    it (as root, before dropping) or the non-root app can't write the DB."""
    text = ENTRYPOINT.read_text()
    assert "chown" in text and "/data" in text, "entrypoint must chown /data for the app user"


def test_entrypoint_still_launches_uvicorn() -> None:
    text = ENTRYPOINT.read_text()
    assert "uvicorn tools.llm_proxy.main:app" in text


# --- multi-stage build (audit finding S17) ------------------------------------

def test_dockerfile_is_multi_stage() -> None:
    """S17: a builder stage compiles wheels; the runtime stage must not carry
    the toolchain."""
    text = DOCKERFILE.read_text()
    froms = [ln for ln in text.splitlines() if ln.strip().upper().startswith("FROM ")]
    assert len(froms) >= 2, "expected a multi-stage build (builder + runtime)"


def test_runtime_stage_has_no_build_toolchain() -> None:
    """S17: build-essential must only exist in the builder stage, a compiler
    in the production image lets a foothold build exploits in place."""
    text = DOCKERFILE.read_text()
    runtime = text.split("FROM ")[-1]
    assert "build-essential" not in runtime, "no compiler in the runtime stage"


def test_image_installs_from_hash_pinned_lockfile() -> None:
    """S16: the image must install from the hash-pinned lock so a compromised
    or surprise upstream release can't slip into a deploy."""
    text = DOCKERFILE.read_text()
    assert "requirements.lock" in text
    assert "--require-hashes" in text
