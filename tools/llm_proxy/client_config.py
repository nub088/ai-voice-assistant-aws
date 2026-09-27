"""Per-client configuration loader.

Single source of truth for branding, hours, and per-tenant prompt/taxonomy
paths. The prompt loader and SMS notifier read from a `ClientConfig` so the
same Pipecat code path can serve different clients by pointing at a
different JSON file via `CLIENT_CONFIG_PATH`.

Schema lives in `config/clients/<client_id>.json`. See
`config/clients/clearslope.json` for the canonical example.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .prompt_loader import LoadedPrompt
    from .sheets_writer import SheetsWriter
    from .sms_notifier import SmsNotifier

DEFAULT_CONFIG_PATH = Path("config/clients/clearslope.json")


@dataclass(frozen=True)
class BusinessIdentity:
    name: str           # full legal/branded name, e.g. "Clear Slope Pest Control"
    short_name: str     # casual sign-off form, e.g. "Clear Slope"
    industry: str       # one-line description, e.g. "pest control and wildlife exclusion"
    ai_name: str        # the receptionist persona, e.g. "Sarah"


@dataclass(frozen=True)
class BusinessHours:
    timezone: ZoneInfo
    weekdays: frozenset[int]   # 0=Mon ... 6=Sun
    open_hour: int             # inclusive, 24-hour
    close_hour: int            # exclusive, 24-hour


@dataclass(frozen=True)
class PromptPaths:
    path: Path
    taxonomy_path: Path


@dataclass(frozen=True)
class ClientConfig:
    client_id: str
    twilio_number: str      # E.164 inbound number; routing key for POST /twilio/voice (D-01)
    env_prefix: str         # e.g. CLEARSLOPE or ALLGREEN; scopes per-client env vars (D-04)
    minute_ceiling: int | None  # max minutes per restart; None = unlimited (D-07, D-08)
    business: BusinessIdentity
    hours: BusinessHours
    prompt: PromptPaths
    # When True, the voice pipeline injects an EU/UK/Swiss GDPR-mode prefix into
    # the system prompt (opt-in recording, guaranteed human handoff, special-
    # category data restraint) and exposes the `set_recording_preference` and
    # `human_handoff` tools to the model. See docs/gdpr-gap-analysis.md.
    gdpr_mode: bool = False
    # When True, the voice pipeline injects a BC PIPA-mode prefix into the
    # system prompt and uses a hardcoded TTS greeting (uninterruptable) with
    # an opt-out for transcript retention. Mutually exclusive with gdpr_mode.
    # See docs/superpowers/specs/2026-05-21-bc-pipa-compliance-design.md.
    pipa_mode: bool = False
    # When False, the client is a template / not-yet-provisioned tenant: still
    # registered so /twilio/voice tests don't 404, but excluded from /healthz
    # readiness checks (missing Sheets/SMS wiring is expected, not a failure).
    live: bool = True


@dataclass
class ClientContext:
    """Runtime bundle assembled at startup for one client (D-03)."""

    config: ClientConfig
    loaded_prompt: LoadedPrompt
    sheets_writer: SheetsWriter | None
    sms_notifier: SmsNotifier | None
    auth_token: str  # global TWILIO_AUTH_TOKEN; passed to signature validator per call (D-12)


def load_client_config(path: Path = DEFAULT_CONFIG_PATH) -> ClientConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    biz = raw["business"]
    hrs = raw["hours"]
    prm = raw["prompt"]
    ceiling = raw.get("minute_ceiling")
    gdpr_mode = bool(raw.get("gdpr_mode", False))
    pipa_mode = bool(raw.get("pipa_mode", False))
    if gdpr_mode and pipa_mode:
        raise ValueError(
            f"config {path}: gdpr_mode and pipa_mode are mutually exclusive; "
            f"pick one based on caller jurisdiction"
        )
    return ClientConfig(
        client_id=raw["client_id"],
        twilio_number=raw["twilio_number"],
        env_prefix=raw["env_prefix"],
        minute_ceiling=int(ceiling) if ceiling is not None else None,
        business=BusinessIdentity(
            name=biz["name"],
            short_name=biz["short_name"],
            industry=biz["industry"],
            ai_name=biz["ai_name"],
        ),
        hours=BusinessHours(
            timezone=ZoneInfo(hrs["timezone"]),
            weekdays=frozenset(int(d) for d in hrs["weekdays"]),
            open_hour=int(hrs["open_hour"]),
            close_hour=int(hrs["close_hour"]),
        ),
        prompt=PromptPaths(
            path=Path(prm["path"]),
            taxonomy_path=Path(prm["taxonomy_path"]),
        ),
        gdpr_mode=gdpr_mode,
        pipa_mode=pipa_mode,
        live=bool(raw.get("live", True)),
    )


def scan_client_configs(config_dir: Path) -> dict[str, ClientConfig]:
    """Return dict[twilio_number, ClientConfig] for every *.json in config_dir."""
    result: dict[str, ClientConfig] = {}
    for path in sorted(config_dir.glob("*.json")):
        cfg = load_client_config(path)
        if cfg.twilio_number in result:
            raise ValueError(
                f"duplicate twilio_number {cfg.twilio_number!r}: "
                f"{cfg.client_id} and {result[cfg.twilio_number].client_id}"
            )
        result[cfg.twilio_number] = cfg
    return result


def interpolate(template: str, config: ClientConfig) -> str:
    """Replace `{{placeholder}}` tokens with values from the client config.

    Placeholders supported: business_name, business_short_name, industry,
    ai_name. Unknown placeholders are left in place so they're easy to spot
    on inspection.
    """
    return (
        template
        .replace("{{business_name}}", config.business.name)
        .replace("{{business_short_name}}", config.business.short_name)
        .replace("{{industry}}", config.business.industry)
        .replace("{{ai_name}}", config.business.ai_name)
    )
