"""
Text REPL for the All Green receptionist agent.

You type as the caller; the agent (Claude) replies. The script watches the
agent's output for `<log_request>{...}</log_request>` and `<end_call/>` tags.
On `<log_request>`, the JSON payload is appended as a row to a local CSV.
On `<end_call/>`, the script prints a summary and exits.

The full conversation transcript is saved to .tmp/transcripts/<timestamp>.txt
for review.

This is the v0 prototype, no Vapi, no Twilio, no Google Sheets, no phone.
The point is to iterate on the system prompt fast before wiring any of that.

Usage:
    python tools/text_chat_with_agent.py
    python tools/text_chat_with_agent.py --model claude-sonnet-4-6
    python tools/text_chat_with_agent.py --prompt tools/prompts/receptionist_v0.2.md
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROMPT = PROJECT_ROOT / "tools" / "prompts" / "receptionist_v0.1.md"
LOG_CSV = PROJECT_ROOT / ".tmp" / "receptionist_test_log.csv"
TRANSCRIPT_DIR = PROJECT_ROOT / ".tmp" / "transcripts"

CSV_FIELDS = [
    "timestamp",
    "session_id",
    "caller_name",
    "callback_phone",
    "address",
    "reported_pest",
    "identified_pest",
    "preferred_date",
    "preferred_time_window",
    "notes",
]

LOG_REQUEST_RE = re.compile(r"<log_request>\s*(\{.*?\})\s*</log_request>", re.DOTALL)
END_CALL_RE = re.compile(r"<end_call\s*/?>", re.IGNORECASE)


def strip_tags(text: str) -> str:
    """Remove log_request blocks and end_call tags from text shown to the caller."""
    text = LOG_REQUEST_RE.sub("", text)
    text = END_CALL_RE.sub("", text)
    return text.strip()


def append_row(payload: dict, session_id: str) -> None:
    LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
    write_header = not LOG_CSV.exists()
    row = {
        "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "session_id": session_id,
        **{k: payload.get(k, "") for k in CSV_FIELDS if k not in {"timestamp", "session_id"}},
    }
    with LOG_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def save_transcript(session_id: str, lines: list[str]) -> Path:
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"{session_id}.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT, help="Path to system prompt markdown.")
    parser.add_argument("--model", default="claude-haiku-4-5", help="Anthropic model ID.")
    parser.add_argument("--max-tokens", type=int, default=1024)
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set in .env", file=sys.stderr)
        return 1

    if not args.prompt.exists():
        print(f"ERROR: prompt file not found: {args.prompt}", file=sys.stderr)
        return 1

    system_prompt = args.prompt.read_text(encoding="utf-8")
    client = Anthropic()
    session_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    transcript: list[str] = [f"# Session {session_id}", f"# Model: {args.model}", f"# Prompt: {args.prompt}", ""]

    print(f"Session {session_id}, model {args.model}")
    print("Type as the caller. Ctrl-C to abort.\n")

    messages: list[dict] = []

    # Kick off: agent speaks first (greeting). Send an empty user "ring" turn.
    messages.append({"role": "user", "content": "[the phone rings, caller has just connected]"})

    while True:
        response = client.messages.create(
            model=args.model,
            max_tokens=args.max_tokens,
            system=system_prompt,
            messages=messages,
        )
        agent_text = "".join(block.text for block in response.content if block.type == "text")
        spoken = strip_tags(agent_text)
        if spoken:
            print(f"AGENT: {spoken}\n")
        transcript.append(f"AGENT: {agent_text}")
        messages.append({"role": "assistant", "content": agent_text})

        # Log any structured payload.
        for match in LOG_REQUEST_RE.finditer(agent_text):
            try:
                payload = json.loads(match.group(1))
            except json.JSONDecodeError as e:
                print(f"[!] log_request JSON did not parse: {e}", file=sys.stderr)
                continue
            append_row(payload, session_id)
            print(f"[logged to {LOG_CSV.name}]\n")

        if END_CALL_RE.search(agent_text):
            transcript_path = save_transcript(session_id, transcript)
            print(f"[call ended, transcript: {transcript_path}]")
            return 0

        try:
            user_text = input("CALLER: ").strip()
        except (EOFError, KeyboardInterrupt):
            transcript.append("CALLER: [aborted by user]")
            save_transcript(session_id, transcript)
            print("\n[aborted]")
            return 130

        if not user_text:
            continue
        transcript.append(f"CALLER: {user_text}")
        messages.append({"role": "user", "content": user_text})


if __name__ == "__main__":
    sys.exit(main())
