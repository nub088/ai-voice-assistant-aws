"""
Replay a sequence of caller utterances through the production prompt + LLM
without a live Twilio call. Useful for catching prompt regressions.

Input file is JSON:

    {
      "label": "mice-renter-apartment",
      "caller_turns": [
        "Hi, I think I have mice in my walls",
        "It's an apartment, I rent",
        ...
      ]
    }

Output: .tmp/replays/<label>-<timestamp>.json with the agent's text reply
at each turn plus any extracted log_request payloads. Stdout shows the
exchange in human-readable form.

Uses the same LoadedPrompt + ClientConfig + Haiku model as the voice path,
so a behavioral change in production is reproducible here.

Usage:
    python tools/replay_transcript.py path/to/scenario.json
    python tools/replay_transcript.py scenario.json --model claude-sonnet-4-6
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPLAY_DIR = PROJECT_ROOT / ".tmp" / "replays"

LOG_REQUEST_RE = re.compile(r"<log_request>\s*(\{.*?\})\s*</log_request>", re.DOTALL)
END_CALL_RE = re.compile(r"<end_call\s*/?>", re.IGNORECASE)


def strip_tags(text: str) -> str:
    text = LOG_REQUEST_RE.sub("", text)
    text = END_CALL_RE.sub("", text)
    return text.strip()


def replay(
    *,
    scenario_path: Path,
    model: str,
    max_tokens: int,
) -> dict:
    # Imported lazily so `--help` works without ANTHROPIC_API_KEY set.
    from tools.llm_proxy.prompt_loader import load_prompt, time_context

    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    label = scenario.get("label", scenario_path.stem)
    caller_turns: list[str] = scenario["caller_turns"]
    if not caller_turns:
        raise ValueError("scenario must contain at least one caller turn")

    loaded = load_prompt()
    full_prompt = f"{time_context(loaded.config)}\n\n{loaded.text}"
    client = Anthropic()

    messages: list[dict] = [
        {"role": "user", "content": "[the phone rings, caller has just connected]"}
    ]
    exchange: list[dict] = []
    log_requests: list[dict] = []
    ended = False
    print(f"--- replay: {label} ({model}) ---\n")

    def step_agent(turn_index: int) -> str:
        nonlocal ended
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=full_prompt,
            messages=messages,
        )
        agent_text = "".join(b.text for b in response.content if b.type == "text")
        spoken = strip_tags(agent_text)
        print(f"AGENT: {spoken}\n")
        exchange.append({"turn": turn_index, "role": "agent", "raw": agent_text, "spoken": spoken})
        messages.append({"role": "assistant", "content": agent_text})
        for m in LOG_REQUEST_RE.finditer(agent_text):
            try:
                payload = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            log_requests.append({"turn": turn_index, "payload": payload})
        if END_CALL_RE.search(agent_text):
            ended = True
        return agent_text

    # Greeting (turn 0), agent speaks first in response to the synthetic ring.
    step_agent(0)

    for i, caller_turn in enumerate(caller_turns, start=1):
        if ended:
            break
        print(f"CALLER: {caller_turn}\n")
        exchange.append({"turn": i, "role": "caller", "text": caller_turn})
        messages.append({"role": "user", "content": caller_turn})
        step_agent(i)

    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = REPLAY_DIR / f"{label}-{ts}.json"
    result = {
        "label": label,
        "scenario_path": str(scenario_path),
        "model": model,
        "prompt_version": loaded.version,
        "ended": ended,
        "log_requests": log_requests,
        "exchange": exchange,
    }
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[saved replay -> {out_path}]")
    if log_requests:
        print(f"[{len(log_requests)} log_request payload(s) emitted]")
    if not ended:
        print("[note: agent did not call end_call within the scenario]")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path, help="Path to scenario JSON")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--max-tokens", type=int, default=1024)
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1
    if not args.scenario.exists():
        print(f"ERROR: scenario not found: {args.scenario}", file=sys.stderr)
        return 1

    replay(scenario_path=args.scenario, model=args.model, max_tokens=args.max_tokens)
    return 0


if __name__ == "__main__":
    sys.exit(main())
