"""
Retention enforcement, delete files past their policy lifetime.

Per `docs/gdpr-gap-analysis.md` §4.3, retention used to be aspirational ("90
days for transcripts"). This script makes it real: it scans a set of paths,
removes files whose mtime is older than the configured age, and prints a
structured JSON summary so the run can be archived as audit evidence.

Designed to be safe by default: `--dry-run` is implied unless `--apply` is
passed explicitly. Files older than the cutoff are listed but not removed
otherwise. No directories are removed, only files.

Usage:
    # Show what would be deleted (default, no destruction):
    python tools/retention_enforcer.py

    # Actually delete:
    python tools/retention_enforcer.py --apply

    # Override the default policy:
    python tools/retention_enforcer.py --apply \\
        --rule .tmp:14 \\
        --rule eval/labeled_calls:90

Designed to run as a Fly scheduled task once per day for EU client
deployments. Logs to stdout as JSON, one object per file, plus a summary
object at the end.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("retention_enforcer")

# Default rules: (path relative to project root, max age in days).
# A path that doesn't exist is silently skipped, adding a new client doesn't
# require a config change here, but a real EU deployment should override these.
DEFAULT_RULES: list[tuple[str, int]] = [
    (".tmp", 30),                              # ephemeral processing files
    ("eval/labeled_calls", 90),                # quality-review transcripts
    ("eval/results/top_agent_transcripts", 90),  # top-N agent samples
]


@dataclass
class Rule:
    path: Path
    max_age_days: int

    @property
    def cutoff_epoch(self) -> float:
        return time.time() - (self.max_age_days * 86400)


def parse_rule_spec(spec: str) -> Rule:
    """Parse a `path:days` CLI rule into a Rule."""
    if ":" not in spec:
        raise argparse.ArgumentTypeError(
            f"--rule must be PATH:DAYS, got {spec!r}"
        )
    path_str, _, days_str = spec.rpartition(":")
    try:
        days = int(days_str)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--rule days must be an integer, got {days_str!r}"
        ) from exc
    if days < 0:
        raise argparse.ArgumentTypeError("--rule days must be non-negative")
    return Rule(path=Path(path_str), max_age_days=days)


def iter_files(root: Path) -> Iterable[Path]:
    """Yield every regular file under `root`. Skips symlinks for safety."""
    if not root.exists():
        return
    if root.is_file():
        yield root
        return
    for entry in root.rglob("*"):
        # Don't follow symlinks to avoid escaping the intended directory.
        if entry.is_symlink():
            continue
        if entry.is_file():
            yield entry


def run(rules: list[Rule], *, apply: bool) -> dict:
    """Walk each rule's path and process expired files.

    Prints one JSON object per matched file to stdout. Returns a summary
    dict containing per-rule and overall totals (also printed at the end).
    """
    per_rule_summary: list[dict] = []
    total_files_inspected = 0
    total_files_expired = 0
    total_files_removed = 0
    total_bytes_freed = 0
    errors: list[dict] = []

    for rule in rules:
        if not rule.path.exists():
            per_rule_summary.append(
                {
                    "path": str(rule.path),
                    "max_age_days": rule.max_age_days,
                    "status": "skipped, path does not exist",
                    "files_inspected": 0,
                    "files_expired": 0,
                    "files_removed": 0,
                    "bytes_freed": 0,
                }
            )
            continue

        rule_files_inspected = 0
        rule_files_expired = 0
        rule_files_removed = 0
        rule_bytes_freed = 0

        for path in iter_files(rule.path):
            rule_files_inspected += 1
            total_files_inspected += 1
            try:
                stat = path.stat()
            except OSError as exc:
                errors.append({"path": str(path), "error": f"stat: {exc}"})
                continue

            if stat.st_mtime >= rule.cutoff_epoch:
                continue  # still within retention window

            rule_files_expired += 1
            total_files_expired += 1

            event = {
                "path": str(path),
                "mtime_epoch": int(stat.st_mtime),
                "age_days": round((time.time() - stat.st_mtime) / 86400, 1),
                "size_bytes": stat.st_size,
                "rule_path": str(rule.path),
                "max_age_days": rule.max_age_days,
                "action": "delete" if apply else "would-delete",
            }

            if apply:
                try:
                    path.unlink()
                except OSError as exc:
                    event["action"] = "error"
                    event["error"] = f"unlink: {exc}"
                    errors.append(event)
                else:
                    rule_files_removed += 1
                    rule_bytes_freed += stat.st_size
                    total_files_removed += 1
                    total_bytes_freed += stat.st_size

            print(json.dumps(event, sort_keys=True), flush=True)

        per_rule_summary.append(
            {
                "path": str(rule.path),
                "max_age_days": rule.max_age_days,
                "status": "applied" if apply else "dry-run",
                "files_inspected": rule_files_inspected,
                "files_expired": rule_files_expired,
                "files_removed": rule_files_removed,
                "bytes_freed": rule_bytes_freed,
            }
        )

    return {
        "summary": {
            "mode": "apply" if apply else "dry-run",
            "files_inspected": total_files_inspected,
            "files_expired": total_files_expired,
            "files_removed": total_files_removed,
            "bytes_freed": total_bytes_freed,
            "errors": errors,
            "per_rule": per_rule_summary,
        }
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Delete files past their retention policy. Dry-run by default."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete files. Without this flag, only logs what would be deleted.",
    )
    parser.add_argument(
        "--rule",
        type=parse_rule_spec,
        action="append",
        metavar="PATH:DAYS",
        help=(
            "Retention rule. May be passed multiple times. If not given, the "
            "built-in defaults apply (.tmp:30, eval/labeled_calls:90, "
            "eval/results/top_agent_transcripts:90)."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="Project root to resolve relative rule paths against (default: cwd)",
    )
    args = parser.parse_args(argv)

    rules_raw = args.rule or [Rule(Path(p), d) for p, d in DEFAULT_RULES]
    # Resolve each rule's path against --root so the script can be run from any cwd.
    rules = [Rule(args.root / r.path, r.max_age_days) for r in rules_raw]

    result = run(rules, apply=args.apply)
    print(json.dumps(result, sort_keys=True, indent=2), flush=True)

    # Non-zero exit on errors so cron / CI catches them.
    return 1 if result["summary"]["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
