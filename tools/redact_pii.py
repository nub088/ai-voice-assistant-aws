"""
PII redaction for call transcripts.

Pipeline:
  1. Presidio (regex + spaCy NER), names, phone numbers, emails, locations
  2. Optional Claude Haiku contextual pass, catches contextual PII the NER misses
  3. Audit pass, flags any residual PII-shaped tokens (10+ digit runs, emails, etc.)

Filenames are also redacted via a non-reversible HMAC-SHA256 truncated to 12 hex chars,
prefixed with `call_`. Same input filename → same hash, so paired .json/.txt files stay paired.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field

from presidio_analyzer import AnalyzerEngine, RecognizerResult
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

DEFAULT_ENTITIES = [
    "PERSON",
    "PHONE_NUMBER",
    "EMAIL_ADDRESS",
    "LOCATION",
    "CREDIT_CARD",
    "IP_ADDRESS",
    "URL",
    "US_SSN",
]

# Patterns the audit pass flags as suspicious residuals.
_AUDIT_PATTERNS = {
    "long_number_run": re.compile(r"\b\d{7,}\b"),
    "phone_like": re.compile(r"\b\d{3}[\s\-.]?\d{3}[\s\-.]?\d{4}\b"),
    "email_like": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "ca_postal_code": re.compile(r"\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b"),
    "us_zip": re.compile(r"\b\d{5}(?:-\d{4})?\b"),
}

# A receptionist reading a phone number aloud digit-by-digit looks like a
# sequence of short numeric tokens close together. Detect when, within any
# 200-character window, the digits across small numeric tokens sum to ≥10, # that's a phone number being spelled out. The audit flags it; the
# contextual pass (or manual review) does the actual redaction.
_SHORT_NUMERIC_TOKEN = re.compile(r"\b\d{1,5}\b")
_PHONE_SPELLED_OUT_WINDOW = 200
_PHONE_SPELLED_OUT_MIN_DIGITS = 10

# Whisper segment timestamps. We mask these before NER so Presidio doesn't
# misclassify them (e.g., `00:00:29` as a PERSON), then restore them after.
_TIMESTAMP_RE = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?\]")
_TIMESTAMP_MASK_PREFIX = "TS"  # private-use Unicode area, won't collide with text

# Source-file metadata header in the .txt files. Replace with the hashed name
# rather than letting Presidio chew on the raw filename.
_SOURCE_LINE_RE = re.compile(r"^Source:\s*(.+)$", re.MULTILINE)

# Mirror of presidio-analyzer's bundled conf/default.yaml `ner_model_configuration`.
# A zero-config `AnalyzerEngine()` loads this implicitly; because we now pass an
# explicit nlp_engine (to pin the spaCy model), we must supply it ourselves, # otherwise we silently lose the spaCy->Presidio entity mapping (e.g.
# FAC->LOCATION, which catches street addresses) and the labels_to_ignore list.
_NER_MODEL_CONFIGURATION = {
    "model_to_presidio_entity_mapping": {
        "PER": "PERSON",
        "PERSON": "PERSON",
        "NORP": "NRP",
        "FAC": "LOCATION",
        "LOC": "LOCATION",
        "GPE": "LOCATION",
        "LOCATION": "LOCATION",
        "ORG": "ORGANIZATION",
        "ORGANIZATION": "ORGANIZATION",
        "DATE": "DATE_TIME",
        "TIME": "DATE_TIME",
    },
    "low_confidence_score_multiplier": 0.4,
    "low_score_entity_names": [],
    "labels_to_ignore": [
        "ORGANIZATION",
        "CARDINAL",
        "EVENT",
        "LANGUAGE",
        "LAW",
        "MONEY",
        "ORDINAL",
        "PERCENT",
        "PRODUCT",
        "QUANTITY",
        "WORK_OF_ART",
    ],
}


@dataclass
class RedactionResult:
    redacted_text: str
    counts: dict[str, int] = field(default_factory=dict)
    residuals: list[dict] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.residuals


def mask_residuals(
    text: str, residuals: list[dict], placeholder: str = "[REDACTED]"
) -> str:
    """Scrub audit-detected residual PII out of already-redacted text.

    The audit pass (`_audit`) only *detects* PII-shaped tokens the NER and
    contextual passes missed, it never removes them, so `redacted_text` can
    still contain raw phone numbers, postal codes, etc. flagged in
    `residuals`. Anything persisted to the durable store or written to logs
    must go through this first, replacing every occurrence of each residual's
    matched text with `placeholder`. No-op when `residuals` is empty.
    """
    for residual in residuals:
        raw = residual.get("text", "")
        if raw:
            text = text.replace(raw, placeholder)
    return text


class PIIRedactor:
    def __init__(
        self,
        hmac_key: bytes,
        *,
        use_contextual_pass: bool = True,
        anthropic_client=None,
        spacy_model: str = "en_core_web_sm",
        redact_special_categories: bool = False,
    ):
        if not hmac_key or len(hmac_key) < 16:
            raise ValueError("hmac_key must be at least 16 bytes")
        self._hmac_key = hmac_key
        self._spacy_model = spacy_model
        # Wire `spacy_model` into the analyzer's NLP engine explicitly. Without
        # this, AnalyzerEngine() falls back to Presidio's bundled default
        # (en_core_web_lg), which the prod image does NOT install, construction
        # then raises OSError and transcript persistence is silently disabled.
        # Default is en_core_web_sm to match the Dockerfile and stay within the
        # 1 GB box's RAM budget (lg is ~600 MB-1 GB resident).
        nlp_engine = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": spacy_model}],
                # Replicate Presidio's bundled default.yaml NER mapping. The
                # zero-config AnalyzerEngine() we used to call loaded this
                # implicitly; once we pass an explicit nlp_engine we must carry
                # it ourselves or we lose mappings like FAC->LOCATION (street
                # addresses) and the labels_to_ignore list.
                "ner_model_configuration": _NER_MODEL_CONFIGURATION,
            }
        ).create_engine()
        self._analyzer = AnalyzerEngine(
            nlp_engine=nlp_engine, default_score_threshold=0.4
        )
        self._anonymizer = AnonymizerEngine()
        self._use_contextual_pass = use_contextual_pass
        self._anthropic = anthropic_client
        # When True, the contextual pass also looks for GDPR Article 9
        # special-category data (health, religion, ethnicity, sexual
        # orientation, political opinion, trade-union membership, biometric
        # data, genetic data) and replaces it with category placeholders.
        # Requires anthropic_client; ignored when the contextual pass is off.
        self._redact_special_categories = redact_special_categories

    def hash_filename(self, filename: str) -> str:
        """Map a source filename to a stable, non-reversible redacted name.

        The HMAC is keyed (`hmac_key`), so an attacker who only sees the redacted
        filename cannot recover the source via brute force without the key.
        """
        stem, _, ext = filename.rpartition(".")
        if not stem:
            stem, ext = filename, ""
        digest = hmac.new(self._hmac_key, stem.encode("utf-8"), hashlib.sha256).hexdigest()
        suffix = f".{ext}" if ext else ""
        return f"call_{digest[:12]}{suffix}"

    def redact_text(self, text: str) -> RedactionResult:
        """Run the three-pass redaction pipeline on a transcript body."""
        # Pre-process: replace `Source: <filename>` with the hashed name and
        # mask Whisper timestamps so Presidio doesn't misclassify them.
        preprocessed = self._rewrite_source_line(text)
        masked, timestamp_map = self._mask_timestamps(preprocessed)

        # Pass 1: Presidio
        analyzer_results: list[RecognizerResult] = self._analyzer.analyze(
            text=masked, entities=DEFAULT_ENTITIES, language="en"
        )
        # A weak NER model (e.g. en_core_web_sm) sometimes mislabels a
        # timestamp sentinel as PERSON/LOCATION, which would replace it with a
        # placeholder and make _restore_timestamps unable to put the timestamp
        # back. Drop any result overlapping a sentinel so timestamps are never
        # anonymized, regardless of the model. Model-independent guarantee.
        analyzer_results = self._drop_sentinel_overlaps(
            analyzer_results, masked, timestamp_map
        )
        operators = self._build_operators()
        anonymized = self._anonymizer.anonymize(
            text=masked, analyzer_results=analyzer_results, operators=operators
        )
        redacted = self._restore_timestamps(anonymized.text, timestamp_map)

        # Pass 2: contextual pass (optional)
        if self._use_contextual_pass and self._anthropic is not None:
            redacted = self._contextual_pass(redacted)

        # Pass 3: audit
        residuals = self._audit(redacted)

        counts = self._count_placeholders(redacted)
        return RedactionResult(redacted_text=redacted, counts=counts, residuals=residuals)

    # ------------------------------------------------------------------ internals

    def _build_operators(self) -> dict[str, OperatorConfig]:
        # Per-entity replacement strategy. Names get an indexed placeholder so the
        # same person stays consistent within a transcript; everything else is a
        # fixed token that preserves type without leaking ordering.
        return {
            "DEFAULT": OperatorConfig("replace", {"new_value": "[REDACTED]"}),
            "PERSON": OperatorConfig("replace", {"new_value": "[NAME]"}),
            "PHONE_NUMBER": OperatorConfig("replace", {"new_value": "[PHONE]"}),
            "EMAIL_ADDRESS": OperatorConfig("replace", {"new_value": "[EMAIL]"}),
            "LOCATION": OperatorConfig("replace", {"new_value": "[LOCATION]"}),
            "CREDIT_CARD": OperatorConfig("replace", {"new_value": "[CC]"}),
            "IP_ADDRESS": OperatorConfig("replace", {"new_value": "[IP]"}),
            "URL": OperatorConfig("replace", {"new_value": "[URL]"}),
            "US_SSN": OperatorConfig("replace", {"new_value": "[SSN]"}),
        }

    def _contextual_pass(self, text: str) -> str:
        """Ask Claude Haiku to find any residual PII the Presidio pass missed.

        Haiku is given the partially-redacted text and asked to return the raw
        substrings that still look like PII. We replace those locally; Haiku is
        not asked to produce the final text (so we keep deterministic control of
        the placeholder format).

        When `redact_special_categories=True`, the prompt additionally asks
        the auditor to surface GDPR Article 9 special-category data so we can
        replace it with category-typed placeholders ([HEALTH], [RELIGION],
        [ETHNICITY], [SEXUAL_ORIENTATION], [POLITICAL], [UNION], [BIOMETRIC],
        [GENETIC]).
        """
        article9_clause = (
            "\n- GDPR Article 9 special-category data: health conditions, "
            "allergies, medications, disabilities, religion, ethnicity or "
            "national origin, sexual orientation, political views, trade-union "
            "membership, biometric identifiers, genetic information"
        ) if self._redact_special_categories else ""
        article9_categories = (
            "|HEALTH|RELIGION|ETHNICITY|SEXUAL_ORIENTATION|POLITICAL|UNION|BIOMETRIC|GENETIC"
            if self._redact_special_categories else ""
        )

        prompt = (
            "You are a strict PII auditor. The text below has already had obvious "
            "PII (names tagged by NER, phone numbers, emails) replaced with "
            "placeholders like [NAME], [PHONE], [EMAIL], [LOCATION]. Your job is "
            "to find any REMAINING PII that the first pass missed.\n\n"
            "Look specifically for:\n"
            "- People's first or last names that weren't replaced\n"
            "- Street addresses or building numbers (e.g., '123 Main St')\n"
            "- Postal codes (Canadian or US)\n"
            "- Phone numbers in unusual formats or read aloud digit by digit\n"
            "- Specific business names if used to identify the caller"
            f"{article9_clause}\n\n"
            "Return a JSON object: {\"findings\": [{\"text\": \"<exact substring>\", "
            f"\"category\": \"NAME|ADDRESS|POSTAL|PHONE{article9_categories}|OTHER\"}}, ...]}}. "
            "If nothing is found, return {\"findings\": []}. "
            "Output ONLY the JSON, no prose.\n\n"
            "TEXT:\n"
            f"{text}"
        )

        try:
            resp = self._anthropic.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            payload = resp.content[0].text.strip()
            # Strip markdown fences if Haiku adds them despite instructions.
            payload = re.sub(r"^```(?:json)?\s*|\s*```$", "", payload, flags=re.MULTILINE)
            findings = json.loads(payload).get("findings", [])
        except Exception:
            # Contextual pass is best-effort. Fall back to what Presidio produced.
            return text

        result = text
        for finding in findings:
            raw = finding.get("text", "").strip()
            category = finding.get("category", "OTHER").upper()
            if not raw or len(raw) < 2:
                continue
            placeholder = {
                "NAME": "[NAME]",
                "ADDRESS": "[ADDRESS]",
                "POSTAL": "[POSTAL]",
                "PHONE": "[PHONE]",
                "HEALTH": "[HEALTH]",
                "RELIGION": "[RELIGION]",
                "ETHNICITY": "[ETHNICITY]",
                "SEXUAL_ORIENTATION": "[SEXUAL_ORIENTATION]",
                "POLITICAL": "[POLITICAL]",
                "UNION": "[UNION]",
                "BIOMETRIC": "[BIOMETRIC]",
                "GENETIC": "[GENETIC]",
            }.get(category, "[REDACTED]")
            result = result.replace(raw, placeholder)
        return result

    def _audit(self, text: str) -> list[dict]:
        residuals: list[dict] = []
        for label, pattern in _AUDIT_PATTERNS.items():
            for match in pattern.finditer(text):
                # Skip if the match is inside a placeholder bracket.
                start = match.start()
                end = match.end()
                window = text[max(0, start - 1) : min(len(text), end + 1)]
                if "[" in window and "]" in window:
                    continue
                residuals.append({"label": label, "text": match.group(), "offset": start})

        # Spelled-out phone-number detection: scan for clusters of short digit
        # tokens whose digit-counts sum to ≥10 within a 200-char window. We
        # walk through the token list once and skip past each detected cluster
        # so a long call doesn't produce hundreds of overlapping findings.
        # Strip Whisper timestamps first, their digits are not PII and would
        # otherwise dominate the cluster-density signal.
        text_for_phone_audit = _TIMESTAMP_RE.sub("", text)
        tokens = list(_SHORT_NUMERIC_TOKEN.finditer(text_for_phone_audit))
        i = 0
        while i < len(tokens):
            anchor = tokens[i]
            digit_sum = len(anchor.group())
            window_end = anchor.end() + _PHONE_SPELLED_OUT_WINDOW
            cluster_end_idx = i
            for j in range(i + 1, len(tokens)):
                if tokens[j].start() > window_end:
                    break
                digit_sum += len(tokens[j].group())
                cluster_end_idx = j
                if digit_sum >= _PHONE_SPELLED_OUT_MIN_DIGITS:
                    snippet = text_for_phone_audit[anchor.start() : tokens[j].end()]
                    residuals.append(
                        {
                            "label": "phone_spelled_out",
                            "text": snippet,
                            "offset": anchor.start(),
                        }
                    )
                    break
            # Advance past this cluster so we don't re-anchor on every token in it.
            i = cluster_end_idx + 1
        return residuals

    @staticmethod
    def _count_placeholders(text: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for match in re.finditer(r"\[([A-Z_]+)\]", text):
            key = match.group(1)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _rewrite_source_line(self, text: str) -> str:
        """Rewrite any `Source: <filename>` header to the hashed equivalent.

        The .txt files emitted by the upstream transcription tool start with a
        `Source: 3274877133_Jan_..._604__270-1401.mp3` line that embeds the
        caller's phone number. We replace it deterministically rather than
        relying on Presidio. We also strip the file extension so a `.mp3`
        suffix doesn't trip the downstream URL detector.
        """

        def _sub(match: re.Match[str]) -> str:
            hashed = self.hash_filename(match.group(1).strip())
            stem = hashed.rsplit(".", 1)[0]
            return f"Source: {stem}"

        return _SOURCE_LINE_RE.sub(_sub, text)

    @staticmethod
    def _drop_sentinel_overlaps(
        results: list[RecognizerResult],
        masked: str,
        timestamp_map: dict[str, str],
    ) -> list[RecognizerResult]:
        """Drop analyzer results whose span overlaps a timestamp sentinel token.

        Sentinels stand in for `[HH:MM:SS]` markers, which are never PII; a
        result touching one is a misclassification (the model grabbed the
        opaque token, often with an adjacent word). Removing it keeps the
        sentinel intact so `_restore_timestamps` can put the timestamp back.
        """
        if not timestamp_map:
            return results
        spans: list[tuple[int, int]] = []
        for token in timestamp_map:
            start = masked.find(token)
            if start >= 0:
                spans.append((start, start + len(token)))
        if not spans:
            return results
        return [
            r
            for r in results
            if not any(r.start < end and r.end > start for start, end in spans)
        ]

    def _mask_timestamps(self, text: str) -> tuple[str, dict[str, str]]:
        """Replace `[HH:MM:SS]` markers with non-PII tokens before NER.

        Returns the masked text and a map from mask token → original timestamp,
        so we can restore them after redaction.
        """
        mapping: dict[str, str] = {}

        def _sub(match: re.Match[str]) -> str:
            token = f"⁣{_TIMESTAMP_MASK_PREFIX}{len(mapping):05d}⁣"
            mapping[token] = match.group(0)
            return token

        return _TIMESTAMP_RE.sub(_sub, text), mapping

    @staticmethod
    def _restore_timestamps(text: str, mapping: dict[str, str]) -> str:
        for token, original in mapping.items():
            text = text.replace(token, original)
        return text


def load_default_redactor(
    *,
    use_contextual_pass: bool = True,
    redact_special_categories: bool = False,
) -> PIIRedactor:
    """Construct a redactor from environment variables.

    Required: REDACTION_HMAC_KEY (32-byte hex string, see .env.example).
    Optional: ANTHROPIC_API_KEY (enables the contextual pass).

    Set `redact_special_categories=True` (or REDACT_SPECIAL_CATEGORIES=1 in
    the environment) for EU-client transcripts where GDPR Article 9 data
    must be replaced with category placeholders before retention.
    """
    hmac_hex = os.environ.get("REDACTION_HMAC_KEY", "").strip()
    if not hmac_hex:
        raise RuntimeError(
            "REDACTION_HMAC_KEY is not set. Generate one with: "
            "python -c 'import secrets; print(secrets.token_hex(32))'"
        )
    try:
        hmac_key = bytes.fromhex(hmac_hex)
    except ValueError as exc:
        raise RuntimeError("REDACTION_HMAC_KEY must be hex-encoded") from exc

    anthropic_client = None
    if use_contextual_pass and os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic

        anthropic_client = anthropic.Anthropic()

    env_flag = os.environ.get("REDACT_SPECIAL_CATEGORIES", "").strip().lower() in (
        "1", "true", "yes",
    )
    return PIIRedactor(
        hmac_key=hmac_key,
        use_contextual_pass=use_contextual_pass and anthropic_client is not None,
        anthropic_client=anthropic_client,
        redact_special_categories=redact_special_categories or env_flag,
    )
