"""AWS path contracts without touching an AWS account."""
from __future__ import annotations

import io
import json
import asyncio
from types import SimpleNamespace

from tools.llm_proxy import aws_store
from tools.llm_proxy import aws_config
from tools.llm_proxy.twilio_auth import signed_webhook_url
from tools.llm_proxy import function_tools
from infra.aws import post_call_agent


def test_aws_transcript_keeps_redacted_text_out_of_dynamodb(monkeypatch):
    calls = []
    s3 = SimpleNamespace(put_object=lambda **kwargs: calls.append(("s3", kwargs)))
    table = SimpleNamespace(put_item=lambda **kwargs: calls.append(("ddb", kwargs)))
    monkeypatch.setattr(aws_store, "_s3", lambda: s3)
    monkeypatch.setattr(aws_store, "_table", lambda: table)
    monkeypatch.setattr(aws_store, "_bucket", lambda: "private-bucket")

    aws_store.save(
        call_sid="CA123", client_id="demo", redacted_text="[PERSON] called",
        placeholder_counts={"PERSON": 1}, residual_labels=[], residual_count=0,
        quarantine=False, duration_sec=20, caller_number_last4="1234",
    )

    assert [kind for kind, _ in calls] == ["s3", "ddb"]
    assert json.loads(calls[0][1]["Body"])["redacted_text"] == "[PERSON] called"
    assert "redacted_text" not in calls[1][1]["Item"]
    assert calls[1][1]["Item"]["s3_key"].startswith("transcripts/")


def test_deletion_removes_transcript_and_agent_output(monkeypatch):
    deleted = []
    item = {"pk": "CALL#CA123", "sk": "TRANSCRIPT", "s3_key": "transcripts/a.json"}
    s3 = SimpleNamespace(delete_object=lambda **kwargs: deleted.append(kwargs["Key"]))
    table = SimpleNamespace(
        get_item=lambda **kwargs: {"Item": item},
        delete_item=lambda **kwargs: deleted.append("index"),
    )
    monkeypatch.setattr(aws_store, "_s3", lambda: s3)
    monkeypatch.setattr(aws_store, "_table", lambda: table)
    monkeypatch.setattr(aws_store, "_bucket", lambda: "private-bucket")

    assert aws_store.delete_by_call_sid("CA123") == 1
    assert deleted == ["transcripts/a.json", "analysis/a.json", "index"]


def test_post_call_agent_reads_redacted_input_and_writes_structured_output(monkeypatch):
    writes = []
    s3 = SimpleNamespace(
        get_object=lambda **kwargs: {"Body": io.BytesIO(json.dumps({
            "call_sid": "CA123", "client_id": "demo", "quarantine": False,
            "redacted_text": "[PERSON] needs a callback",
        }).encode())},
        put_object=lambda **kwargs: writes.append(kwargs),
    )
    runtime = SimpleNamespace(converse=lambda **kwargs: {
        "output": {"message": {"content": [{"text": json.dumps({
            "summary": "Callback requested", "urgency": "routine",
            "follow_up_needed": True, "reason": "Caller asked",
        })}]}}
    })
    monkeypatch.setattr(post_call_agent.boto3, "client", lambda service, **kwargs: {
        "s3": s3, "bedrock-runtime": runtime,
    }[service])
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "test-model")

    result = post_call_agent.handle({"detail": {
        "bucket": {"name": "private-bucket"},
        "object": {"key": "transcripts/a.json"},
    }}, None)

    assert result == {"output_key": "analysis/a.json"}
    assert writes[0]["Key"] == "analysis/a.json"
    assert json.loads(writes[0]["Body"])["follow_up_needed"] is True


def test_alb_twilio_signature_url_uses_configured_hostname(monkeypatch):
    monkeypatch.setenv("AWS_ALB_PROXY", "1")
    monkeypatch.setenv("PUBLIC_BASE_URL", "voice.example.com")
    request = SimpleNamespace(url=SimpleNamespace(path="/twilio/voicemail.xml", query="client_id=demo"))
    assert signed_webhook_url(request) == "https://voice.example.com/twilio/voicemail.xml?client_id=demo"


def test_secret_bootstrap_writes_runtime_client_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWS_APP_SECRET_ARN", "arn:example")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "initial")
    data = {"TWILIO_AUTH_TOKEN": "example", "CLIENT_CONFIG": {"client_id": "demo"}}
    secret = SimpleNamespace(get_secret_value=lambda **kwargs: {"SecretString": json.dumps(data)})
    monkeypatch.setattr(aws_config.boto3, "client", lambda *args, **kwargs: secret)
    aws_config.load_aws_secrets()
    assert json.loads((tmp_path / "config/clients/aws.json").read_text()) == {"client_id": "demo"}
    assert aws_config.os.environ["TWILIO_AUTH_TOKEN"] == "example"


def test_knowledge_tool_uses_tenant_knowledge_base(monkeypatch):
    received = []
    runtime = SimpleNamespace(retrieve=lambda **kwargs: (
        received.append(kwargs) or {"retrievalResults": [{"content": {"text": "Approved hours"}}]}
    ))
    monkeypatch.setattr(function_tools.boto3, "client", lambda *args, **kwargs: runtime)

    async def inline_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(function_tools.asyncio, "to_thread", inline_thread)

    async def run():
        results = []

        async def callback(value):
            results.append(value)

        async def end_call():
            pass

        handlers = function_tools.make_handlers(
            sheets_writer=None, call_sid="CA123", on_end_call=end_call,
            knowledge_base_id="tenant-kb",
        )
        await handlers["retrieve_knowledge"](SimpleNamespace(
            arguments={"query": "What are your hours?"}, result_callback=callback,
        ))
        return results

    assert asyncio.run(run()) == [{"status": "ok", "passages": ["Approved hours"]}]
    assert received[0]["knowledgeBaseId"] == "tenant-kb"
