"""
Extract sales-rep names from the raw transcript corpus.

Approach: scan the first ~300 chars of every `.txt` transcript for a self-
introduction pattern ("Hello, this is X with All Green...", "My name is X.",
etc.), aggregate by name across all calls, and emit any name that appears as
the introducer in ≥`MIN_OCCURRENCES` calls. The resulting JSON is reviewed by
the user before being used as the redaction allow-list.

This pass runs over RAW (unredacted) text, but it only ever writes the
*names* into a small JSON file; no transcript content leaves the script's
memory. Names of frequent reps are not customer PII (they're employees who
introduce themselves on every call).

Usage:
    python -m tools.extract_sales_reps                          # run with defaults
    python -m tools.extract_sales_reps --min-occurrences 10
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

DEFAULT_SOURCE = Path("all green 2025 transcripts")
DEFAULT_OUTPUT = Path("tools/config/sales_rep_allowlist.json")
DEFAULT_MIN_OCCURRENCES = 5

# Patterns that capture the name of the person introducing the call. We only
# look at the first ~300 chars to avoid matching customer self-introductions
# later in the call.
_NAME_TOKEN = r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"  # "Liza" or "Liza Smith"
INTRODUCTION_PATTERNS = [
    re.compile(
        rf"(?:hello|hi|hey)[,!.\s]+(?:there[,!.\s]+)?(?:all green[,!.\s]+)?"
        rf"this is\s+{_NAME_TOKEN}\b",
        re.IGNORECASE,
    ),
    re.compile(rf"this is\s+{_NAME_TOKEN}\s+(?:here|speaking|with|at)\b", re.IGNORECASE),
    re.compile(rf"my name is\s+{_NAME_TOKEN}\b", re.IGNORECASE),
    re.compile(rf"^[\s\[\]\d:]*{_NAME_TOKEN}\s+here\s+(?:with|from|at)\s+all green", re.IGNORECASE | re.MULTILINE),
]

OPENING_WINDOW_CHARS = 400

# Words that look like names but shouldn't be (greetings/companies that the
# transcription occasionally capitalizes mid-sentence).
NAME_BLACKLIST = {
    "Hello",
    "Hi",
    "Hey",
    "Yes",
    "No",
    "Okay",
    "Sure",
    "Thanks",
    "Thank You",
    "All Green",
    "All Green Pest",
    "Pest Control",
    "Calling",
    "Al Green",
    "The Number",
    "Not Secure",
    "Our Best",
}

# Connecting words that the greedy 2-word name capture pulls in. We strip them
# from the tail of the captured name post-match.
NAME_TAIL_STOPWORDS = {
    "Here",
    "From",
    "With",
    "At",
    "Speaking",
    "And",
    "For",
    "How",
    "Is",
    "I",
    "Calling",
    "All",
}


def _strip_tail_stopwords(name: str) -> str:
    parts = name.split()
    while parts and parts[-1] in NAME_TAIL_STOPWORDS:
        parts.pop()
    return " ".join(parts)


def extract_introducer_name(text: str) -> Optional[str]:
    window = text[:OPENING_WINDOW_CHARS]
    for pattern in INTRODUCTION_PATTERNS:
        match = pattern.search(window)
        if match:
            name = _strip_tail_stopwords(match.group(1).strip().title())
            if name and name not in NAME_BLACKLIST:
                return name
    return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--min-occurrences",
        type=int,
        default=DEFAULT_MIN_OCCURRENCES,
        help=f"Minimum self-introduction count for a name to be considered a sales rep (default: {DEFAULT_MIN_OCCURRENCES}).",
    )
    parser.add_argument("--show-all", action="store_true", help="Print all candidate names with counts (not just those over the threshold).")
    args = parser.parse_args(argv)

    if not args.source.exists():
        print(f"ERROR: source directory not found: {args.source}", file=sys.stderr)
        return 2

    txt_files = sorted(args.source.glob("*.txt"))
    if not txt_files:
        print(f"No .txt files in {args.source}", file=sys.stderr)
        return 1

    counts: Counter[str] = Counter()
    miss_count = 0
    for txt in txt_files:
        try:
            text = txt.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"  skip {txt.name}: {exc}", file=sys.stderr)
            continue
        name = extract_introducer_name(text)
        if name:
            counts[name] += 1
        else:
            miss_count += 1

    if args.show_all:
        print("\nAll candidate introducer names:", file=sys.stderr)
        for name, count in counts.most_common():
            print(f"  {count:4d}  {name}", file=sys.stderr)

    allowlist = sorted(name for name, count in counts.items() if count >= args.min_occurrences)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "generated_from": str(args.source),
                "min_occurrences": args.min_occurrences,
                "names": allowlist,
                "counts": dict(counts.most_common()),
                "files_scanned": len(txt_files),
                "files_with_match": len(txt_files) - miss_count,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\nScanned {len(txt_files)} transcripts; matched introducer in {len(txt_files) - miss_count}.", file=sys.stderr)
    print(f"Names appearing in ≥{args.min_occurrences} calls (sales-rep allow-list):", file=sys.stderr)
    for name in allowlist:
        print(f"  - {name}  ({counts[name]} calls)", file=sys.stderr)
    print(f"\nWrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
