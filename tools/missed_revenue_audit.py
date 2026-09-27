#!/usr/bin/env python3
"""
Missed-revenue audit for SearchKings call-tracking CSVs.

Reads one or more yearly CSVs (columns: status, calledAtDate, calledAtTime,
reportingTag, ...) and produces a markdown report covering call volume,
after-hours volume, booking rate (detected from the reportingTag summaries),
and an estimated lost-revenue figure for prospects.

Usage:
    python tools/missed_revenue_audit.py \
        --csvs "/path/to/*.csv" \
        --avg-ticket 400 \
        --output .tmp/audit/all_green_audit.md
"""
from __future__ import annotations

import argparse
import csv
import glob
import re
import sys
from collections import defaultdict
from datetime import datetime, time
from pathlib import Path

POS_BOOKED_RE = re.compile(r"\b(booked|scheduled|confirmed|books|schedules)\b", re.IGNORECASE)
TIME_ANCHOR_RE = re.compile(
    r"("
    r"\b(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\b|"
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b|"
    r"\b\d{1,2}\s*[–\-]\s*\d{1,2}\s*(?:AM|PM)\b|"
    r"\b\d{1,2}\s*(?:AM|PM)\b|"
    r"\bbetween\s+\d|"
    r"\bnext\s+(?:week|day|Mon|Tues|Wed|Thurs|Fri|Sat|Sun)|"
    r"\btomorrow\b|"
    r"\bthis\s+(?:Mon|Tues|Wed|Thurs|Fri|Sat|Sun)|"
    r"\bthe\s+\d{1,2}(?:st|nd|rd|th)\b|"
    r"\b\d{1,2}(?:st|nd|rd|th)\b"
    r")",
    re.IGNORECASE,
)
NEG_BOOKED_RE = re.compile(
    r"("
    r"\bno\s+appointment\s+(was\s+|is\s+)?(booked|scheduled|set|made)\b|"
    r"\bno\s+booking\b|"
    r"\bno\s+(appointment|service)\s+booking\b|"
    r"\bnot\s+booked\b|"
    r"\bdid(?:n[’']?t|\s+not)\s+(book|schedule)\b|"
    r"\bcould(?:n[’']?t|\s+not)\s+(book|schedule)\b|"
    r"\bwithout\s+(booking|scheduling)\b|"
    r"\bdeclined\s+to\s+(book|schedule)\b|"
    r"\bdeclined\s+booking\b|"
    r"\bbefore\s+(booking|proceeding|scheduling|deciding|committing)\b|"
    r"\bno\s+outcome\s+recorded\b|"
    r"\bcall\s+lacks\b|"
    r"\bno\s+appointment\s+discussed\b|"
    r"\bno\s+appointment\s+(yet\b|booked\s+yet)|"
    r"\bcalled\s+to\s+(reschedule|cancel|confirm)\b|"
    r"\bto\s+reschedule\s+(their|an?|the)\s+appointment\b|"
    r"\bto\s+cancel\b|"
    r"\bawaiting\s+customer\s+confirmation\b|"
    r"\b(will|plans\s+to)\s+(consult|discuss|think|callback|call\s+back|follow\s+up)\b"
    r")",
    re.IGNORECASE,
)

DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d")
TIME_FORMATS = ("%I:%M %p", "%I:%M%p", "%H:%M")


def parse_date(s: str):
    s = (s or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_time(s: str):
    s = re.sub(r"\s+(PST|PDT|EST|EDT|MST|MDT|CST|CDT|UTC).*$", "", (s or "").strip())
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    return None


def is_booked(tag: str) -> bool:
    if not tag:
        return False
    if NEG_BOOKED_RE.search(tag):
        return False
    return bool(POS_BOOKED_RE.search(tag) and TIME_ANCHOR_RE.search(tag))


def is_after_hours(t: time | None, d, biz_open: time, biz_close: time, biz_days: set[int]):
    if t is None or d is None:
        return None
    if d.weekday() not in biz_days:
        return True
    return t < biz_open or t >= biz_close


def audit(csv_paths, biz_open, biz_close, biz_days):
    yearly = defaultdict(lambda: {
        "total": 0, "answered": 0, "missed": 0, "busy": 0,
        "tagged": 0, "booked": 0,
        "ah_total": 0, "ah_answered": 0, "ah_missed": 0,
        "unknown_time": 0,
    })
    for path in csv_paths:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                d = parse_date(row.get("calledAtDate", ""))
                t = parse_time(row.get("calledAtTime", ""))
                year = d.year if d else None
                if year is None:
                    continue
                b = yearly[year]
                b["total"] += 1
                status = (row.get("status") or "").strip().lower()
                if status == "answered":
                    b["answered"] += 1
                elif status == "no answer":
                    b["missed"] += 1
                elif status == "busy":
                    b["busy"] += 1
                tag = (row.get("reportingTag") or "").strip()
                if tag:
                    b["tagged"] += 1
                    if is_booked(tag):
                        b["booked"] += 1
                ah = is_after_hours(t, d, biz_open, biz_close, biz_days)
                if ah is None:
                    b["unknown_time"] += 1
                elif ah:
                    b["ah_total"] += 1
                    if status == "answered":
                        b["ah_answered"] += 1
                    elif status in ("no answer", "busy"):
                        b["ah_missed"] += 1
    return yearly


def overall_booking_rate(yearly):
    tagged = sum(b["tagged"] for b in yearly.values())
    booked = sum(b["booked"] for b in yearly.values())
    return (booked / tagged) if tagged else 0.0


def render(yearly, avg_ticket, biz_open, biz_close, biz_days, client_name):
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    biz_str = ", ".join(day_names[d] for d in sorted(biz_days))
    out = []
    p = out.append
    p(f"# Missed-Revenue Audit, {client_name}")
    p("")
    p(f"- Avg booking value: **${avg_ticket:,.0f}**")
    p(f"- Business hours: **{biz_open.strftime('%-I:%M %p')} – {biz_close.strftime('%-I:%M %p')}** ({biz_str})")
    p("- Source: SearchKings call-tracking CSV")
    p('- "Booked" detected from `reportingTag` summary text: a booking verb (booked/scheduled/confirmed) co-occurring with a time anchor (day/date/time-window), minus explicit negations ("no appointment booked", "before deciding", "called to reschedule", etc.)')
    p("")

    p("## Call volume by year")
    p("")
    p("| Year | Total | Answered | Missed | Busy | Missed % | After-hrs | AH missed | Tagged | Booked | Booking rate |")
    p("|------|-------|----------|--------|------|----------|-----------|-----------|--------|--------|--------------|")
    totals = defaultdict(int)
    for year in sorted(yearly):
        b = yearly[year]
        miss = b["missed"] + b["busy"]
        miss_pct = 100 * miss / b["total"] if b["total"] else 0
        rate = 100 * b["booked"] / b["tagged"] if b["tagged"] else 0
        rate_str = f"{rate:.1f}%" if b["tagged"] else "n/a"
        p(
            f"| {year} | {b['total']} | {b['answered']} | {b['missed']} | {b['busy']} | "
            f"{miss_pct:.1f}% | {b['ah_total']} | {b['ah_missed']} | {b['tagged']} | {b['booked']} | {rate_str} |"
        )
        for k, v in b.items():
            totals[k] += v
    miss_all = totals["missed"] + totals["busy"]
    miss_pct_all = 100 * miss_all / totals["total"] if totals["total"] else 0
    rate_all = 100 * totals["booked"] / totals["tagged"] if totals["tagged"] else 0
    p(
        f"| **All** | **{totals['total']}** | **{totals['answered']}** | **{totals['missed']}** | **{totals['busy']}** | "
        f"**{miss_pct_all:.1f}%** | **{totals['ah_total']}** | **{totals['ah_missed']}** | "
        f"**{totals['tagged']}** | **{totals['booked']}** | **{rate_all:.1f}%** |"
    )
    p("")

    fallback_rate = overall_booking_rate(yearly)
    p("## Estimated lost revenue")
    p("")
    p(
        "Method: `missed_calls × booking_rate × avg_ticket`. "
        "Booking rate uses that year's tagged calls when available, otherwise overall average. "
        "Conservative, only counts hard misses (no-answer + busy), not abandoned-after-pickup."
    )
    p("")
    p("| Year | Missed | Booking rate used | Est. lost bookings | Est. lost revenue |")
    p("|------|--------|-------------------|--------------------|-------------------|")
    grand_lost_b = 0.0
    grand_lost_r = 0.0
    for year in sorted(yearly):
        b = yearly[year]
        miss = b["missed"] + b["busy"]
        if b["tagged"]:
            rate = b["booked"] / b["tagged"]
            note = ""
        else:
            rate = fallback_rate
            note = " *(overall avg)*"
        lost_b = miss * rate
        lost_r = lost_b * avg_ticket
        grand_lost_b += lost_b
        grand_lost_r += lost_r
        p(f"| {year} | {miss} | {rate*100:.1f}%{note} | {lost_b:.1f} | ${lost_r:,.0f} |")
    p(f"| **All** | **{miss_all}** | - | **{grand_lost_b:.1f}** | **${grand_lost_r:,.0f}** |")
    p("")

    p("## After-hours analysis")
    p("")
    p(
        "Every after-hours answered call is one a human receptionist physically can't take, "
        "and every after-hours missed call is a 100%-net-new lead an AI agent would have captured."
    )
    p("")
    ah_total = totals["ah_total"]
    ah_missed = totals["ah_missed"]
    ah_pct = 100 * ah_total / totals["total"] if totals["total"] else 0
    p(f"- After-hours calls (all years): **{ah_total}** ({ah_pct:.1f}% of all calls)")
    p(f"- After-hours missed (all years): **{ah_missed}**")
    if fallback_rate:
        recovery = ah_missed * fallback_rate * avg_ticket
        p(f"- After-hours-missed lost revenue at {fallback_rate*100:.1f}% booking rate: **${recovery:,.0f}**")
    p("")

    p("## Sales line")
    p("")
    if grand_lost_r:
        p(
            f"> Across {totals['total']:,} calls, {client_name} missed an estimated "
            f"**{grand_lost_b:.0f} bookings** worth roughly **${grand_lost_r:,.0f}** "
            f"because no one picked up. {ah_total:,} of those calls came in after hours, "
            f"a window an AI receptionist covers automatically."
        )
    p("")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csvs", nargs="+", required=True, help="CSV paths or globs.")
    ap.add_argument("--avg-ticket", type=float, default=400.0, help="Avg booking value (default $400).")
    ap.add_argument("--biz-open", default="08:00")
    ap.add_argument("--biz-close", default="18:00")
    ap.add_argument("--biz-days", default="0,1,2,3,4,5", help="0=Mon..6=Sun (default Mon-Sat).")
    ap.add_argument("--client", default="Client", help="Client name for the report header.")
    ap.add_argument("--output", help="Write markdown report to this path.")
    args = ap.parse_args(argv)

    paths = []
    for pat in args.csvs:
        m = glob.glob(pat)
        paths.extend(m if m else [pat])
    paths = [p for p in paths if Path(p).is_file()]
    if not paths:
        ap.error("No CSV files matched.")

    biz_open = datetime.strptime(args.biz_open, "%H:%M").time()
    biz_close = datetime.strptime(args.biz_close, "%H:%M").time()
    biz_days = {int(x) for x in args.biz_days.split(",") if x.strip()}

    yearly = audit(paths, biz_open, biz_close, biz_days)
    report = render(yearly, args.avg_ticket, biz_open, biz_close, biz_days, args.client)
    print(report)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"\nWrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
