"""Tests for the Pipecat function-tool wrappers.

These tests stub `FunctionCallParams` (the only thing the handlers consume) so
they exercise the full happy + error paths without needing the rest of Pipecat
to be wired up.
"""
from __future__ import annotations

from typing import Any

import pytest

from tools.llm_proxy.function_tools import (
    END_CALL_SCHEMA,
    LOG_REQUEST_SCHEMA,
    TOOLS_SCHEMA,
    make_handlers,
)


class FakeSheetsWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.rows: list[list[Any]] = []
        self._fail = fail

    def append_row(self, row: list[Any]) -> None:
        if self._fail:
            raise RuntimeError("sheets api boom")
        self.rows.append(row)


class FakeSmsNotifier:
    def __init__(self) -> None:
        self.customer_calls: list[dict[str, Any]] = []
        self.owner_calls: list[dict[str, Any]] = []

    async def send_customer_confirmation(self, *, to: str, caller_name: str, urgency: str) -> bool:
        self.customer_calls.append({"to": to, "caller_name": caller_name, "urgency": urgency})
        return True

    async def send_owner_emergency_alert(
        self, *, caller_name: str, address: str, pest: str, callback: str
    ) -> bool:
        self.owner_calls.append(
            {"caller_name": caller_name, "address": address, "pest": pest, "callback": callback}
        )
        return True


class FakeFunctionCallParams:
    def __init__(self, arguments: dict[str, Any]):
        self.arguments = arguments
        self.results: list[Any] = []

    async def result_callback(self, payload: Any) -> None:
        self.results.append(payload)


def test_schemas_present() -> None:
    assert LOG_REQUEST_SCHEMA.name == "log_request"
    assert "caller_name" in LOG_REQUEST_SCHEMA.properties
    assert "callback_number" in LOG_REQUEST_SCHEMA.properties
    assert END_CALL_SCHEMA.name == "end_call"
    # ToolsSchema should expose both tools.
    assert TOOLS_SCHEMA.standard_tools[0].name == "log_request"
    assert TOOLS_SCHEMA.standard_tools[1].name == "end_call"


@pytest.mark.asyncio
async def test_log_request_writes_row_via_sheets_writer() -> None:
    writer = FakeSheetsWriter()
    end_called = {"yes": False}

    async def on_end() -> None:
        end_called["yes"] = True

    handlers = make_handlers(sheets_writer=writer, call_sid="CA123", on_end_call=on_end)
    params = FakeFunctionCallParams(
        arguments={
            "caller_name": "Alex",
            "callback_number": "604-555-0123",
            "service_address": "100 Main St",
            "identified_pest": "carpenter ant",
            "pest_description": "big black ants near the deck",
            "urgency": "routine",
            "best_callback_time": "morning",
            "out_of_scope": False,
            "needs_followup_notes": "",
            "ai_confidence": "high",
        }
    )
    await handlers["log_request"](params)
    assert params.results == [{"status": "saved"}]
    assert len(writer.rows) == 1
    # The full phone number must NOT land in the row, only last4.
    row_text = " ".join(str(c) for c in writer.rows[0])
    assert "604" not in row_text
    assert "0123" in row_text
    # call_sid should appear in the vapi_call_id column (preserved schema).
    assert "CA123" in row_text


@pytest.mark.asyncio
async def test_log_request_returns_error_when_sheets_unavailable() -> None:
    async def on_end() -> None:
        pass

    handlers = make_handlers(sheets_writer=None, call_sid="CA1", on_end_call=on_end)
    params = FakeFunctionCallParams(arguments={"caller_name": "X"})
    await handlers["log_request"](params)
    assert params.results == [{"status": "error", "message": "booking system not configured"}]


@pytest.mark.asyncio
async def test_log_request_returns_error_on_writer_failure() -> None:
    writer = FakeSheetsWriter(fail=True)

    async def on_end() -> None:
        pass

    handlers = make_handlers(sheets_writer=writer, call_sid="CA1", on_end_call=on_end)
    params = FakeFunctionCallParams(
        arguments={
            "caller_name": "X",
            "callback_number": "555",
            "service_address": "x",
            "urgency": "routine",
        }
    )
    await handlers["log_request"](params)
    assert params.results[0]["status"] == "error"
    assert "boom" in params.results[0]["message"]


@pytest.mark.asyncio
async def test_end_call_invokes_callback() -> None:
    end_called = {"yes": False}

    async def on_end() -> None:
        end_called["yes"] = True

    handlers = make_handlers(sheets_writer=None, call_sid="CA1", on_end_call=on_end)
    params = FakeFunctionCallParams(arguments={})
    await handlers["end_call"](params)
    assert params.results == [{"status": "ending"}]
    assert end_called["yes"] is True


@pytest.mark.asyncio
async def test_log_request_sends_customer_sms_for_routine_call() -> None:
    writer = FakeSheetsWriter()
    sms = FakeSmsNotifier()

    async def on_end() -> None:
        pass

    handlers = make_handlers(
        sheets_writer=writer,
        call_sid="CA1",
        on_end_call=on_end,
        sms_notifier=sms,
        caller_number="+16045550123",
    )
    params = FakeFunctionCallParams(
        arguments={
            "caller_name": "Alex Doe",
            "callback_number": "604-555-0123",
            "service_address": "100 Main St",
            "identified_pest": "carpenter ant",
            "urgency": "routine",
        }
    )
    await handlers["log_request"](params)
    assert params.results == [{"status": "saved"}]
    assert len(sms.customer_calls) == 1
    assert sms.customer_calls[0]["to"] == "+16045550123"
    assert sms.customer_calls[0]["caller_name"] == "Alex Doe"
    assert sms.customer_calls[0]["urgency"] == "routine"
    # Routine should NOT page the owner.
    assert sms.owner_calls == []


@pytest.mark.asyncio
async def test_log_request_emergency_pages_owner_and_customer() -> None:
    writer = FakeSheetsWriter()
    sms = FakeSmsNotifier()

    async def on_end() -> None:
        pass

    handlers = make_handlers(
        sheets_writer=writer,
        call_sid="CA1",
        on_end_call=on_end,
        sms_notifier=sms,
        caller_number="+16045550123",
    )
    params = FakeFunctionCallParams(
        arguments={
            "caller_name": "Alex",
            "callback_number": "604-555-0123",
            "service_address": "100 Main St",
            "identified_pest": "norway rat",
            "urgency": "emergency",
        }
    )
    await handlers["log_request"](params)
    assert len(sms.customer_calls) == 1
    assert len(sms.owner_calls) == 1
    owner = sms.owner_calls[0]
    assert owner["address"] == "100 Main St"
    assert owner["pest"] == "norway rat"
    assert owner["callback"] == "+16045550123"


@pytest.mark.asyncio
async def test_log_request_skips_customer_sms_when_caller_number_missing() -> None:
    """Anonymous / blocked callers still get logged and (if emergency) page the owner."""
    writer = FakeSheetsWriter()
    sms = FakeSmsNotifier()

    async def on_end() -> None:
        pass

    handlers = make_handlers(
        sheets_writer=writer,
        call_sid="CA1",
        on_end_call=on_end,
        sms_notifier=sms,
        caller_number="",
    )
    params = FakeFunctionCallParams(
        arguments={
            "caller_name": "Alex",
            "callback_number": "555",
            "service_address": "100 Main St",
            "urgency": "emergency",
            "identified_pest": "wasp",
        }
    )
    await handlers["log_request"](params)
    assert sms.customer_calls == []
    assert len(sms.owner_calls) == 1


@pytest.mark.asyncio
async def test_log_request_no_sms_when_notifier_is_none() -> None:
    writer = FakeSheetsWriter()

    async def on_end() -> None:
        pass

    handlers = make_handlers(
        sheets_writer=writer,
        call_sid="CA1",
        on_end_call=on_end,
        sms_notifier=None,
        caller_number="+16045550123",
    )
    params = FakeFunctionCallParams(
        arguments={
            "caller_name": "Alex",
            "callback_number": "555",
            "service_address": "100 Main St",
            "urgency": "emergency",
        }
    )
    await handlers["log_request"](params)
    assert params.results == [{"status": "saved"}]
    assert len(writer.rows) == 1


@pytest.mark.asyncio
async def test_log_request_is_idempotent_across_duplicate_calls() -> None:
    """Models occasionally call log_request twice; SMS must fire exactly once."""
    writer = FakeSheetsWriter()
    sms = FakeSmsNotifier()

    async def on_end() -> None:
        pass

    handlers = make_handlers(
        sheets_writer=writer,
        call_sid="CA1",
        on_end_call=on_end,
        sms_notifier=sms,
        caller_number="+16045550123",
    )
    args = {
        "caller_name": "Alex",
        "callback_number": "604-555-0123",
        "service_address": "100 Main St",
        "urgency": "emergency",
        "identified_pest": "rat",
    }
    await handlers["log_request"](FakeFunctionCallParams(arguments=dict(args)))
    await handlers["log_request"](FakeFunctionCallParams(arguments=dict(args)))
    assert len(sms.customer_calls) == 1
    assert len(sms.owner_calls) == 1
    # The Sheet is the source of truth; both calls still write rows.
    assert len(writer.rows) == 2


@pytest.mark.asyncio
async def test_log_request_does_not_send_sms_when_sheets_write_fails() -> None:
    """If the row didn't land, don't promise the customer we got it."""
    writer = FakeSheetsWriter(fail=True)
    sms = FakeSmsNotifier()

    async def on_end() -> None:
        pass

    handlers = make_handlers(
        sheets_writer=writer,
        call_sid="CA1",
        on_end_call=on_end,
        sms_notifier=sms,
        caller_number="+16045550123",
    )
    params = FakeFunctionCallParams(
        arguments={
            "caller_name": "Alex",
            "callback_number": "555",
            "service_address": "x",
            "urgency": "emergency",
        }
    )
    await handlers["log_request"](params)
    assert params.results[0]["status"] == "error"
    assert sms.customer_calls == []
    assert sms.owner_calls == []


@pytest.mark.asyncio
async def test_log_request_writes_transcript_retain_true_by_default() -> None:
    """No opt-out → transcript_retain=True in the Sheet row."""
    from tools.llm_proxy.sheets_writer import HEADER_ROW

    writer = FakeSheetsWriter()

    async def _noop() -> None:
        return None

    handlers = make_handlers(
        sheets_writer=writer,
        call_sid="CA200",
        on_end_call=_noop,
    )
    params = FakeFunctionCallParams(arguments={
        "caller_name": "Bea", "callback_number": "604-555-7777",
        "service_address": "1 X St", "urgency": "routine",
    })
    await handlers["log_request"](params)
    assert len(writer.rows) == 1
    idx = HEADER_ROW.index("transcript_retain")
    assert writer.rows[0][idx] is True


@pytest.mark.asyncio
async def test_set_recording_preference_false_then_log_request_writes_retain_false() -> None:
    """Caller said 'no transcript' → transcript_retain=False in the Sheet row."""
    from tools.llm_proxy.sheets_writer import HEADER_ROW

    writer = FakeSheetsWriter()

    async def _noop() -> None:
        return None

    handlers = make_handlers(
        sheets_writer=writer,
        call_sid="CA201",
        on_end_call=_noop,
    )
    # Caller opts out mid-call.
    pref_params = FakeFunctionCallParams(arguments={"record": False, "reason": "no transcript"})
    await handlers["set_recording_preference"](pref_params)
    assert pref_params.results == [{"status": "noted", "record": False}]

    # Subsequent log_request must record the opt-out.
    log_params = FakeFunctionCallParams(arguments={
        "caller_name": "Cee", "callback_number": "604-555-8888",
        "service_address": "2 Y St", "urgency": "routine",
    })
    await handlers["log_request"](log_params)
    assert len(writer.rows) == 1
    idx = HEADER_ROW.index("transcript_retain")
    assert writer.rows[0][idx] is False


def test_recording_state_is_exposed_for_end_of_call_consumers() -> None:
    """The end-of-call transcript log path reads recording_state, expose it."""
    async def _noop() -> None:
        return None

    handlers = make_handlers(sheets_writer=None, call_sid="CA202", on_end_call=_noop)
    # The factory returns the recording_state dict as a hidden attribute on the
    # returned mapping, so voice_pipeline.run_voice_session can read it.
    assert "_recording_state" in handlers
    assert handlers["_recording_state"]["record"] is None


@pytest.mark.asyncio
async def test_set_recording_preference_missing_field_does_not_silently_opt_out() -> None:
    """PIPA s. 14: a withdrawal must be the caller's act, not coerced from a missing arg."""
    async def _noop() -> None:
        return None

    handlers = make_handlers(sheets_writer=None, call_sid="CA203", on_end_call=_noop)
    # Schema violation: 'record' field omitted entirely.
    pref_params = FakeFunctionCallParams(arguments={"reason": "ambiguous"})
    await handlers["set_recording_preference"](pref_params)
    # The handler refuses the call rather than coercing None→False.
    assert pref_params.results == [
        {"status": "error", "message": "record field required"}
    ]
    # And it does NOT touch the state dict, caller stays at "no preference".
    assert handlers["_recording_state"]["record"] is None
