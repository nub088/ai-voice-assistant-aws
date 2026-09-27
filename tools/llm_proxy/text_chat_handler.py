"""
Handles the /text-chat endpoint used by the Clearslope Digital demo widget.

Callers send the full conversation history each turn (stateless). The handler
calls Anthropic or Bedrock (non-streaming) using the same system prompt as the
voice proxy, strips internal tags, and signals when the agent ends the call.
"""
from __future__ import annotations

import re
import os
import asyncio

import boto3

import anthropic
from pydantic import BaseModel

from .prompt_loader import LoadedPrompt, time_context

LOG_REQUEST_RE = re.compile(r"<log_request>\s*(\{.*?\})\s*</log_request>", re.DOTALL)
END_CALL_RE = re.compile(r"<end_call\s*/?>", re.IGNORECASE)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS = 1024


def strip_tags(text: str) -> str:
    text = LOG_REQUEST_RE.sub("", text)
    text = END_CALL_RE.sub("", text)
    return text.strip()


class ChatMessage(BaseModel):
    role: str
    content: str


class TextChatRequest(BaseModel):
    session_id: str
    messages: list[ChatMessage]


class TextChatResponse(BaseModel):
    reply: str
    ended: bool


async def handle_text_chat(
    client: anthropic.AsyncAnthropic | None,
    loaded: LoadedPrompt,
    body: TextChatRequest,
) -> TextChatResponse:
    bedrock_model = os.environ.get("BEDROCK_MODEL_ID", "").strip()
    if bedrock_model:
        def _converse():
            runtime = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))
            return runtime.converse(
                modelId=bedrock_model,
                system=[{"text": loaded.text + "\n" + time_context(loaded.config)}],
                messages=[{"role": m.role, "content": [{"text": m.content}]} for m in body.messages],
                inferenceConfig={"maxTokens": DEFAULT_MAX_TOKENS},
            )

        response = await asyncio.to_thread(_converse)
        raw = "".join(block.get("text", "") for block in response["output"]["message"]["content"])
        return TextChatResponse(reply=strip_tags(raw), ended=bool(END_CALL_RE.search(raw)))

    assert client is not None
    response = await client.messages.create(
        model=DEFAULT_MODEL,
        max_tokens=DEFAULT_MAX_TOKENS,
        system=[
            # P5: the static prompt is the cached prefix; the volatile time
            # line trails it OUTSIDE the cache_control span so cross-call
            # cache reads survive the clock ticking.
            {"type": "text", "text": loaded.text, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": time_context(loaded.config)},
        ],
        messages=[{"role": m.role, "content": m.content} for m in body.messages],
    )
    raw = "".join(block.text for block in response.content if block.type == "text")
    return TextChatResponse(reply=strip_tags(raw), ended=bool(END_CALL_RE.search(raw)))
