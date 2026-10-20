"""三种协议下生产 Agent 和真实 SDK 的线上请求形状、纠错次数上限。"""

import asyncio
import json

import pytest
from pydantic_ai.messages import TextPart

from auction_game.ai.agent_execution import CallFailedError, OutputFailedError
from auction_game.ai.agent_tasks import TaskRunner
from tests.support.factories import TEST_GAME, text_reply
from tests.support.wire import PROTOCOLS, MODEL, SYSTEM, GOOD, Harness, reply, run, make_agent


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_real_sdk_stream_contract(protocol):
    async def scenario():
        data = dict(GOOD, super_bid=0)
        async with Harness(protocol, [reply(data)]) as h:
            result = await run(make_agent(h.model), coins=4)
            assert result.output == data
            assert len(h.requests) == 1
            request, body = h.requests[0]
            assert request.url.path == {"chat": "/v1/chat/completions", "responses": "/v1/responses",
                                        "anthropic": "/anthropic/v1/messages"}[protocol]
            assert body["model"] == MODEL and body["stream"] is True
            field = "max_output_tokens" if protocol == "responses" else "max_tokens"
            assert body[field] == 123 and "max_completion_tokens" not in body
            assert request.extensions["timeout"]["read"] == 3.0
            assert request.headers["x-api-key" if protocol == "anthropic" else "authorization"] == (
                "fake-key" if protocol == "anthropic" else "Bearer fake-key")
            assert json.dumps(body).count(SYSTEM) == 1
            assert json.dumps(body).count('"today"') == 1
            assert not body.get("tools") and "tool_choice" not in body
            assert "response_format" not in body and "text" not in body
            assert "output_config" not in body
            parts = [p for m in result.all_messages() for p in m.parts]
            assert [p.content for p in parts if isinstance(p, TextPart)] == [text_reply(data)]
    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("first,second", [
    (reply(text=json.dumps(GOOD)), reply(dict(GOOD, super_bid="None"))),
    (reply(dict(GOOD, bid=99)), reply(text="missing sections")),
], ids=["format-then-business", "business-then-format"])
def test_mixed_corrections_share_two_request_limit(protocol, first, second):
    h = Harness(protocol, [first, second])
    with pytest.raises(OutputFailedError):
        TaskRunner(h.factory)("plan", "plan", [], game=TEST_GAME, coins=6)
    assert len(h.requests) == 2 and h.closes == 1


@pytest.mark.parametrize("value", ["null", 2.0])
def test_non_integer_super_bid_gets_correction(value):
    h = Harness("chat", [reply(dict(GOOD, super_bid=value)), reply()])
    result = TaskRunner(h.factory)("plan", "plan", [], game=TEST_GAME, coins=6)
    assert result.output["super_bid"] is None and len(h.requests) == 2
    assert "super_bid" in json.dumps(h.requests[1][1]["messages"][-1], ensure_ascii=False)


def test_hallucinated_tool_call_fails_without_retry():
    def chunk(delta, finish=None):
        event = {"id": "chat_1", "object": "chat.completion.chunk", "created": 1, "model": MODEL,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return ("data: " + json.dumps(event) + "\n\n").encode()

    call = {"index": 0, "id": "call_1", "type": "function",
            "function": {"name": "bid", "arguments": json.dumps(GOOD)}}
    h = Harness("chat", [[chunk({"role": "assistant", "tool_calls": [call]}),
                          chunk({}, "tool_calls"), b"data: [DONE]\n\n"]])
    with pytest.raises(CallFailedError, match="IncompleteStream"):
        TaskRunner(h.factory)("bid", "today", [], game=TEST_GAME, coins=6)
    assert len(h.requests) == h.closes == 1
