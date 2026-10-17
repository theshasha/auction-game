"""累计记忆、真实 SDK 送模隔离及跨局事务；全部离线。"""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter

from auction_game.ai import prompts
from auction_game.ai.agent_execution import OutputFailedError
from auction_game.ai.agent_tasks import TaskRunner
from auction_game.ai.ai_speech import AISpeechDirector
from auction_game.controller import Controller
from tests.support.factories import (bid_reply, fake_task_runner, make_ai_obs, make_log,
                                     make_player, make_standings, speech_reply)
from tests.support.wire import Harness, PROTOCOLS, reply


def encoded(messages):
    return ModelMessagesTypeAdapter.dump_json(messages).decode()


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_cleaned_history_preserves_phase_and_tool_pairs_without_mutating_audit(protocol):
    from pydantic_ai.messages import (ModelRequest, ModelResponse, TextPart, ThinkingPart,
                                     ToolCallPart, ToolReturnPart, UserPromptPart)
    from auction_game.ai.ai_player import completed_history
    raw = [ModelRequest(parts=[UserPromptPart("old task")]), ModelResponse(parts=[
        ThinkingPart("secret", signature="old_signature_value", provider_name="openai"),
        TextPart("working", id="msg_old", provider_name="openai",
                 provider_details={"phase": "commentary", "other": "discard"}),
        ToolCallPart("bid", json.dumps(bid_reply(2)), tool_call_id="call_old|fc_old",
                     id="fc_old", provider_name="openai"),
    ], provider_name="openai", provider_response_id="resp_old"),
        ModelRequest(parts=[ToolReturnPart("bid", "success", tool_call_id="call_old|fc_old")])]
    before = encoded(raw)
    cleaned = completed_history(raw)
    assert encoded(raw) == before
    assert cleaned[1].parts[0].provider_details == {"phase": "commentary"}
    h = Harness(protocol, [reply()])
    if protocol == "responses":
        h.factory = replace(h.factory, model_profile={"openai_supports_phase": True,
                             "openai_supports_encrypted_reasoning_content": True})
    TaskRunner(h.factory)("bid", "new task", cleaned, game=make_ai_obs().game, coins=6)
    wire = json.dumps(h.requests[0][1])
    for marker in ("secret", "old_signature_value", "msg_old", "fc_old", "resp_old", "discard"):
        assert marker not in wire
    assert wire.count("call_old") == 2
    if protocol == "responses":
        assert '"phase": "commentary"' in wire


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("auto", [False, True])
def test_real_sdk_keeps_thinking_in_audit_only_across_tasks(protocol, auto, tmp_path):
    thinking = "OLD_PRIVATE_THINKING" * 1000
    h = Harness(protocol, [reply(dict(bid_reply("bad")), thinking=thinking),
                           reply(bid_reply(2), thinking=thinking), reply(bid_reply(1)),
                           reply(tool=False, text="audience")])
    if protocol == "responses":
        h.factory = replace(h.factory, model_profile={"openai_supports_encrypted_reasoning_content": True},
                            model_settings={"openai_previous_response_id": "auto"} if auto else {})
    player = make_player(TaskRunner(h.factory), tmp_path)
    player.plan_strategy("original task", "opening_plan")
    assert len(h.requests) == 2
    # SDK 自行重放任务内纠错：thinking 与原工具配对保持完整。
    correction = json.dumps(h.requests[1][1])
    if protocol != "responses" or not auto:
        assert "OLD_PRIVATE_THINKING" in correction
    assert "call_1" in correction
    if protocol == "responses" and auto:
        assert "resp_1" in correction  # 当前任务 SDK auto 链接仍有效
    elif protocol == "responses":
        assert "encrypted_old_reasoning" in correction and "rs_old" in correction
    audit = player.log_path.read_text(encoding="utf-8")
    assert thinking in audit and "thinking" in audit
    if protocol == "responses":
        assert "encrypted_old_reasoning" in audit and "resp_1" in audit
    if protocol == "anthropic":
        assert "signed_old_reasoning" in audit and "redacted_old_reasoning" in audit
    player.decide_bid(make_ai_obs())  # 消费首日缓存
    player.decide_bid(make_ai_obs(day=2))
    before = player.checkpoint()
    assert player.final_summary("audience review") == "audience"
    assert player.checkpoint() == before
    for _, body in h.requests[2:]:
        wire = json.dumps(body, ensure_ascii=False)
        assert "OLD_PRIVATE_THINKING" not in wire
        for marker in ("encrypted_old_reasoning", "signed_old_reasoning", "redacted_old_reasoning",
                       "rs_old", "resp_1", "previous_response_id", "fc_1", "fc_2"):
            assert marker not in wire
        assert "original task" in wire
        from pydantic_ai.messages import ToolCallPart
        assert any(p.args_as_dict().get("strategy") == "我的私有分析"
                   for m in player.messages for p in m.parts if isinstance(p, ToolCallPart))
        assert "call_1" in wire and "call_2" in wire
        assert len(wire) < len(thinking)  # 旧长推理不随独立任务累积


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_sdk_memory_tool_and_cross_match_replacement(protocol):
    h = Harness(protocol, [reply(speech_reply("old public signal")), reply({"memory": "cumulative-v1"}),
                           reply(bid_reply(1)), reply({"memory": "cumulative-v2"}), reply(bid_reply(2))])
    player = make_player(TaskRunner(h.factory))
    player.give_speech("old match raw", "match_1_speech")
    player.save_memory(prompts.memory_request({"Bravo": "other signal"}), "match_1_memory")
    body = h.requests[-1][1]
    tool = body["tools"][0].get("function", body["tools"][0])
    assert tool["name"] == "save_memory"
    schema = tool.get("parameters", tool.get("input_schema"))
    assert schema["required"] == ["memory"] and set(schema["properties"]) == {"memory"}
    assert schema["additionalProperties"] is False
    player.plan_strategy(prompts.next_match_plan_request([], player.game, 2), "match_1_plan")
    wire = json.dumps(h.requests[-1][1], ensure_ascii=False)
    assert "cumulative-v1" in wire and "最新累计成绩" in wire and "第 2 局" in wire
    assert "old match raw" not in wire and "old public signal" not in wire and "other signal" not in wire
    player.save_memory("second match signals", "match_2_memory")
    assert "cumulative-v1" in json.dumps(h.requests[-1][1])
    player.plan_strategy(prompts.next_match_plan_request([], player.game, 3), "match_2_plan")
    wire = json.dumps(h.requests[-1][1], ensure_ascii=False)
    assert "cumulative-v2" in wire and "cumulative-v1" not in wire
    assert "second match signals" not in wire and len(h.requests) == h.closes == 5


def test_cumulative_memory_replaces_prior_context_but_review_preserves_game(tmp_path):
    long_memory = "第二版累计经验" + "观察与猜测。" * 2500
    runner = fake_task_runner([speech_reply("own raw signal"), {"memory": "第一版经验"},
                               bid_reply(1), speech_reply("match two signal"), {"memory": long_memory},
                               bid_reply(2), "review"])
    player = make_player(runner, tmp_path)
    player.give_speech("first match raw", "opening_speech")
    checkpoint = player.checkpoint()
    player.save_memory(prompts.memory_request({"Bravo": "other raw signal"}), "match_1_memory")
    assert "own raw signal" in encoded(runner.calls[-1]) and "other raw signal" in encoded(runner.calls[-1])
    assert player.memory == "第一版经验"
    assert "first match raw" not in encoded(player.messages)
    standings = make_standings({"Alpha": [3], "Bravo": [1]})
    player.plan_strategy(prompts.next_match_plan_request(standings, player.game, 2), "match_1_plan")
    second_input = encoded(runner.calls[-1])
    assert "第一版经验" in second_input and "最新累计成绩" in second_input
    assert "own raw signal" not in second_input and "first match raw" not in second_input
    player.give_speech("second match raw", "match_2_speech")
    player.save_memory("all second match signals", "match_2_memory")
    assert "第一版经验" in encoded(runner.calls[-1]) and "second match raw" in encoded(runner.calls[-1])
    assert player.memory == long_memory and "第一版经验" not in encoded(player.messages)
    player.plan_strategy(prompts.next_match_plan_request(standings, player.game, 3), "match_2_plan")
    third_input = encoded(runner.calls[-1])
    assert long_memory in third_input and "second match raw" not in third_input
    assert "第一版经验" not in third_input and len(runner.calls) == 6  # 超软目标只请求一次
    player.final_summary("review all matches")
    assert "first match raw" in encoded(runner.calls[-1]) and "second match raw" in encoded(runner.calls[-1])
    entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
    assert entries[4]["memory_chars"] == len(long_memory)
    assert "累计私有记忆" in player.log_path.with_suffix(".md").read_text(encoding="utf-8")
    player.rollback(checkpoint)
    assert player.memory == "" and player.checkpoint() == checkpoint


@pytest.mark.parametrize("invalid", [{"memory": " "}, {"memory": 1}, {}, {"memory": "ok", "strategy": "extra"}])
def test_memory_uses_strict_nonempty_schema_and_shared_correction(invalid):
    runner = fake_task_runner([invalid, {"memory": "saved"}])
    player = make_player(runner)
    player.save_memory("整理", "match_1_memory")
    assert player.memory == "saved" and len(runner.calls) == 2


@pytest.mark.parametrize("fault", ["output", ".jsonl", ".md"])
def test_failed_memory_does_not_commit_context_review_cache_or_memory(fault, tmp_path, monkeypatch):
    replies = [{"memory": " "}] * 2 if fault == "output" else [{"memory": "new"}]
    player = make_player(fake_task_runner([bid_reply(2), *replies]), tmp_path)
    player.plan_strategy("old context", "opening_plan")
    before = deepcopy(player.checkpoint())
    if fault != "output":
        original_open = Path.open

        def open_file(path, mode="r", *args, **kwargs):
            if path == player.log_path.with_suffix(fault) and mode == "a":
                raise OSError("disk")
            return original_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(OutputFailedError if fault == "output" else OSError):
        player.save_memory("整理", "match_1_memory")
    assert player.checkpoint() == before


@pytest.mark.parametrize("playoff", [False, True])
def test_director_publishes_all_speeches_before_private_memory_and_plan(tmp_path, playoff):
    players = []
    for name in ("Alpha", "Bravo"):
        def memory(messages, name=name):
            public_log = (tmp_path / "speeches.md").read_text(encoding="utf-8")
            assert "public-Alpha" in public_log and "public-Bravo" in public_log
            text = encoded(messages)
            assert "public-Alpha" in text and "public-Bravo" in text
            # 自己的发言只在历史中出现一次，新提示仅补充其他玩家。
            assert text.count("public-" + name) == 1
            prompt = messages[-1].parts[-1].content
            assert "public-" + name not in prompt
            assert "public-" + ("Bravo" if name == "Alpha" else "Alpha") in prompt
            assert "private-" not in text
            return {"memory": "private-" + name}
        p = make_player(fake_task_runner([speech_reply("public-" + name), memory, bid_reply(1)]))
        p.name = name
        players.append(p)
    director = AISpeechDirector(players, Controller(mode="continuous", announce=lambda _: None), tmp_path, game=players[0].game)
    director.on_match_end(make_log(is_playoff=playoff), make_standings({"Alpha": [2], "Bravo": [0]}), True)
    for p in players:
        text = encoded(p.task_runner.calls[-1])
        assert "private-" + p.name in text
        assert "private-" + ("Bravo" if p.name == "Alpha" else "Alpha") not in text
        assert "public-Alpha" not in text and "public-Bravo" not in text
        assert len(p.task_runner.calls) == 3
    assert "private-" not in (tmp_path / "speeches.md").read_text(encoding="utf-8")


def test_final_match_skips_memory_and_next_plan(tmp_path):
    player = make_player(fake_task_runner([speech_reply("last speech")]))
    director = AISpeechDirector([player], Controller(mode="continuous", announce=lambda _: None), tmp_path, game=player.game)
    director.on_match_end(make_log(), [], False)
    assert len(player.task_runner.calls) == 1 and player.memory == ""
