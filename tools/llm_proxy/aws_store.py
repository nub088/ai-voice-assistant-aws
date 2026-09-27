"""AWS persistence for the voice app: redacted text in S3, indexes in DynamoDB.

Selected by AWS_CALLS_TABLE in transcript_store. The live call path treats
post-call persistence as best effort, matching the original SQLite behavior.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

logger = logging.getLogger("aws_store")


def _table():
    return boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION")).Table(
        os.environ["AWS_CALLS_TABLE"]
    )


def _s3():
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION"))


def _bucket() -> str:
    return os.environ["AWS_TRANSCRIPT_BUCKET"]


def _key(call_sid: str) -> str:
    # Do not allow an externally supplied identifier to become an S3 path.
    return "transcripts/" + hashlib.sha256(call_sid.encode()).hexdigest() + ".json"


def _utc(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _scan(filter_expression, projection_expression=None):
    kwargs = {"FilterExpression": filter_expression}
    if projection_expression:
        kwargs["ProjectionExpression"] = projection_expression
    table = _table()
    while True:
        page = table.scan(**kwargs)
        yield from page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def save(*, call_sid, client_id, redacted_text, placeholder_counts,
         residual_labels, residual_count, quarantine, duration_sec,
         caller_number_last4):
    key = _key(call_sid)
    payload = {
        "call_sid": call_sid,
        "client_id": client_id,
        "created_at": _utc(),
        "redacted_text": redacted_text,
        "placeholder_counts": placeholder_counts,
        "residual_labels": residual_labels,
        "residual_count": residual_count,
        "quarantine": bool(quarantine),
        "duration_sec": duration_sec,
        "caller_number_last4": caller_number_last4,
    }
    try:
        _s3().put_object(
            Bucket=_bucket(), Key=key, Body=json.dumps(payload).encode(),
            ContentType="application/json", ServerSideEncryption="AES256",
        )
        _table().put_item(
            Item={
                "pk": "CALL#" + call_sid, "sk": "TRANSCRIPT",
                "client_id": client_id, "created_at": payload["created_at"],
                "caller_number_last4": caller_number_last4 or "",
                "s3_key": key, "quarantine": bool(quarantine),
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            logger.exception("call_sid=%s AWS transcript save failed", call_sid)
    except Exception:
        logger.exception("call_sid=%s AWS transcript save failed", call_sid)


def save_minute_counter(*, client_id, total_minutes):
    try:
        _table().put_item(Item={
            "pk": "CLIENT#" + client_id, "sk": "MINUTES",
            "total_min": Decimal(str(total_minutes)), "updated_at": _utc(),
        })
    except Exception:
        logger.exception("client_id=%s AWS minute counter save failed", client_id)


def save_call_usage(*, call_sid, client_id, duration_sec, llm_model,
                    llm_prompt_tokens, llm_completion_tokens,
                    llm_cache_read_tokens, llm_cache_creation_tokens,
                    tts_model, tts_characters):
    try:
        _table().put_item(
            Item={
                "pk": "CALL#" + call_sid, "sk": "USAGE",
                "client_id": client_id, "created_at": _utc(),
                "duration_sec": Decimal(str(duration_sec)),
                "llm_model": llm_model or "", "tts_model": tts_model or "",
                "llm_prompt_tokens": int(llm_prompt_tokens),
                "llm_completion_tokens": int(llm_completion_tokens),
                "llm_cache_read_tokens": int(llm_cache_read_tokens),
                "llm_cache_creation_tokens": int(llm_cache_creation_tokens),
                "tts_characters": int(tts_characters),
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            logger.exception("call_sid=%s AWS usage save failed", call_sid)
    except Exception:
        logger.exception("call_sid=%s AWS usage save failed", call_sid)


def _delete_transcript(item) -> int:
    _s3().delete_object(Bucket=_bucket(), Key=item["s3_key"])
    _s3().delete_object(Bucket=_bucket(), Key="analysis/" + item["s3_key"].removeprefix("transcripts/"))
    _table().delete_item(Key={"pk": item["pk"], "sk": "TRANSCRIPT"})
    return 1


def delete_by_call_sid(call_sid: str) -> int:
    item = _table().get_item(Key={"pk": "CALL#" + call_sid, "sk": "TRANSCRIPT"}).get("Item")
    return _delete_transcript(item) if item else 0


def delete_by_caller_last4(caller_number_last4: str) -> int:
    items = _scan(Attr("sk").eq("TRANSCRIPT") & Attr("caller_number_last4").eq(caller_number_last4))
    return sum(_delete_transcript(item) for item in items)


def purge_expired(retention_days: int = 90, now: datetime | None = None) -> int:
    cutoff = _utc((now or datetime.now(timezone.utc)) - timedelta(days=retention_days))
    items = _scan(Attr("sk").eq("TRANSCRIPT") & Attr("created_at").lt(cutoff))
    return sum(_delete_transcript(item) for item in items)


def load_minute_counters() -> dict[str, float]:
    try:
        return {
            item["pk"].removeprefix("CLIENT#"): float(item["total_min"])
            for item in _scan(Attr("sk").eq("MINUTES"))
        }
    except Exception:
        logger.exception("AWS minute counter load failed")
        return {}
