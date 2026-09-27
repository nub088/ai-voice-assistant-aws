"""
Ingest the existing transcript corpus.

For each `.txt` / `.json` pair in the source directory:
  1. Redact text bodies (Presidio + optional Haiku contextual pass + audit).
  2. Classify the call: voicemail-only vs. handled-inbound. (Finer-grained
     classification, outbound, out-of-scope, agent identity, happens later in
     `analyze_agents.py`.)
  3. Write redacted outputs with HMAC-hashed filenames to `<dest>/handled/`,
     `<dest>/voicemails/`, plus a per-file `_redaction.json` sidecar with
     placeholder counts and any residual audit warnings.
  4. Append an entry to a manifest mapping hashed name → source filename + class.

The manifest contains source filenames (which embed phone numbers as PII), so
it is written outside the redacted-output tree and is gitignored.

Usage:
    python -m tools.ingest_transcripts --dry-run --limit 20
    python -m tools.ingest_transcripts                        # process all
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from tools.redact_pii import PIIRedactor, RedactionResult, load_default_redactor

DEFAULT_SOURCE = Path("all green 2025 transcripts")
DEFAULT_DEST = Path("eval/labeled_calls")
DEFAULT_MANIFEST = Path(".tmp/ingestion_manifest.json")

VOICEMAIL_PHRASES = (
    "outside of our office hours",
    "outside our office hours",
    "we'll call you back as soon as possible",
    "leave a message",
    "leave us a message",
    "our office hours are",
)


@dataclass
class CallClassification:
    label: str  # "voicemail" | "handled"
    reason: str
    word_count: int


@dataclass
class ManifestEntry:
    source_filename: str
    hashed_name: str
    classification: str
    word_count: int
    redaction_counts: dict[str, int]
    residual_count: int
    residual_labels: list[str]
    ingested_at: str


def classify_call(text: str) -> CallClassification:
    """Heuristic classification.

    Rationale: most after-hours calls in the corpus are short voicemail bumps
    (caller hits the greeting and hangs up). Handled calls have multi-turn
    conversation. We use word count + greeting-phrase match as a fast filter;
    a more accurate LLM classifier runs in `analyze_agents.py` later.
    """
    word_count = len(text.split())
    lowered = text.lower()
    has_voicemail_phrase = any(phrase in lowered for phrase in VOICEMAIL_PHRASES)

    if word_count < 80 and has_voicemail_phrase:
        return CallClassification("voicemail", "short + voicemail-greeting phrase", word_count)
    if word_count < 30:
        return CallClassification("voicemail", "very short call", word_count)
    return CallClassification("handled", "multi-turn conversation", word_count)


def redact_json_payload(raw_json: dict, redactor: PIIRedactor) -> tuple[dict, list[RedactionResult]]:
    """Redact every text-bearing field in the Whisper JSON.

    `transcription` is the full transcript; `segments[*].text` are timestamped
    chunks. `source_file` embeds the phone number, we replace it with the
    hashed name to keep the JSON self-describing without leaking PII.
    """
    results: list[RedactionResult] = []
    new_payload = dict(raw_json)

    full_text = raw_json.get("transcription", "") or ""
    if full_text:
        result = redactor.redact_text(full_text)
        results.append(result)
        new_payload["transcription"] = result.redacted_text

    new_segments = []
    for segment in raw_json.get("segments", []) or []:
        seg_text = segment.get("text", "") or ""
        if seg_text:
            result = redactor.redact_text(seg_text)
            results.append(result)
            new_seg = dict(segment)
            new_seg["text"] = result.redacted_text
            new_segments.append(new_seg)
        else:
            new_segments.append(segment)
    if "segments" in new_payload:
        new_payload["segments"] = new_segments

    if "source_file" in new_payload:
        original = new_payload["source_file"]
        new_payload["source_file"] = redactor.hash_filename(original)

    return new_payload, results


def aggregate_results(results: list[RedactionResult]) -> tuple[dict[str, int], list[dict]]:
    counts: dict[str, int] = {}
    residuals: list[dict] = []
    for result in results:
        for key, value in result.counts.items():
            counts[key] = counts.get(key, 0) + value
        residuals.extend(result.residuals)
    return counts, residuals


def process_pair(
    txt_path: Path,
    json_path: Path,
    redactor: PIIRedactor,
    dest_root: Path,
    *,
    dry_run: bool,
) -> ManifestEntry:
    text_body = txt_path.read_text(encoding="utf-8")
    json_payload = json.loads(json_path.read_text(encoding="utf-8"))

    txt_result = redactor.redact_text(text_body)
    redacted_json, json_results = redact_json_payload(json_payload, redactor)

    # Classification uses the redacted full transcript; placeholders don't affect
    # length-based heuristics meaningfully and we never want to read raw text
    # twice when we don't have to.
    classification = classify_call(redacted_json.get("transcription", "") or txt_result.redacted_text)

    counts, residuals = aggregate_results([txt_result] + json_results)
    hashed_stem = redactor.hash_filename(txt_path.name).rsplit(".", 1)[0]

    if not dry_run:
        bucket = "voicemails" if classification.label == "voicemail" else "handled"
        out_dir = dest_root / bucket
        out_dir.mkdir(parents=True, exist_ok=True)

        (out_dir / f"{hashed_stem}.txt").write_text(txt_result.redacted_text, encoding="utf-8")
        (out_dir / f"{hashed_stem}.json").write_text(
            json.dumps(redacted_json, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (out_dir / f"{hashed_stem}_redaction.json").write_text(
            json.dumps(
                {
                    "counts": counts,
                    "residuals": residuals,
                    "classification": asdict(classification),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    return ManifestEntry(
        source_filename=txt_path.name,
        hashed_name=hashed_stem,
        classification=classification.label,
        word_count=classification.word_count,
        redaction_counts=counts,
        residual_count=len(residuals),
        residual_labels=sorted({r["label"] for r in residuals}),
        ingested_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )


def find_pairs(source: Path) -> list[tuple[Path, Path]]:
    txt_files = sorted(source.glob("*.txt"))
    pairs: list[tuple[Path, Path]] = []
    for txt in txt_files:
        json_path = txt.with_suffix(".json")
        if json_path.exists():
            pairs.append((txt, json_path))
    return pairs


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest the transcript corpus.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N pairs.")
    parser.add_argument("--dry-run", action="store_true", help="Process but do not write outputs.")
    parser.add_argument(
        "--no-contextual",
        action="store_true",
        help="Skip the Claude Haiku contextual pass even if ANTHROPIC_API_KEY is set.",
    )
    args = parser.parse_args(argv)

    load_dotenv()

    if not args.source.exists():
        print(f"ERROR: source directory not found: {args.source}", file=sys.stderr)
        return 2

    pairs = find_pairs(args.source)
    if args.limit:
        pairs = pairs[: args.limit]
    if not pairs:
        print(f"No .txt/.json pairs found in {args.source}", file=sys.stderr)
        return 1

    redactor = load_default_redactor(use_contextual_pass=not args.no_contextual)
    contextual_state = "ON" if redactor._use_contextual_pass else "OFF"  # noqa: SLF001
    print(
        f"Ingesting {len(pairs)} pairs from {args.source} "
        f"→ {args.dest} (dry-run={args.dry_run}, contextual_pass={contextual_state})",
        file=sys.stderr,
    )

    entries: list[ManifestEntry] = []
    counts_by_class = {"voicemail": 0, "handled": 0}
    flagged: list[ManifestEntry] = []

    for i, (txt_path, json_path) in enumerate(pairs, 1):
        try:
            entry = process_pair(txt_path, json_path, redactor, args.dest, dry_run=args.dry_run)
        except Exception as exc:
            print(f"  [{i}/{len(pairs)}] FAILED {txt_path.name}: {exc}", file=sys.stderr)
            continue
        entries.append(entry)
        counts_by_class[entry.classification] = counts_by_class.get(entry.classification, 0) + 1
        if entry.residual_count > 0:
            flagged.append(entry)
        if i % 50 == 0 or i == len(pairs):
            print(f"  [{i}/{len(pairs)}] processed", file=sys.stderr)

    if not args.dry_run:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(
            json.dumps([asdict(e) for e in entries], indent=2),
            encoding="utf-8",
        )

    print("\n=== Ingestion summary ===", file=sys.stderr)
    print(f"  total processed: {len(entries)}", file=sys.stderr)
    for label, count in counts_by_class.items():
        print(f"  {label}: {count}", file=sys.stderr)
    print(f"  files with residual audit warnings: {len(flagged)}", file=sys.stderr)
    if flagged[:5]:
        print("  first few flagged:", file=sys.stderr)
        for entry in flagged[:5]:
            print(
                f"    - {entry.hashed_name} "
                f"(residuals: {entry.residual_count}, labels: {entry.residual_labels})",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
