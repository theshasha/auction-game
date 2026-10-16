"""生产 TaskRunner 的 HTTP 异常分类、流隔离与客户端生命周期。"""

from dataclasses import replace
from copy import deepcopy
import json

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessagesTypeAdapter, ToolCallPart, ToolReturnPart

from auction_game.ai.agent_execution import CallFailedError, OutputFailedError
from auction_game.ai.agent_tasks import TaskRunner
from tests.support.factories import CHANNELS, TEST_GAME, make_player, make_ai_obs
from auction_game.ai.llm_client import retryable_request_error
from tests.support.wire import Harness, PROTOCOLS, reply, GOOD


@pytest.mark.parametrize("status,expected", [
    (400, False), (401, False), (403, False), (408, True), (429, True),
    (499, False), (500, True), (599, True), (600, False),
])
def test_status_retry_classification(status, expected):
    assert retryable_request_error(ModelHTTPError(status, "alias")) is expected


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("status,retryable", [(401, False), (503, True)])
def test_sdk_status_retry_and_client_close(protocol, status, retryable):
    h = Harness(protocol, [status, reply()])
    player = make_player(TaskRunner(h.factory))
    if retryable:
        assert player.decide_bid(make_ai_obs()) == (2, None)
    else:
        with pytest.raises(CallFailedError):
            player.decide_bid(make_ai_obs())
        assert len(player.messages) == 1
    assert len(h.requests) == (2 if retryable else 1)
    assert h.closes == 1 and all(s.closed for s in h.streams)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("fault", ["connect", "stream", "limit", "missing"])
def test_failed_flow_never_commits_partial_messages(protocol, fault, tmp_path):
    item = (fault if fault == "connect" else reply(broken=True, ending="missing") if fault == "stream"
            else reply(ending=fault))
    h = Harness(protocol, [item, deepcopy(item)])
    player = make_player(TaskRunner(h.factory), tmp_path)
    before = deepcopy(player.messages)
    with pytest.raises(CallFailedError):
        player.decide_bid(make_ai_obs())
    assert player.messages == before and player._pending_day1_bid is None
    assert len(h.requests) == (2 if fault in ("connect", "stream") else 1)
    entry = json.loads(player.log_path.read_text(encoding="utf-8"))
    audit = ModelMessagesTypeAdapter.validate_python(entry["messages"])
    assert not any(isinstance(p, ToolCallPart) for m in audit for p in m.parts)
    assert entry["request_errors"] and h.closes == 1
    assert all(s.closed for s in h.streams)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("succeeds", [True, False], ids=["recovers", "output-budget-exhausted"])
def test_mixed_network_and_output_budget_reaches_at_most_four_attempts(protocol, succeeds, tmp_path):
    final = reply() if succeeds else reply(dict(GOOD, bid=99))
    h = Harness(protocol, ["connect", reply(dict(GOOD, bid="2")),
                           reply(broken=True, ending="missing"), final])
    player = make_player(TaskRunner(h.factory, capture_requests=True), tmp_path)
    before = deepcopy(player.messages)
    if succeeds:
        assert player.decide_bid(make_ai_obs()) == (2, None)
        parts = [p for m in player.messages for p in m.parts]
        assert sum(isinstance(p, ToolCallPart) for p in parts) == 2
    else:
        with pytest.raises(OutputFailedError):
            player.decide_bid(make_ai_obs())
        assert player.messages == before
    assert len(h.requests) == 4 and h.closes == 1
    assert h.requests[0][1] == h.requests[1][1] and h.requests[2][1] == h.requests[3][1]
    assert h.requests[0][1] != h.requests[2][1]
    entry = json.loads(player.log_path.read_text(encoding="utf-8"))
    assert [(e["request"], e["attempt"]) for e in entry["request_errors"]] == [(1, 1), (2, 1)]
    from auction_game.reporting.request_records import load_requests
    assert "requests" not in entry
    load_requests(tmp_path / "capture.jsonl", [entry])
    assert [record["errors"] for record in entry["requests"]] == [[error] for error in entry["request_errors"]]
    markdown = player.log_path.with_suffix(".md").read_text(encoding="utf-8")
    assert "网络尝试 4 次（请求 1：2 次；请求 2：2 次）" in markdown
    assert f"输出纠错 1 次，{'最终通过校验' if succeeds else '最终失败'}" in markdown
    assert all(s.closed for s in h.streams)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_success_then_failure_in_new_loop_keeps_previous_state_and_closes(protocol, tmp_path):
    h = Harness(protocol, ["connect", reply(), 400])
    player = make_player(TaskRunner(h.factory), tmp_path)
    player.plan_strategy("plan", "opening_plan")
    before = deepcopy(player.messages), player._pending_day1_bid
    with pytest.raises(CallFailedError):
        player.give_speech("speak", "opening_speech")
    assert (player.messages, player._pending_day1_bid) == before
    assert h.closes == 2 and h.loops[0] is h.loops[1] and h.loops[0] is not h.loops[2]
    assert all(loop.is_closed() for loop in h.loops)
    entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
    assert [[(e["request"], e["attempt"]) for e in entry["request_errors"]] for entry in entries] == [
        [(1, 1)], [(1, 1)]]


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("task", ["bid", "summary"])
def test_empty_complete_response_uses_one_output_correction(protocol, task):
    h = Harness(protocol, [reply(tool=False, text=""),
                           reply(tool=False, text="summary") if task == "summary" else reply()])
    runner = TaskRunner(h.factory)
    from tests.support.factories import TEST_GAME
    result = runner(task, "today", [], game=TEST_GAME, coins=6)
    if task == "summary":
        assert result.output == "summary"
    else:
        assert result.output.bid == 2
    assert len(h.requests) == 2 and h.closes == 1
    assert not result.request_errors


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_correction_then_network_exhaustion_audits_only_complete_reply(protocol, tmp_path):
    h = Harness(protocol, [reply(dict(GOOD, bid="2")), reply(broken=True, ending="missing"),
                           reply(broken=True, ending="missing")])
    player = make_player(TaskRunner(h.factory), tmp_path)
    with pytest.raises(CallFailedError):
        player.decide_bid(make_ai_obs())
    entry = json.loads(player.log_path.read_text(encoding="utf-8"))
    audit = ModelMessagesTypeAdapter.validate_python(entry["messages"])
    assert sum(isinstance(p, ToolCallPart) for m in audit for p in m.parts) == 1
    assert [(e["request"], e["attempt"]) for e in entry["request_errors"]] == [(2, 1), (2, 2)]
    markdown = player.log_path.with_suffix(".md").read_text(encoding="utf-8")
    assert "网络尝试 3 次（请求 1：1 次；请求 2：2 次）" in markdown
    assert "输出纠错 1 次，最终失败" in markdown
    assert len(h.requests) == 3 and len(player.messages) == 1 and h.closes == 1


def test_non_transport_model_api_error_is_not_retryable():
    from pydantic_ai.exceptions import ModelAPIError
    assert not retryable_request_error(ModelAPIError("alias", "provider configuration failure"))


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_player_task_chain_preserves_tool_history_on_wire(protocol, tmp_path):
    long_summary = "观众总结" * 200
    h = Harness(protocol, [reply(), reply(), reply({"strategy": "private", "speech": "public"}),
                           reply(tool=False, text=long_summary)])
    player = make_player(TaskRunner(h.factory), tmp_path)
    assert player.plan_strategy("plan", "opening_plan") is None
    assert player.decide_bid(make_ai_obs()) == (2, None)
    assert len(h.requests) == 1
    assert player.decide_bid(make_ai_obs(day=2)) == (2, None)
    assert player.give_speech("speak", "final_speech") == "public"
    before = deepcopy(player.messages)
    assert player.final_summary("original summary request and other players' speeches") == long_summary
    assert player.messages == before and len(h.requests) == h.closes == 4
    body = json.dumps(h.requests[-1][1], ensure_ascii=False)
    assert body.count("【你的身份】") == 1 and "original summary request" in body
    assert "private" in body and "other players' speeches" in body
    names = ["plan", "bid", "speech"]
    for index, (_, request_body) in enumerate(h.requests):
        encoded_body = json.dumps(request_body, ensure_ascii=False)
        assert encoded_body.count("【你的身份】") == 1
        tools = request_body.get("tools", [])
        assert [t.get("function", t)["name"] for t in tools] == names[index:index + 1]
        if protocol == "chat":
            messages = request_body["messages"]
            calls = [(c["id"], c["function"]["name"]) for m in messages for c in m.get("tool_calls", [])]
            returns = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
        elif protocol == "responses":
            items = request_body["input"]
            calls = [(p["call_id"], p["name"]) for p in items if p.get("type") == "function_call"]
            returns = [p["call_id"] for p in items if p.get("type") == "function_call_output"]
        else:
            parts = [p for m in request_body["messages"] for p in m["content"] if isinstance(p, dict)]
            calls = [(p["id"], p["name"]) for p in parts if p["type"] == "tool_use"]
            returns = [p["tool_use_id"] for p in parts if p["type"] == "tool_result"]
        assert calls == [(f"call_{i + 1}", name) for i, name in enumerate(names[:index])]
        assert returns == [call_id for call_id, _ in calls]
    assert body.index('"plan"') < body.index("当前是第 1 局第 2 天") < body.index('"speak"')
    assert "public" in body
    parts = [p for m in before for p in m.parts]
    assert [p.tool_call_id for p in parts if isinstance(p, ToolCallPart)] == [
        p.tool_call_id for p in parts if isinstance(p, ToolReturnPart)]
    encoded = ModelMessagesTypeAdapter.dump_json(before)
    assert ModelMessagesTypeAdapter.dump_json(ModelMessagesTypeAdapter.validate_json(encoded)) == encoded
    entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
    assert [e["task"] for e in entries] == ["plan", "bid", "speech", "summary"]
    assert entries[-1]["result"] == long_summary
    assert long_summary in player.log_path.with_suffix(".md").read_text(encoding="utf-8")
    assert all(s.closed for s in h.streams)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_factory_configuration_drives_wire_request(protocol, monkeypatch):
    from auction_game.ai import llm_client
    channel = {"base_url": "https://configured.invalid/custom", "api_key_env": "TEST_WIRE_KEY"}
    monkeypatch.setattr(llm_client, "load_dotenv", lambda path: None)
    monkeypatch.setenv(channel["api_key_env"], "fake-key")
    h = Harness(protocol, [reply(tool=False)])
    factory = llm_client.make_model_factory(provider="channel-name-is-not-protocol", channel=channel,
                                           model="custom-model-alias", protocol=protocol,
                                           timeout=17, max_tokens=321,
                                           model_settings={"openai_reasoning_effort": "high"} if protocol != "anthropic" else {})
    assert factory.base_url == channel["base_url"]
    factory = replace(factory, base_url=h.factory.base_url, transport=h.factory.transport)
    assert TaskRunner(factory)("summary", "ping", []).output == "no tool"
    request, body = h.requests[0]
    assert body["model"] == "custom-model-alias"
    assert body["max_output_tokens" if protocol == "responses" else "max_tokens"] == 321
    if protocol == "responses":
        assert body["reasoning"]["effort"] == "high"
    elif protocol == "chat":
        assert body["reasoning_effort"] == "high"
    else:
        assert "thinking" not in body and "reasoning_effort" not in body
    assert request.extensions["timeout"]["read"] == 17 and h.closes == 1


@pytest.mark.parametrize("provider,protocol,choice", [
    ("hub", "responses", "auto"), ("hub", "chat", "required"),
    ("other", "responses", "required"),
])
def test_channel_profile_only_changes_configured_responses(provider, protocol, choice, monkeypatch):
    import asyncio
    from auction_game.ai import llm_client
    from auction_game.reporting.replay import capture_protocol_definition
    monkeypatch.setattr(llm_client, "_require_env", lambda *args: "fake-key")
    if protocol == "responses":
        # 模拟框架已推断的公开能力，验证局部覆盖仍保留 context 与 include。
        monkeypatch.setattr(llm_client.OpenAIProvider, "model_profile", lambda self, model_name: {
            "context_window": 1000, "openai_supports_encrypted_reasoning_content": True})
    channel = deepcopy(CHANNELS["hub"])
    if provider == "other":
        channel.pop("responses_profile")
    h = Harness(protocol, [reply()])
    factory = llm_client.make_model_factory(
        provider=provider, channel=channel, model=h.factory.model, protocol=protocol,
        timeout=3, max_tokens=123, model_settings={"openai_reasoning_effort": "high"})
    factory = replace(factory, transport=h.factory.transport)
    result = TaskRunner(factory, capture_requests=True)("bid", "today", [], game=TEST_GAME, coins=6)
    assert result.output.bid == 2
    body = h.requests[0][1]
    assert body["tool_choice"] == choice and body["tools"]
    assert (body["reasoning"]["effort"] if protocol == "responses" else body["reasoning_effort"]) == "high"
    async def capture():
        if protocol == "responses":
            async with factory.open() as model:
                assert model.profile["context_window"] == 1000
                assert model.profile["openai_supports_encrypted_reasoning_content"] is True
        return await capture_protocol_definition(factory, result.requests[0])
    snapshot = asyncio.run(capture())
    assert snapshot["definition"]["tool_choice"] == choice
    assert snapshot["model_profile"] == factory.model_profile
    if protocol == "responses":
        assert body["include"] == ["reasoning.encrypted_content"]


@pytest.mark.parametrize("response,status", [(reply({"reply": "OK"}), 0),
                                            (503, 1), (reply({"reply": ""}), 1)])
def test_probe_single_streamed_tool_request_never_retries(response, status, monkeypatch, capsys):
    from auction_game.cli import check_llm
    h = Harness("responses", [response])
    pc = dict(model_key="fake", provider="fake", model="alias", protocol="responses", max_tokens=4096,
              model_settings={"openai_reasoning_effort": "high"},
              capabilities={"reasoning_status": "high", "reasoning_detail": "synthetic"})
    monkeypatch.setattr(check_llm, "load_configuration", lambda: (
         {"llm": {"timeout_seconds": 3, "channels": {"fake": CHANNELS["hub"]}}},
        [pc, pc],
    ))
    def factory(**kwargs):
        assert kwargs["request_attempts"] == 1 and kwargs["max_tokens"] == 2048
        return replace(h.factory, request_attempts=kwargs["request_attempts"], max_tokens=kwargs["max_tokens"],
                       model_settings=kwargs["model_settings"],
                       model_profile=check_llm.channel_model_profile(kwargs["channel"], kwargs["protocol"]))
    monkeypatch.setattr(check_llm, "make_model_factory", factory)
    assert check_llm.main([]) == status
    assert len(h.requests) == h.closes == 1
    body = h.requests[0][1]
    assert body["stream"] is True and body["tools"][0]["name"] == "ping"
    assert body["reasoning"]["effort"] == "high"
    assert body["tool_choice"] == "auto"
    output = capsys.readouterr().out
    assert "比赛预算=4096" in output and "本次探测预算=2048" in output
    assert '"supports_forced_tool_choice": false' in output
    assert "须单独授权" not in output
