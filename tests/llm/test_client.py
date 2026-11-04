"""生产 TaskRunner 的 HTTP 异常分类、流隔离与客户端生命周期。"""

import asyncio
from dataclasses import replace
from copy import deepcopy
import json
import threading
import time

import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter, TextPart
from pydantic_ai.models.function import FunctionModel

from auction_game.llm import client as llm_client
from auction_game.llm.execution import CallFailedError, OutputFailedError, TaskCancelledError
from auction_game.agent.tasks import TASKS, TaskRunner
from auction_game.llm.client import BufferedRequest, RunSignals, wait_before_retry
from tests.support.factories import CHANNELS, TEST_GAME, make_player, make_ai_obs, text_reply, task_entries
from tests.support.wire import Harness, PROTOCOLS, make_agent, reply, run, sse, GOOD


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("status", [400, 401, 422, 429, 503])
def test_any_status_is_resent_three_times_and_client_closes(protocol, status, tmp_path):
    h = Harness(protocol, [status, status, status, reply()])
    player = make_player(TaskRunner(h.factory), tmp_path)
    assert player.bid(make_ai_obs(), {}, "第 1 局第 2 天出价").submission == (2, None)
    assert len(h.requests) == 4
    assert h.closes == 1 and all(s.closed for s in h.streams)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("fault", ["connect", "stream", "limit", "missing", 401])
def test_failed_flow_keeps_history_and_audits_no_partial_reply(protocol, fault, tmp_path):
    item = (fault if fault in ("connect", 401) else reply(broken=True, ending="missing") if fault == "stream"
            else reply(ending=fault))
    h = Harness(protocol, [deepcopy(item) for _ in range(4)])
    player = make_player(TaskRunner(h.factory), tmp_path)
    before = deepcopy(player.messages)
    with pytest.raises(CallFailedError):
        player.bid(make_ai_obs(), {}, "第 1 局第 2 天出价")
    assert player.messages == before
    assert len(h.requests) == 4
    entry = task_entries(player.log.path)[-1]
    audit = ModelMessagesTypeAdapter.validate_python(entry["messages"])
    assert not any(isinstance(p, TextPart) for m in audit for p in m.parts)
    assert entry["request_errors"] and h.closes == 1
    assert all(s.closed for s in h.streams)


def test_anthropic_delta_without_usage_is_resent(tmp_path):
    empty = [chunk for chunk in sse("anthropic", text="") if b"content_block" not in chunk]
    empty = [chunk.replace(b', "usage": {"output_tokens": 3}', b"") for chunk in empty]
    h = Harness("anthropic", [empty, reply()])
    player = make_player(TaskRunner(h.factory), tmp_path)
    assert player.bid(make_ai_obs(), {}, "第 1 局第 2 天出价").submission == (2, None)
    assert len(h.requests) == 2 and h.requests[0][1] == h.requests[1][1]
    errors = task_entries(player.log.path)[-1]["request_errors"]
    assert [(e["request"], e["attempt"], e["type"]) for e in errors] == [(1, 1, "IncompleteStream")]
    assert h.closes == 1 and all(s.closed for s in h.streams)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("succeeds", [True, False], ids=["recovers", "output-budget-exhausted"])
def test_mixed_network_and_output_budget_reaches_at_most_four_attempts(protocol, succeeds, tmp_path):
    final = reply() if succeeds else reply(dict(GOOD, bid=99))
    h = Harness(protocol, ["connect", reply(dict(GOOD, bid="2")),
                           reply(broken=True, ending="missing"), final])
    player = make_player(TaskRunner(h.factory), tmp_path)
    before = deepcopy(player.messages)
    if succeeds:
        assert player.bid(make_ai_obs(), {}, "第 1 局第 2 天出价").submission == (2, None)
        parts = [p for m in player.messages for p in m.parts]
        assert [p.content for p in parts if isinstance(p, TextPart)] == [
            text_reply(dict(GOOD, bid="2")), text_reply(GOOD)]
    else:
        with pytest.raises(OutputFailedError):
            player.bid(make_ai_obs(), {}, "第 1 局第 2 天出价")
        assert player.messages == before
    assert len(h.requests) == 4 and h.closes == 1
    assert h.requests[0][1] == h.requests[1][1] and h.requests[2][1] == h.requests[3][1]
    assert h.requests[0][1] != h.requests[2][1]
    entry = task_entries(player.log.path)[-1]
    if succeeds:
        assert sum(p["part_kind"] == "text" for m in entry["messages"] for p in m["parts"]) == 2
    assert [(e["request"], e["attempt"]) for e in entry["request_errors"]] == [(1, 1), (2, 1)]
    markdown = player.log.readable_path.read_text(encoding="utf-8")
    assert f"纠错 1 次，{'已通过' if succeeds else '仍失败'}" in markdown
    assert all(s.closed for s in h.streams)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_success_then_failure_in_new_loop_keeps_previous_state_and_closes(protocol, tmp_path):
    h = Harness(protocol, ["connect", reply(), 400, 400, 400, 400])
    player = make_player(TaskRunner(h.factory), tmp_path)
    player.plan_strategy("plan", "第 1 局策略规划（含首日出价）")
    before = deepcopy(player.messages)
    with pytest.raises(CallFailedError):
        player.give_speech("speak", "开局发言")
    assert player.messages == before
    assert h.closes == 2 and h.loops[0] is h.loops[1] and h.loops[0] is not h.loops[2]
    assert all(loop.is_closed() for loop in h.loops)
    entries = task_entries(player.log.path)
    assert [[(e["request"], e["attempt"]) for e in entry["request_errors"]] for entry in entries] == [
        [(1, 1)], [(1, 1), (1, 2), (1, 3), (1, 4)]]


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("task", ["bid", "summary"])
def test_empty_complete_response_uses_one_output_correction(protocol, task):
    h = Harness(protocol, [reply(text=""),
                           reply(text="summary") if task == "summary" else reply()])
    runner = TaskRunner(h.factory)
    result = runner(TASKS[task], "today", [], game=TEST_GAME, coins=6)
    if task == "summary":
        assert result.output.text == "summary"
    else:
        assert result.output.bid == 2
    assert len(h.requests) == 2 and h.closes == 1
    assert not result.request_errors


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_correction_then_network_exhaustion_audits_only_complete_reply(protocol, tmp_path):
    h = Harness(protocol, [reply(dict(GOOD, bid="2"))] + [reply(broken=True, ending="missing")] * 4)
    player = make_player(TaskRunner(h.factory), tmp_path)
    with pytest.raises(CallFailedError):
        player.bid(make_ai_obs(), {}, "第 1 局第 2 天出价")
    entry = task_entries(player.log.path)[-1]
    audit = ModelMessagesTypeAdapter.validate_python(entry["messages"])
    assert [p.content for m in audit for p in m.parts if isinstance(p, TextPart)] == [
        text_reply(dict(GOOD, bid="2"))]
    assert [(e["request"], e["attempt"]) for e in entry["request_errors"]] == [(2, 1), (2, 2), (2, 3), (2, 4)]
    markdown = player.log.readable_path.read_text(encoding="utf-8")
    assert "纠错 1 次，仍失败" in markdown
    assert len(h.requests) == 5 and len(player.messages) == 1 and h.closes == 1


def test_each_resend_waits_retry_delay(monkeypatch, tmp_path):
    delays = []

    async def record(delay, signals):
        delays.append(delay)
        return True

    monkeypatch.setattr(llm_client, "wait_before_retry", record)
    h = Harness("chat", ["connect", 503, reply()])
    player = make_player(TaskRunner(replace(h.factory, retry_delay=10)), tmp_path)
    assert player.bid(make_ai_obs(), {}, "第 1 局第 2 天出价").submission == (2, None)
    assert delays == [10, 10] and len(h.requests) == 3


def test_program_error_is_not_resent():
    calls = []

    async def stream(messages, info):
        calls.append(messages)
        raise TypeError("bug in adapter")
        yield ""

    model = BufferedRequest(FunctionModel(stream_function=stream), attempts=4, retry_delay=0, signals=None)
    with pytest.raises(TypeError):
        asyncio.run(run(make_agent(model)))
    assert len(calls) == 1


def test_paused_failure_is_not_resent(tmp_path):
    signals = RunSignals()
    signals.paused.set()
    h = Harness("chat", ["connect", reply()])
    player = make_player(TaskRunner(replace(h.factory, signals=signals)), tmp_path)
    with pytest.raises(CallFailedError):
        player.bid(make_ai_obs(), {}, "第 1 局第 2 天出价")
    assert len(h.requests) == 1


def test_correction_is_still_sent_while_paused(tmp_path):
    signals = RunSignals()
    signals.paused.set()
    h = Harness("chat", [reply(dict(GOOD, bid="2")), reply()])
    player = make_player(TaskRunner(replace(h.factory, signals=signals)), tmp_path)
    assert player.bid(make_ai_obs(), {}, "第 1 局第 2 天出价").submission == (2, None)
    assert len(h.requests) == 2


def test_pause_ends_retry_wait_early():
    signals = RunSignals()
    threading.Timer(0.05, signals.paused.set).start()
    started = time.monotonic()
    assert asyncio.run(wait_before_retry(10, signals)) is False
    assert time.monotonic() - started < 2
    signals.paused.clear()
    assert asyncio.run(wait_before_retry(0, signals)) is True


@pytest.mark.parametrize("script,texts", [
    (["hang"], []),
    ([reply(dict(GOOD, bid="2")), "hang"], [text_reply(dict(GOOD, bid="2"))]),
    (["connect", reply()], []),
], ids=["in-flight", "during-correction", "during-retry-wait"])
def test_cancel_interrupts_task_and_logs_cancelled(script, texts, tmp_path):
    signals = RunSignals()
    h = Harness("chat", script)
    player = make_player(TaskRunner(replace(h.factory, retry_delay=10, signals=signals)), tmp_path)
    before = deepcopy(player.messages)
    threading.Timer(0.05, signals.cancelled.set).start()
    started = time.monotonic()
    with pytest.raises(TaskCancelledError):
        player.bid(make_ai_obs(), {}, "第 1 局第 2 天出价")
    assert time.monotonic() - started < 2
    assert player.messages == before and h.closes == 1
    entry = task_entries(player.log.path)[-1]
    assert entry["status"] == "cancelled" and "error" not in entry and "result" not in entry
    audit = ModelMessagesTypeAdapter.validate_python(entry["messages"])
    assert [p.content for m in audit for p in m.parts if isinstance(p, TextPart)] == texts
    assert len(entry["request_errors"]) == (1 if script[0] == "connect" else 0)
    markdown = player.log.readable_path.read_text(encoding="utf-8")
    assert markdown.rstrip().endswith("> 已取消：用户按 q 退出比赛，请求被中断。\n\n---")


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_player_task_chain_replays_native_text_history_on_wire(protocol, tmp_path):
    long_summary = "观众总结" * 200
    h = Harness(protocol, [reply(), reply(), reply({"strategy": "private", "speech": "public"}), reply(),
                           reply(text=long_summary)])
    player = make_player(TaskRunner(h.factory), tmp_path)
    assert player.plan_strategy("plan", "第 1 局策略规划（含首日出价）").submission == (2, None)
    assert player.bid(make_ai_obs(), {}, "第 1 局第 2 天出价").submission == (2, None)
    assert player.give_speech("speak", "最终感言").speech == "public"
    player.plan_strategy("next plan", "第 2 局策略规划（含首日出价）")
    before = deepcopy(player.messages)
    assert player.final_summary("original summary request and other players' speeches", "观众总结（仅观众可见）").text == long_summary
    assert player.messages == before and len(h.requests) == h.closes == 5
    body = json.dumps(h.requests[-1][1], ensure_ascii=False)
    assert body.count("【你的身份】") == 1 and "original summary request" in body
    assert "private" in body and "other players' speeches" in body
    outputs = [text_reply(GOOD), text_reply(GOOD),
               text_reply({"strategy": "private", "speech": "public"}), text_reply(GOOD)]
    for index, (_, request_body) in enumerate(h.requests):
        encoded_body = json.dumps(request_body, ensure_ascii=False)
        assert encoded_body.count("【你的身份】") == 1
        if index < 4:
            assert "<strategy>" in encoded_body
            assert ("<speech>" if index == 2 else "<decision>") in encoded_body
        if protocol == "responses":
            messages = [p for p in request_body["input"] if "role" in p]
        else:
            messages = request_body["messages"]
        assistant = [m for m in messages if m["role"] == "assistant"]
        assert len(assistant) == index
        for message, expected in zip(assistant, outputs):
            content = message["content"]
            actual = content if isinstance(content, str) else "".join(p.get("text", "") for p in content)
            assert actual == expected
        if protocol != "responses":
            assert any(m["role"] == "assistant" for m in messages) == (index > 0)
    assert body.index("plan\\n") < body.index("当前是第 1 局第 2 天") < body.index("speak\\n")
    assert "public" in body
    parts = [p for m in before for p in m.parts]
    assert [p.content for p in parts if isinstance(p, TextPart)] == outputs
    encoded = ModelMessagesTypeAdapter.dump_json(before)
    assert ModelMessagesTypeAdapter.dump_json(ModelMessagesTypeAdapter.validate_json(encoded)) == encoded
    entries = task_entries(player.log.path)
    assert [e["task"] for e in entries] == ["plan", "bid", "speech", "plan", "summary"]
    assert entries[-1]["result"] == long_summary
    assert long_summary in player.log.readable_path.read_text(encoding="utf-8")
    assert all(s.closed for s in h.streams)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_factory_configuration_drives_wire_request(protocol, monkeypatch):
    from auction_game.llm import client as llm_client
    channel = {"base_url": "https://configured.invalid/custom", "api_key_env": "TEST_WIRE_KEY"}
    monkeypatch.setattr(llm_client, "load_dotenv", lambda path: None)
    monkeypatch.setenv(channel["api_key_env"], "fake-key")
    h = Harness(protocol, [reply(text="configured reply")])
    factory = llm_client.make_model_factory(provider="channel-name-is-not-protocol", channel=channel,
                                           model="custom-model-alias", protocol=protocol,
                                           timeout=17, max_tokens=321,
                                           model_settings={"anthropic_thinking": {"type": "adaptive"},
                                                           "anthropic_effort": "high"} if protocol == "anthropic"
                                           else {"openai_reasoning_effort": "high"})
    assert factory.base_url == channel["base_url"]
    factory = replace(factory, base_url=h.factory.base_url, transport=h.factory.transport)
    assert TaskRunner(factory)(TASKS["summary"], "ping", [], game=TEST_GAME).output.text == "configured reply"
    request, body = h.requests[0]
    assert body["model"] == "custom-model-alias"
    assert body["max_output_tokens" if protocol == "responses" else "max_tokens"] == 321
    if protocol == "responses":
        assert body["reasoning"]["effort"] == "high"
    elif protocol == "chat":
        assert body["reasoning_effort"] == "high"
    else:
        assert body["thinking"] == {"type": "adaptive"} and body["output_config"]["effort"] == "high"
    assert request.extensions["timeout"]["read"] == 17 and h.closes == 1


@pytest.mark.parametrize("response,status", [(reply(text="OK"), 0),
                                             (503, 1), (reply(text=""), 1)])
def test_probe_single_streamed_text_request_never_retries(response, status, monkeypatch, capsys):
    from auction_game.cli import check_llm
    from auction_game.configuration import PlayerConfig, ResolvedModel
    from auction_game.game.lineup import PlayerProfile
    h = Harness("responses", [response])
    model = ResolvedModel(key="fake", provider="fake", model="alias", protocol="responses",
                          models_dev={"provider": "fake", "model": "alias"}, max_tokens=4096,
                          model_settings={"openai_reasoning_effort": "high"},
                          capabilities={"reasoning_status": "high", "reasoning_detail": "synthetic"})
    pc = PlayerConfig(PlayerProfile("甲", "人设", "正文", 0), model, model)
    monkeypatch.setattr(check_llm, "load_configuration", lambda **kwargs: (
         {"llm": {"timeout_seconds": 3, "channels": {"fake": CHANNELS["hub"]}}},
        [pc, pc],
    ))
    def factory(**kwargs):
        assert kwargs["request_attempts"] == 1 and kwargs["max_tokens"] == 2048
        return replace(h.factory, request_attempts=kwargs["request_attempts"], max_tokens=kwargs["max_tokens"],
                       model_settings=kwargs["model_settings"])
    monkeypatch.setattr(check_llm, "make_model_factory", factory)
    assert check_llm.main([]) == status
    assert len(h.requests) == h.closes == 1
    body = h.requests[0][1]
    assert body["stream"] is True
    assert body["reasoning"]["effort"] == "high"
    output = capsys.readouterr().out
    assert "比赛预算=4096" in output and "本次探测预算=2048" in output
