"""CLI that publishes the weekly call-intake digest.

Reads the live booking sheet, computes a rolling-window digest (default
last 7 days), prints the rendered text to stdout, and appends a summary
row to the `WeeklyDigest` tab in the same spreadsheet so the proprietor
sees the trend without leaving Sheets.

Run:
    python -m tools.llm_proxy.digest_cli
    python -m tools.llm_proxy.digest_cli --days 30
    python -m tools.llm_proxy.digest_cli --dry-run

`--dry-run` skips the WeeklyDigest write, useful for sanity-checking the
sheet credentials and seeing what would be published.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from typing import Protocol

from dotenv import load_dotenv

from .digest import (
    DIGEST_HEADER_ROW,
    DIGEST_TAB_NAME,
    compute_digest,
    parse_rows,
    render_digest_row,
    render_digest_text,
)
from .sheets_writer import writer_from_env

logger = logging.getLogger("digest_cli")


class _DigestSheets(Protocol):
    def read_all_rows(self) -> list[list[object]]: ...
    def ensure_tab(self, tab_name: str, header: list[object]) -> None: ...
    def append_to_tab(self, tab_name: str, row: list[object]) -> None: ...


def run(
    writer: _DigestSheets,
    *,
    days: int = 7,
    now: datetime | None = None,
    dry_run: bool = False,
) -> str:
    """Compute the digest, optionally publish it, and return the rendered text.

    Pulled out of `main()` so tests (and any future scheduler harness) can
    drive it with a fake sheets client.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    values = writer.read_all_rows()
    rows = parse_rows(values)
    digest = compute_digest(rows, now=now, days=days)
    text = render_digest_text(digest)
    if not dry_run:
        writer.ensure_tab(DIGEST_TAB_NAME, list(DIGEST_HEADER_ROW))
        writer.append_to_tab(DIGEST_TAB_NAME, render_digest_row(digest))
        logger.info(
            "published digest period_end=%s total_calls=%d",
            digest.period_end.isoformat(),
            digest.total_calls,
        )
    return text


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Generate weekly call-intake digest.")
    parser.add_argument("--days", type=int, default=7, help="Window size in days (default 7).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print only; do not write to the WeeklyDigest tab.",
    )
    args = parser.parse_args(argv)

    writer = writer_from_env()
    if writer is None:
        print(
            "ERROR: Sheets writer not configured. Set GOOGLE_SHEET_ID and "
            "GOOGLE_SERVICE_ACCOUNT_FILE in the environment.",
            file=sys.stderr,
        )
        return 2

    try:
        text = run(writer, days=args.days, dry_run=args.dry_run)
    except Exception:
        logger.exception("digest run failed")
        return 1

    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
