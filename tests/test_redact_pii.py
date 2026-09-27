"""Tests for tools/redact_pii.py.

Run from project root: `pytest tests/test_redact_pii.py -v`

The contextual pass (Claude Haiku) is disabled in these tests so they can run
without ANTHROPIC_API_KEY. The Presidio + audit passes are exercised directly.
"""
from __future__ import annotations

import secrets

import pytest

from tools.redact_pii import PIIRedactor


@pytest.fixture(scope="module")
def redactor() -> PIIRedactor:
    return PIIRedactor(
        hmac_key=bytes.fromhex(secrets.token_hex(32)),
        use_contextual_pass=False,
    )


def _loaded_model_name(redactor: PIIRedactor) -> str:
    """Return the full name (e.g. 'en_core_web_sm') of the spaCy model the
    redactor's Presidio analyzer actually loaded."""
    nlp = redactor._analyzer.nlp_engine.nlp["en"]  # noqa: SLF001
    return f"{nlp.meta['lang']}_{nlp.meta['name']}"


def test_redactor_uses_configured_spacy_model() -> None:
    """The redactor must wire its `spacy_model` arg into Presidio's NLP engine
    and actually load THAT model. Before this fix the arg was stored but never
    used, so Presidio silently loaded its hardcoded default (en_core_web_lg),     which the prod Dockerfile doesn't install, so construction blew up in prod."""
    r = PIIRedactor(
        hmac_key=bytes.fromhex(secrets.token_hex(32)),
        use_contextual_pass=False,
        spacy_model="en_core_web_sm",
    )
    assert _loaded_model_name(r) == "en_core_web_sm"


def test_redactor_defaults_to_small_model_matching_dockerfile() -> None:
    """Default model must be the one the prod image installs (en_core_web_sm),
    so the redactor constructs in prod instead of throwing on a missing lg."""
    r = PIIRedactor(
        hmac_key=bytes.fromhex(secrets.token_hex(32)),
        use_contextual_pass=False,
    )
    assert _loaded_model_name(r) == "en_core_web_sm"


def test_redacts_phone_numbers(redactor: PIIRedactor) -> None:
    text = "Call me at 604-555-0123 or 778.555.9876 anytime."
    result = redactor.redact_text(text)
    assert "604" not in result.redacted_text
    assert "778" not in result.redacted_text
    assert "[PHONE]" in result.redacted_text
    assert result.is_clean, f"Residuals leaked through: {result.residuals}"


def test_redacts_email(redactor: PIIRedactor) -> None:
    text = "Email me at john.smith@example.com about the wasps."
    result = redactor.redact_text(text)
    assert "@example.com" not in result.redacted_text
    assert "[EMAIL]" in result.redacted_text
    assert result.is_clean


def test_redacts_person_names(redactor: PIIRedactor) -> None:
    text = "Hello, this is Liza here with Clear Slope Pest Control speaking with John Smith."
    result = redactor.redact_text(text)
    assert "Liza" not in result.redacted_text
    assert "John Smith" not in result.redacted_text
    assert "[NAME]" in result.redacted_text


def test_audit_flags_residual_phone(redactor: PIIRedactor) -> None:
    # A 10-digit run that Presidio failed to catch (e.g., "six oh four" written as digits without separators).
    text = "My number is 6045550123 thanks."
    result = redactor.redact_text(text)
    if "6045550123" in result.redacted_text:
        assert any(r["label"] in {"long_number_run", "phone_like"} for r in result.residuals)


def test_audit_flags_canadian_postal(redactor: PIIRedactor) -> None:
    text = "I'm at unit 304, postal code V6B 1A1."
    result = redactor.redact_text(text)
    if "V6B 1A1" in result.redacted_text or "V6B1A1" in result.redacted_text:
        assert any(r["label"] == "ca_postal_code" for r in result.residuals)


def test_filename_hash_is_stable(redactor: PIIRedactor) -> None:
    name = "3274877133_Jan_6__2025__604__270-1401.txt"
    a = redactor.hash_filename(name)
    b = redactor.hash_filename(name)
    assert a == b
    assert a.startswith("call_")
    assert a.endswith(".txt")
    assert "604" not in a
    assert "270-1401" not in a


def test_filename_hash_preserves_extension(redactor: PIIRedactor) -> None:
    assert redactor.hash_filename("foo.json").endswith(".json")
    assert redactor.hash_filename("bar.txt").endswith(".txt")
    assert "." not in redactor.hash_filename("noext")[5:]  # only the "call_" prefix


def test_filename_hash_distinguishes_inputs(redactor: PIIRedactor) -> None:
    a = redactor.hash_filename("a.txt")
    b = redactor.hash_filename("b.txt")
    assert a != b


def test_different_keys_produce_different_hashes() -> None:
    name = "sample.txt"
    r1 = PIIRedactor(hmac_key=bytes.fromhex(secrets.token_hex(32)), use_contextual_pass=False)
    r2 = PIIRedactor(hmac_key=bytes.fromhex(secrets.token_hex(32)), use_contextual_pass=False)
    assert r1.hash_filename(name) != r2.hash_filename(name)


def test_rejects_short_hmac_key() -> None:
    with pytest.raises(ValueError):
        PIIRedactor(hmac_key=b"too short", use_contextual_pass=False)


def test_full_call_transcript_redaction(redactor: PIIRedactor) -> None:
    """Round-trip a realistic transcript fragment and assert all expected PII is gone."""
    text = (
        "Hello, this is Liza here with Clear Slope Pest Control. "
        "Hi, this is John Smith calling. I have wasps at 1234 Main Street, Vancouver. "
        "My phone number is 604-555-0123 and you can email me at john@example.com."
    )
    result = redactor.redact_text(text)
    redacted = result.redacted_text

    forbidden = ["Liza", "John Smith", "604-555-0123", "john@example.com", "1234 Main Street"]
    for token in forbidden:
        assert token not in redacted, f"PII '{token}' leaked into redacted output: {redacted}"

    # Counts should be populated.
    assert sum(result.counts.values()) > 0


def test_timestamps_preserved(redactor: PIIRedactor) -> None:
    """Whisper timestamps must not be NER-classified as people."""
    text = "[00:00:29] Got it.\n[00:00:30] You said it's on Commercial Drive?"
    result = redactor.redact_text(text)
    redacted = result.redacted_text
    assert "[00:00:29]" in redacted
    assert "[00:00:30]" in redacted
    # And no `[[NAME]]`-style artefact from a misclassified timestamp.
    assert "[[NAME]]" not in redacted


def test_source_line_rewritten_to_hashed_name(redactor: PIIRedactor) -> None:
    """The `Source:` header in transcript .txt files must be replaced with
    the hashed filename so phone-number-bearing names don't leak."""
    text = (
        "Source: 3270241011_Jan_2__2025__672__558-5259.mp3\n"
        "============================================================\n"
        "Hello, this is the receptionist."
    )
    result = redactor.redact_text(text)
    redacted = result.redacted_text
    assert "3270241011" not in redacted
    assert "558-5259" not in redacted
    assert ".mp3" not in redacted  # cleaned up via hash
    assert "Source: call_" in redacted


def test_audit_flags_spelled_out_phone(redactor: PIIRedactor) -> None:
    """A receptionist reading a phone number aloud digit-by-digit produces a
    cluster of short numeric tokens. The audit pass must flag this as a
    `phone_spelled_out` residual so the call is held back from training data."""
    text = (
        "All right. 604? 604? 873? 873? 7011. 7011. Got it, thanks."
    )
    result = redactor.redact_text(text)
    labels = {r["label"] for r in result.residuals}
    assert "phone_spelled_out" in labels, (
        f"Spelled-out phone number was not flagged. Residuals: {result.residuals}"
    )


def test_audit_does_not_flag_innocuous_short_numbers(redactor: PIIRedactor) -> None:
    """Two small numbers in unrelated context should not trip the spelled-out detector."""
    text = "I have 2 wasps and 3 termites. Maybe 5 in total."
    result = redactor.redact_text(text)
    labels = {r["label"] for r in result.residuals}
    assert "phone_spelled_out" not in labels


# --- Residual masking (audit finding S1) -----------------------------------

def test_mask_residuals_replaces_every_occurrence_of_residual_text() -> None:
    """The audit pass only DETECTS residual PII; mask_residuals must scrub it
    out of the text before it is ever persisted or logged."""
    from tools.redact_pii import mask_residuals

    text = "Caller: my cell is 6042701401, again 6042701401 thanks"
    residuals = [{"label": "long_number_run", "text": "6042701401", "offset": 19}]
    out = mask_residuals(text, residuals)
    assert "6042701401" not in out
    assert out.count("[REDACTED]") == 2


def test_mask_residuals_is_noop_without_residuals() -> None:
    from tools.redact_pii import mask_residuals

    text = "Caller: I have mice in the [LOCATION]. Reach me at [PHONE]."
    assert mask_residuals(text, []) == text
