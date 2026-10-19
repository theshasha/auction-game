"""Offline production Agent / SDK wire contracts; see docs/reference.md.

No dotenv, provider discovery or real HTTP: every SDK gets a fake key and MockTransport.
Successful-stream checking and SDK construction use the production model factory.
"""

import asyncio
from copy import deepcopy
import json

import pytest


from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import TextPart, ToolCallPart, ToolReturnPart

from tests.support.factories import text_reply, task_entries
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
            # Unknown aliases retain the provider's default profile, not guessed model-brand routing.
            assert h.model.profile["supports_tools"] is True
            parts = [p for m in result.all_messages() for p in m.parts]
            assert [p.content for p in parts if isinstance(p, TextPart)] == [text_reply(data)]
            assert not any(isinstance(p, (ToolCallPart, ToolReturnPart)) for p in parts)
    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("replies,succeeds", [
    ([reply(text="missing sections"), reply()], True),
    ([reply(dict(GOOD, bid=6)), reply(text="missing sections")], False),
], ids=["format-then-success", "business-then-format-exhausts"])
def test_one_shared_correction_budget(protocol, replies, succeeds):
    async def scenario():
        h = Harness(protocol, replies)
        async with h:
            if succeeds:
                result = await run(make_agent(h.model), coins=5)
                assert result.output["bid"] == 2
            else:
                with pytest.raises(UnexpectedModelBehavior, match="retries"):
                    await run(make_agent(h.model), coins=5)
            assert len(h.requests) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_bare_json_exhausts_correction(protocol):
    async def scenario():
        # 出价 JSON 必须放在完整的 strategy/decision 文本结构内。
        h = Harness(protocol, [reply(text=json.dumps(GOOD))] * 2)
        async with h:
            with pytest.raises(UnexpectedModelBehavior):
                await run(make_agent(h.model))
            assert len(h.requests) == 2
            assert all(not body.get("tools") and "tool_choice" not in body for _, body in h.requests)
    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("first,second", [
    (reply(text=json.dumps(GOOD)), reply(dict(GOOD, super_bid="None"))),
    (reply(dict(GOOD, bid=99)), reply(text="missing sections")),
], ids=["format-then-business", "business-then-format"])
def test_mixed_corrections_share_two_request_limit(protocol, first, second):
    from auction_game.ai.agent_tasks import TaskRunner
    from auction_game.ai.agent_execution import OutputFailedError
    from tests.support.factories import TEST_GAME
    h = Harness(protocol, [first, second])
    with pytest.raises(OutputFailedError):
        TaskRunner(h.factory)("plan", "plan", [], game=TEST_GAME, coins=6)
    assert len(h.requests) == 2 and h.closes == 1


@pytest.mark.parametrize("value", ["None", "null", True, 2.0])
def test_non_integer_super_bid_gets_correction(value):
    from auction_game.ai.agent_tasks import TaskRunner
    from tests.support.factories import TEST_GAME
    h = Harness("chat", [reply(dict(GOOD, super_bid=value)), reply()])
    result = TaskRunner(h.factory)("plan", "plan", [], game=TEST_GAME, coins=6)
    assert result.output["super_bid"] is None and len(h.requests) == 2
    assert "super_bid" in json.dumps(h.requests[1][1]["messages"][-1], ensure_ascii=False)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_bid_keeps_raw_reply_in_audit_and_history(protocol, tmp_path):
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    from auction_game.ai.agent_tasks import TaskRunner
    from tests.support.factories import make_ai_obs, make_player

    h = Harness(protocol, [reply()])
    player = make_player(TaskRunner(h.factory), tmp_path)
    assert player.decide_bid(make_ai_obs()) == (2, None)
    assert len(h.requests) == h.closes == 1
    entry = task_entries(player.log_path)[-1]
    assert entry["result"]["super_bid"] is None
    audit = ModelMessagesTypeAdapter.validate_python(entry["messages"])
    expected = text_reply(GOOD)
    assert [p.content for m in audit for p in m.parts if isinstance(p, TextPart)] == [expected]
    assert [p.content for m in player.messages for p in m.parts if isinstance(p, TextPart)] == [expected]


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_failed_task_after_sdk_history_merge_audits_both_raw_replies(protocol, tmp_path):
    from pydantic_ai.messages import ModelMessagesTypeAdapter, UserPromptPart
    from auction_game.ai.agent_tasks import TaskRunner
    from auction_game.ai.agent_execution import OutputFailedError
    from tests.support.factories import make_ai_obs, make_player

    invalid = [dict(GOOD, bid="2"), dict(GOOD, bid=99)]
    h = Harness(protocol, [reply(), *(reply(data) for data in invalid)])
    player = make_player(TaskRunner(h.factory), tmp_path)
    player.plan_strategy("previous successful task", "opening_plan")
    before = deepcopy(player.checkpoint())
    with pytest.raises(OutputFailedError):
        player.decide_bid(make_ai_obs(day=2))
    assert player.checkpoint() == before
    entry = task_entries(player.log_path)[-1]
    audit = ModelMessagesTypeAdapter.validate_python(entry["messages"])
    assert [p.content for m in audit for p in m.parts if isinstance(p, TextPart)] == [
        text_reply(data) for data in invalid]
    prompts = [p.content for m in audit for p in m.parts if isinstance(p, UserPromptPart)]
    assert len(prompts) == 1 and "previous successful task" not in prompts[0]
    assert len(h.requests) == 3 and h.closes == 2


def test_hallucinated_tool_call_fails_without_retry():
    from auction_game.ai.agent_tasks import TaskRunner
    from auction_game.ai.agent_execution import CallFailedError
    from tests.support.factories import TEST_GAME

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
