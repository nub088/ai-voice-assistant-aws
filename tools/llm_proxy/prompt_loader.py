"""
Loads the system prompt + pest taxonomy from disk and composes them into a
single string. Hashes the composed result so we can verify cache stability and
log which prompt version a call used.

Designed so the prompt is loaded once at proxy startup and cached in-process, the file watch / hot reload story is deliberately omitted (restart the proxy
to pick up changes; calls in flight finish on the old version).

Branding/hours/timezone are interpolated from a `ClientConfig` so the same
prompt file can serve multiple clients (different business name, AI name,
industry, hours).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .client_config import ClientConfig, interpolate, load_client_config

DEFAULT_PROMPT_PATH = Path("tools/prompts/receptionist_v0.3_data_driven.md")
DEFAULT_TAXONOMY_PATH = Path("tools/prompts/pest_taxonomy.json")

# Compliance prefix files, file-driven so legal text changes don't require
# a code deploy and the LoadedPrompt.version hash captures any change.
DEFAULT_GDPR_PREFIX_PATH = Path("tools/prompts/gdpr_prefix.md")
DEFAULT_PIPA_PREFIX_PATH = Path("tools/prompts/pipa_prefix.md")


def time_context(config: ClientConfig) -> str:
    """Single-line time note injected as its own context message so the model
    knows whether the office is open or closed at call time. Uses the
    client's configured timezone + weekday/hour window.

    Deliberately hour-granular (P5): this line sits at the Anthropic
    prompt-cache lookup boundary right after the big static system block, so
    every distinct rendering forces a fresh cache write. At hour granularity
    all calls within the same hour share a prefix and get cache reads; minute
    precision would make every call a miss for zero behavioral benefit (the
    model only needs day + approximate hour + open/closed status)."""
    now = datetime.now(config.hours.timezone)
    day_name = now.strftime("%A")
    tz_abbrev = now.strftime("%Z") or "local"
    hour_str = now.strftime("%I %p").lstrip("0")
    is_business = (
        now.weekday() in config.hours.weekdays
        and config.hours.open_hour <= now.hour < config.hours.close_hour
    )
    hours_status = "business hours" if is_business else "after hours"
    return f"[Current time: {day_name} around {hour_str} {tz_abbrev}, {hours_status}]"


@dataclass(frozen=True)
class LoadedPrompt:
    text: str
    version: str  # short hash, e.g. "v1-7af3c2"
    prompt_path: Path
    taxonomy_path: Path
    config: ClientConfig
    # Compliance prefixes, interpolated against `config`. Always loaded;
    # voice_pipeline._compose_system_prompt picks the right one based on
    # config.gdpr_mode / config.pipa_mode. Empty string if the prefix file
    # is missing (prefix files are optional for environments that don't
    # ship them, e.g. minimal test fixtures).
    gdpr_prefix: str = ""
    pipa_prefix: str = ""


def _load_prefix_if_present(path: Path, config: ClientConfig) -> str:
    """Read a prefix file and interpolate placeholders. Return "" if missing."""
    if not path.exists():
        return ""
    raw = path.read_text(encoding="utf-8")
    return interpolate(raw, config)


def load_prompt(
    config: ClientConfig | None = None,
    prompt_path: Path | None = None,
    taxonomy_path: Path | None = None,
    gdpr_prefix_path: Path | None = None,
    pipa_prefix_path: Path | None = None,
) -> LoadedPrompt:
    """Load the prompt + taxonomy and interpolate `{{placeholders}}` from config.

    `config` defaults to the canonical Clear Slope config. `prompt_path` and
    `taxonomy_path` default to the paths in the config; explicit overrides
    are useful for tests.

    The GDPR and PIPA compliance prefixes are also loaded (file-driven) and
    folded into the version hash so any legal text change forces a new
    version label, even for clients not currently in that mode.
    """
    if config is None:
        config = load_client_config()
    prompt_path = prompt_path or config.prompt.path
    taxonomy_path = taxonomy_path or config.prompt.taxonomy_path
    gdpr_prefix_path = gdpr_prefix_path or DEFAULT_GDPR_PREFIX_PATH
    pipa_prefix_path = pipa_prefix_path or DEFAULT_PIPA_PREFIX_PATH

    prompt_md = prompt_path.read_text(encoding="utf-8")
    taxonomy_json = taxonomy_path.read_text(encoding="utf-8")
    taxonomy = json.loads(taxonomy_json)
    pretty_taxonomy = json.dumps(taxonomy, indent=2, ensure_ascii=False)

    raw_composed = f"{prompt_md.rstrip()}\n\n```json\n{pretty_taxonomy}\n```\n"
    composed = interpolate(raw_composed, config)

    gdpr_prefix = _load_prefix_if_present(gdpr_prefix_path, config)
    pipa_prefix = _load_prefix_if_present(pipa_prefix_path, config)

    # Hash all four sources so legal text changes (or a tenant rename that
    # alters the rendered prefix) produce a distinct version label.
    hasher = hashlib.sha256()
    hasher.update(composed.encode("utf-8"))
    hasher.update(b"\x00gdpr_prefix\x00")
    hasher.update(gdpr_prefix.encode("utf-8"))
    hasher.update(b"\x00pipa_prefix\x00")
    hasher.update(pipa_prefix.encode("utf-8"))
    digest = hasher.hexdigest()[:6]

    base = prompt_path.stem
    version_tag = base.rsplit("_", 1)[-1] if "_" in base else "v0"
    return LoadedPrompt(
        text=composed,
        version=f"{version_tag}-{digest}",
        prompt_path=prompt_path,
        taxonomy_path=taxonomy_path,
        config=config,
        gdpr_prefix=gdpr_prefix,
        pipa_prefix=pipa_prefix,
    )
