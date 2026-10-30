"""累计记忆、thinking 只留在审计里，以及跨局的事务和回滚。"""

from copy import deepcopy
from dataclasses import replace
import json

import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.providers.openai import OpenAIProvider

from auction_game.ai import prompts
from auction_game.ai.agent_execution import OutputFailedError
from auction_game.ai.agent_tasks import TaskRunner
from auction_game.ai.ai_director import AIDirector
from tests.support.factories import (TEST_GAME, bid_reply, continuous_controller, fake_task_runner, make_ai_obs,
                                     make_log, make_logs, make_player, make_standings, request_text, speech_reply,
                                     task_entries)
from tests.support.wire import Harness, PROTOCOLS, reply


def encoded(messages):
    return ModelMessagesTypeAdapter.dump_json(messages).decode()


def extend_openai_profile(monkeypatch, **overrides):
    original = OpenAIProvider.model_profile
    monkeypatch.setattr(OpenAIProvider, "model_profile", staticmethod(lambda name: {**original(name), **overrides}))


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_cleaned_history_preserves_phase_without_mutating_audit(protocol, monkeypatch):
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ThinkingPart, UserPromptPart
    from auction_game.ai.ai_player import completed_history
    raw = [ModelRequest(parts=[UserPromptPart("old task")]), ModelResponse(parts=[
        ThinkingPart("secret", signature="old_signature_value", provider_name="openai"),
        TextPart("working", id="msg_old", provider_name="openai",
                 provider_details={"phase": "commentary", "other": "discard"}),
    ], provider_name="openai", provider_response_id="resp_old")]
    before = encoded(raw)
    cleaned = completed_history(raw)
    assert encoded(raw) == before
    assert cleaned[1].parts[0].provider_details == {"phase": "commentary"}
    h = Harness(protocol, [reply()])
    extend_openai_profile(monkeypatch, openai_supports_phase=True,
                          openai_supports_encrypted_reasoning_content=True)
    TaskRunner(h.factory)("bid", "new task", cleaned, game=make_ai_obs().game, coins=6)
    wire = json.dumps(h.requests[0][1])
    for marker in ("secret", "old_signature_value", "msg_old", "resp_old", "discard"):
        assert marker not in wire
    if protocol == "responses":
        assert '"phase": "commentary"' in wire


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("auto", [False, True])
def test_real_sdk_keeps_thinking_in_audit_only_across_tasks(protocol, auto, tmp_path, monkeypatch):
    thinking = "OLD_PRIVATE_THINKING" * 1000
    h = Harness(protocol, [reply(dict(bid_reply("bad")), thinking=thinking),
                           reply(bid_reply(2), thinking=thinking), reply(bid_reply(1)),
                           reply(text="audience")])
    if protocol == "responses":
        extend_openai_profile(monkeypatch, openai_supports_encrypted_reasoning_content=True)
        h.factory = replace(h.factory, model_settings={"openai_previous_response_id": "auto"} if auto else {})
    player = make_player(TaskRunner(h.factory), tmp_path)
    player.plan_strategy("original task", "opening_plan")
    assert len(h.requests) == 2
    # SDK 自行重放任务内纠错：thinking 与错误输出原文保持完整。
    correction = json.dumps(h.requests[1][1])
    if protocol != "responses" or not auto:
        assert "OLD_PRIVATE_THINKING" in correction
    if protocol != "responses" or not auto:
        assert "bad" in correction and "<decision>" in correction
    if protocol == "responses" and auto:
        assert "resp_1" in correction  # 当前任务 SDK auto 链接仍有效
    elif protocol == "responses":
        assert "encrypted_old_reasoning" in correction and "rs_old" in correction
    audit = player.log.path.read_text(encoding="utf-8")
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
                       "rs_old", "resp_1", "previous_response_id"):
            assert marker not in wire
        assert "original task" in wire
        from pydantic_ai.messages import TextPart
        assert any("我的私有分析" in p.content
                   for m in player.messages for p in m.parts if isinstance(p, TextPart))
        assert "bad" in wire and "<decision>" in wire
        assert len(wire) < len(thinking)  # 旧长推理不随独立任务累积


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_sdk_plain_memory_and_cross_match_replacement(protocol, tmp_path):
    h = Harness(protocol, [reply(speech_reply("old public signal")), reply({"memory": "cumulative-v1"}),
                           reply(bid_reply(1)), reply({"memory": "cumulative-v2"}), reply(bid_reply(2))])
    player = make_player(TaskRunner(h.factory), tmp_path)
    player.give_speech("old match raw", "match_1_speech")
    player.save_memory(prompts.memory_request({"Bravo": "other signal"}, 1), "match_1_memory")
    assert "不加格式标签或 JSON 外壳" in json.dumps(h.requests[-1][1], ensure_ascii=False)
    assert player.memory == "cumulative-v1"
    player.plan_strategy(prompts.next_match_plan_request(make_logs(1), [], player.game), "match_1_plan")
    wire = json.dumps(h.requests[-1][1], ensure_ascii=False)
    assert "cumulative-v1" in wire and "最新累计成绩" in wire and "第 2 局" in wire
    assert "old match raw" not in wire and "old public signal" not in wire and "other signal" not in wire
    player.save_memory("second match signals", "match_2_memory")
    assert "cumulative-v1" in json.dumps(h.requests[-1][1])
    player.plan_strategy(prompts.next_match_plan_request(make_logs(2), [], player.game), "match_2_plan")
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
    player.save_memory(prompts.memory_request({"Bravo": "other raw signal"}, 1), "match_1_memory")
    assert "own raw signal" in encoded(runner.calls[-1]) and "other raw signal" in encoded(runner.calls[-1])
    assert player.memory == "第一版经验"
    assert "first match raw" not in encoded(player.messages)
    standings = make_standings({"Alpha": [3], "Bravo": [1]})
    player.plan_strategy(prompts.next_match_plan_request(make_logs(1), standings, player.game), "match_1_plan")
    second_input = encoded(runner.calls[-1])
    assert "第一版经验" in second_input and "最新累计成绩" in second_input
    assert "own raw signal" not in second_input and "first match raw" not in second_input
    player.give_speech("second match raw", "match_2_speech")
    player.save_memory("all second match signals", "match_2_memory")
    assert "第一版经验" in encoded(runner.calls[-1]) and "second match raw" in encoded(runner.calls[-1])
    assert player.memory == long_memory and "第一版经验" not in encoded(player.messages)
    player.plan_strategy(prompts.next_match_plan_request(make_logs(2), standings, player.game), "match_2_plan")
    third_input = encoded(runner.calls[-1])
    assert long_memory in third_input and "second match raw" not in third_input
    assert "第一版经验" not in third_input and len(runner.calls) == 6  # 超软目标只请求一次
    player.final_summary("review all matches")
    assert "first match raw" in encoded(runner.calls[-1]) and "second match raw" in encoded(runner.calls[-1])
    entries = task_entries(player.log.path)
    assert entries[4]["history_reset"] == f"【累计私有记忆，仅自己可见】\n{long_memory}"
    markdown = player.log.readable_path.read_text(encoding="utf-8")
    assert markdown.count("此后对话从系统提示和一条消息重新开始") == 2
    assert "这次请求带的是从开局到现在的全部对话" in markdown
    player.rollback(checkpoint)
    assert player.memory == "" and player.checkpoint() == checkpoint


@pytest.mark.parametrize("invalid", ["", "\r\n\t "])
def test_memory_requires_nonempty_plain_body_and_shared_correction(invalid, tmp_path):
    runner = fake_task_runner([invalid, {"memory": "saved"}])
    player = make_player(runner, tmp_path)
    player.save_memory("整理", "match_1_memory")
    assert player.memory == "saved" and len(runner.calls) == 2


def test_failed_memory_keeps_context_review_cache_and_memory(tmp_path):
    player = make_player(fake_task_runner([bid_reply(2), {"memory": " "}, {"memory": " "}]), tmp_path)
    player.plan_strategy("old context", "opening_plan")
    before = deepcopy(player.checkpoint())
    with pytest.raises(OutputFailedError):
        player.save_memory("整理", "match_1_memory")
    assert player.checkpoint() == before


@pytest.mark.parametrize("match_index, playoff", [(1, False), (2, False), (3, True)],
                         ids=["next-regular", "first-playoff", "next-playoff"])
def test_director_publishes_all_speeches_before_private_memory_and_plan(tmp_path, match_index, playoff):
    game = replace(TEST_GAME, initial_coins=13, daily_pool=(2, 1, 3), max_bid=9,
                   super_hit_score=2, regular_matches=2)
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
        players.append(make_player(fake_task_runner([speech_reply("public-" + name), memory, bid_reply(1, 6),
                                                     bid_reply(0)]), tmp_path / name, game=game, name=name))
    director = AIDirector(players, continuous_controller(), tmp_path, game=players[0].game)
    # Bravo 拍卖分较少但命中后总分较高；规划必须读取累计总分。下一局是加赛时两人总分并列。
    next_is_playoff = match_index >= game.regular_matches
    standings = make_standings({"Alpha": [2] * match_index, "Bravo": [1] * match_index},
                              scores_history={"Alpha": [3 if next_is_playoff else 2] * match_index,
                                              "Bravo": [3] * match_index})
    logs = [*make_logs(match_index - 1, game=game), make_log(match_index=match_index, is_playoff=playoff, game=game)]
    director.on_match_end(logs, standings)
    for p in players:
        text = encoded(p.task_runner.calls[-1])
        assert "private-" + p.name in text
        assert "private-" + ("Bravo" if p.name == "Alpha" else "Alpha") not in text
        assert "public-Alpha" not in text and "public-Bravo" not in text
        assert len(p.task_runner.calls) == 3
        plan_input = request_text(p.task_runner, -1)
        for entry in standings:
            row = next(line for line in plan_input.splitlines() if f"| {entry.player} |" in line)
            assert row.split(" | ")[:3] == [f"| {entry.rank}", entry.player, str(entry.total_score)]
        assert plan_input.startswith(f"第 {match_index + 1} 局是加赛局。目前第一名总分并列：Alpha、Bravo各 "
                                     f"{3 * match_index} 分。") is next_is_playoff
        for fact in (f"第 {match_index + 1} 局第 1 天的出价", "你有 13 枚金币",
                     "今日奖池为 2 分", "本局还剩 3 天（含今天），剩余拍卖奖池共 6 分",
                     "如果每天都命中，每人最多还能获得 6 命中分"):
            assert fact in plan_input
        obs = replace(make_ai_obs(game=game, coins=game.initial_coins), match_index=match_index + 1)
        assert p.decide_bid(obs) == (1, 6)
        assert len(p.task_runner.calls) == 3
        assert p.decide_bid(obs) == (0, None)
        assert len(p.task_runner.calls) == 4
    assert "private-" not in (tmp_path / "speeches.md").read_text(encoding="utf-8")
