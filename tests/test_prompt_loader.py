"""Smoke tests for prompt_loader."""
from __future__ import annotations

from tools.llm_proxy.prompt_loader import load_prompt


def test_load_prompt_returns_composed_text_and_version() -> None:
    loaded = load_prompt()
    assert loaded.text, "composed prompt should be non-empty"
    assert "Clear Slope Pest Control" in loaded.text
    assert "AI" in loaded.text  # the self-identification rule
    assert "log_request" in loaded.text  # the active tool the prompt teaches
    assert '"common_name": "German cockroach"' in loaded.text  # taxonomy got embedded
    # Version is `<filename-suffix>-<hash>`, e.g. "driven-bd41c2" for
    # receptionist_v0.3_data_driven.md.
    assert "-" in loaded.version
    assert len(loaded.version.split("-")[-1]) == 6  # hex digest is 6 chars


def test_load_prompt_is_deterministic() -> None:
    a = load_prompt()
    b = load_prompt()
    assert a.text == b.text
    assert a.version == b.version


def test_load_prompt_loads_gdpr_prefix_with_interpolation() -> None:
    """The GDPR compliance prefix lives in tools/prompts/gdpr_prefix.md, not
    as a hardcoded Python constant. It must be loaded and interpolated against
    the client config so legal text changes don't require a code deploy and
    the LoadedPrompt.version hash captures any change to it.
    """
    from dataclasses import replace

    from tools.llm_proxy.client_config import BusinessIdentity, load_client_config

    cfg = load_client_config()
    loaded = load_prompt(config=cfg)
    assert loaded.gdpr_prefix, "gdpr_prefix should be loaded and non-empty"
    assert "GDPR" in loaded.gdpr_prefix
    assert "Article 22" in loaded.gdpr_prefix
    # Placeholders must be substituted.
    assert "{{ai_name}}" not in loaded.gdpr_prefix
    assert "{{business_short_name}}" not in loaded.gdpr_prefix
    assert cfg.business.ai_name in loaded.gdpr_prefix
    assert cfg.business.short_name in loaded.gdpr_prefix

    # A different tenant gets their own names in the rendered prefix.
    other = replace(
        cfg,
        business=BusinessIdentity(
            name="Bob's Plumbing",
            short_name="Bob's",
            industry="residential plumbing",
            ai_name="Mia",
        ),
    )
    other_loaded = load_prompt(config=other)
    assert "Mia" in other_loaded.gdpr_prefix
    assert "Bob's" in other_loaded.gdpr_prefix
    # And the version hash differs because the rendered prefix differs.
    assert loaded.version != other_loaded.version


def test_load_prompt_loads_pipa_prefix() -> None:
    """PIPA prefix is the BC-mode override block, also file-driven."""
    loaded = load_prompt()
    assert loaded.pipa_prefix, "pipa_prefix should be loaded and non-empty"
    assert "PIPA" in loaded.pipa_prefix
    assert "set_recording_preference" in loaded.pipa_prefix


def test_load_prompt_interpolates_business_branding_from_config() -> None:
    """A second tenant should get their own name in the rendered prompt."""
    from dataclasses import replace

    from tools.llm_proxy.client_config import (
        BusinessIdentity,
        load_client_config,
    )

    base = load_client_config()
    other = replace(
        base,
        business=BusinessIdentity(
            name="Bob's Plumbing",
            short_name="Bob's",
            industry="residential plumbing",
            ai_name="Mia",
        ),
    )
    rendered = load_prompt(config=other)
    assert "Bob's Plumbing" in rendered.text
    assert "Mia" in rendered.text
    assert "Clear Slope" not in rendered.text
    assert "Sarah" not in rendered.text
    # No template tokens should leak through.
    assert "{{business_name}}" not in rendered.text
    assert "{{ai_name}}" not in rendered.text
