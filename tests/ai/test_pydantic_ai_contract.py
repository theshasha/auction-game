"""Offline production Agent / SDK wire contracts; see docs/pydantic-ai-integration.md.

No dotenv, provider discovery or real HTTP: every SDK gets a fake key and MockTransport.
Successful-stream checking and SDK construction use the production model factory.
"""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json

import pytest


from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ToolCallPart, ToolReturnPart

from tests.support.factories import CHANNELS
from auction_game.configuration import channel_model_profile
from auction_game.ai.llm_client import RequestCapture
from auction_game.reporting.replay import capture_protocol_definition, protocol_tools
from auction_game.ai.agent_tasks import BidOutput as Bid
from tests.support.wire import PROTOCOLS, MODEL, SYSTEM, GOOD, Harness, reply, run, make_agent

@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_real_sdk_stream_contract(protocol):
    async def scenario():
        data = dict(GOOD, super_bid=0)
        async with Harness(protocol, [reply(data)]) as h:
            capture = RequestCapture(h.model)
            result = await run(make_agent(capture), coins=4)
            assert result.output == Bid(**data)
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
            assert body["tool_choice"] == ({"type": "any"} if protocol == "anthropic" else "required")
            tool = body["tools"][0]
            assert tool.get("function", tool)["name"] == "bid"
            # Unknown aliases retain the provider's default profile, not guessed model-brand routing.
            assert h.model.profile["supports_tools"] is True
            parts = [p for m in result.all_messages() for p in m.parts]
            call = next(p for p in parts if isinstance(p, ToolCallPart))
            returned = next(p for p in parts if isinstance(p, ToolReturnPart))
            assert call.tool_call_id == returned.tool_call_id
            # 同一真实 SDK 请求的工具定义必须与回放离线捕获完全相同；不复制协议转换。
            snapshot = await capture_protocol_definition(h.factory, capture.requests[0])
            assert snapshot["source"] == "offline_sdk_mock_transport"
            assert snapshot["definition"] == protocol_tools(body)
            assert "body" not in snapshot and snapshot["versions"]["openai"]
            schema = tool.get("function", tool)
            schema = schema.get("parameters", schema.get("input_schema"))
            assert schema["additionalProperties"] is False
            assert {"type": "null"} in schema["properties"]["super_bid"]["anyOf"]
            if protocol == "responses":
                assert snapshot["definition"]["tools"][0]["strict"] is False
                # 让同一公开 SDK 边界实际发送 JSON null，覆盖展示提取时的保真。
                nullable = deepcopy(capture.requests[0])
                nullable["parameters"]["output_tools"][0]["parameters_json_schema"]["properties"]["super_bid"]["default"] = None
                with_null = await capture_protocol_definition(h.factory, nullable)
                assert with_null["definition"]["tools"][0]["parameters"]["properties"]["super_bid"]["default"] is None
    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("replies,succeeds", [
    ([reply(dict(GOOD, bid="2")), reply()], True),
    ([reply(dict(GOOD, bid=6)), reply(dict(GOOD, bid="2"))], False),
], ids=["schema-then-success", "business-then-schema-exhausts"])
def test_one_shared_correction_budget(protocol, replies, succeeds):
    async def scenario():
        h = Harness(protocol, replies)
        h.factory = replace(h.factory, model_profile=channel_model_profile(CHANNELS["hub"], protocol))
        async with h:
            if succeeds:
                result = await run(make_agent(h.model), coins=5)
                assert result.output.bid == 2
            else:
                with pytest.raises(UnexpectedModelBehavior, match="retries"):
                    await run(make_agent(h.model), coins=5)
            assert len(h.requests) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("super_bid", [None, "null"])
def test_no_tool_exhausts_correction(protocol, super_bid):
    async def scenario():
        # 即使文本是合法出价 JSON，也必须调用输出工具；auto 保留共享一次纠错。
        h = Harness(protocol, [reply(tool=False, text=json.dumps(dict(GOOD, super_bid=super_bid)))] * 2)
        h.factory = replace(h.factory, model_profile=channel_model_profile(CHANNELS["hub"], protocol))
        async with h:
            with pytest.raises(UnexpectedModelBehavior):
                await run(make_agent(h.model))
            assert len(h.requests) == 2
            if protocol == "responses":
                assert all(body["tool_choice"] == "auto" for _, body in h.requests)
    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("first,second", [
    (reply(tool=False, text=json.dumps(GOOD)), reply(dict(GOOD, super_bid="None"))),
    (reply(dict(GOOD, super_bid="None")), reply(tool=False, text=json.dumps(GOOD))),
    (reply(tool_name="plan"), reply(dict(GOOD, bid=99))),
    (reply(dict(GOOD, bid=99)), reply(tool_name="plan")),
    (reply(tool=False), reply(tool_name="plan")),
])
def test_mixed_corrections_share_two_request_limit(protocol, first, second):
    from auction_game.ai.agent_tasks import TaskRunner
    from auction_game.ai.agent_execution import OutputFailedError
    from tests.support.factories import TEST_GAME
    h = Harness(protocol, [first, second])
    with pytest.raises(OutputFailedError):
        TaskRunner(h.factory)("plan", "plan", [], game=TEST_GAME, coins=6)
    assert len(h.requests) == 2 and h.closes == 1


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("value", ["None", "3", True, False, 2.0, " null", "null ", "NULL", "Null", ""])
def test_super_bid_feedback_preserves_schema_and_explains_json_null(protocol, value):
    from auction_game.ai.agent_tasks import TaskRunner
    from tests.support.factories import TEST_GAME
    h = Harness(protocol, [reply(dict(GOOD, super_bid=value)), reply()])
    result = TaskRunner(h.factory)("plan", "plan", [], game=TEST_GAME, coins=6)
    assert result.output.super_bid is None and len(h.requests) == 2
    body = json.dumps(h.requests[1][1], ensure_ascii=False)
    assert "整数或不加引号的 JSON null" in body
    tool = h.requests[0][1]["tools"][0]
    tool = tool.get("function", tool)
    schema = tool.get("parameters", tool.get("input_schema"))
    assert schema["properties"]["super_bid"]["anyOf"] == [{"type": "integer"}, {"type": "null"}]
    assert "super_bid" in schema["required"]


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("value", [None, "null"])
def test_super_bid_null_compatibility_preserves_raw_audit(protocol, value, tmp_path):
    from pydantic_ai.messages import ModelMessagesTypeAdapter, UserPromptPart
    from auction_game.ai.agent_tasks import TaskRunner
    from tests.support.factories import make_ai_obs, make_player

    h = Harness(protocol, [reply(dict(GOOD, super_bid=value))])
    player = make_player(TaskRunner(h.factory), tmp_path)
    assert player.decide_bid(make_ai_obs()) == (2, None)
    assert len(h.requests) == h.closes == 1
    entry = json.loads(player.log_path.read_text(encoding="utf-8"))
    assert entry["result"]["super_bid"] is None
    audit = ModelMessagesTypeAdapter.validate_python(entry["messages"])
    call = next(p for m in audit for p in m.parts if isinstance(p, ToolCallPart))
    assert call.args_as_dict()["super_bid"] == value
    history = next(p.content for m in player.messages for p in m.parts if isinstance(p, UserPromptPart))
    business_json = history.split("【已完成任务的有效结果】\n")[1].split("\n【历史任务资料结束】")[0]
    assert json.loads(business_json) == entry["result"]


@pytest.mark.parametrize("tool", [True, False])
def test_chat_auto_profile_preserves_tools_and_validation(tool):
    async def scenario():
        h = Harness("chat", [reply(tool=tool, text=json.dumps(GOOD))] * 2)
        profile = channel_model_profile({"chat_profile": {"supports_forced_tool_choice": False}}, "chat")
        h.factory = replace(h.factory, model_profile=profile)
        async with h:
            if tool:
                result = await run(make_agent(h.model))
                assert result.output.bid == GOOD["bid"]
            else:
                with pytest.raises(UnexpectedModelBehavior):
                    await run(make_agent(h.model))
            assert len(h.requests) == (1 if tool else 2)
            assert all(body["tool_choice"] == "auto" and body["tools"] for _, body in h.requests)
    asyncio.run(scenario())
