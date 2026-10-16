"""公开边界、规划缓存及观众总结；任务均经过真实 Agent。"""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter

from auction_game.ai import ai_speech
from auction_game.ai.agent_execution import OutputFailedError
from tests.support.factories import bid_reply, fake_task_runner, make_ai_obs, make_player, speech_reply
from auction_game.game.game_rules import GameSettings
from auction_game.game.players import ScriptedPlayer


def test_director_passes_game_to_both_plans(monkeypatch, tmp_path):
    game = GameSettings(13, 4, (2, 1, 3), 9, 1, 5, 2, 0)
    players = [ScriptedPlayer.constant(name, 0) for name in ("甲", "乙")]
    director = ai_speech.AISpeechDirector(players, None, tmp_path, game=game)
    plans = []

    def request(others, actual_game):
        assert actual_game is game
        plans.append(others)
        return "规划"

    monkeypatch.setattr(ai_speech.prompts, "opening_plan_request", request)
    monkeypatch.setattr(ai_speech.prompts, "next_match_plan_request", request)
    monkeypatch.setattr(ai_speech.prompts, "match_end_speech_request", lambda *args: "局末发言")
    monkeypatch.setattr(director, "_run_round", lambda *args: {"甲": "发言甲", "乙": "发言乙"})
    monkeypatch.setattr(director, "_publish_speeches", lambda *args: None)
    director.on_opening()
    director.on_match_end(SimpleNamespace(match_index=1), [], True)
    assert plans == [{"乙": "发言乙"}, {"甲": "发言甲"}] * 2
    director.on_match_end(SimpleNamespace(match_index=2), [], False)
    assert len(plans) == 4


def test_only_public_speech_returned_without_hard_truncation(tmp_path):
    speech = '第一段说"6 真香"。\n\n第二段。' + "很" * 600
    runner = fake_task_runner([speech_reply(speech)])
    player = make_player(runner, tmp_path)
    assert player.give_speech("请发言", "opening_speech") == speech
    assert len(runner.calls) == 1
    entry = json.loads(player.log_path.read_text(encoding="utf-8"))
    assert entry["result"] == speech_reply(speech)
    assert "烟雾弹" in ModelMessagesTypeAdapter.dump_json(player.messages).decode()
    md = player.log_path.with_suffix(".md").read_text(encoding="utf-8")
    assert speech in md and "私有策略" in md


@pytest.mark.parametrize("invalid", [speech_reply("  "), speech_reply(1),
                                          {"strategy": "  ", "speech": "public"}, "no tool"])
def test_speech_schema_failure_gets_one_correction(invalid):
    runner = fake_task_runner([invalid, speech_reply("补上的发言")])
    player = make_player(runner)
    assert player.give_speech("请发言", "opening_speech") == "补上的发言"
    assert len(runner.calls) == 2


def test_failed_speech_leaves_history_unchanged():
    runner = fake_task_runner(["private analysis", "more private"])
    player = make_player(runner)
    before = deepcopy(player.messages)
    with pytest.raises(OutputFailedError):
        player.give_speech("请发言", "opening_speech")
    assert player.messages == before


def test_failed_task_audit_after_prior_history_keeps_both_attempts(tmp_path):
    from pydantic_ai.messages import ToolCallPart
    runner = fake_task_runner([bid_reply(1), speech_reply(" "), speech_reply(" ")])
    player = make_player(runner, tmp_path)
    player.plan_strategy("plan", "plan")
    with pytest.raises(OutputFailedError):
        player.give_speech("speech", "speech")
    entry = json.loads(player.log_path.read_text(encoding="utf-8").splitlines()[-1])
    audit = ModelMessagesTypeAdapter.validate_python(entry["messages"])
    assert sum(isinstance(p, ToolCallPart) for m in audit for p in m.parts) == 2
    assert audit[0].parts[-1].content == "speech"


def test_blank_summary_uses_output_correction_not_infrastructure_retry():
    runner = fake_task_runner(["  \n", "给观众的话"])
    player = make_player(runner)
    before = deepcopy(player.messages)
    assert player.final_summary("总结") == "给观众的话"
    assert len(runner.calls) == 2 and player.messages == before
    assert "retry-prompt" in ModelMessagesTypeAdapter.dump_json(runner.calls[-1]).decode()


def test_double_blank_summary_fails_without_history_change():
    runner = fake_task_runner([" ", " \n"])
    player = make_player(runner)
    before = deepcopy(player.messages)
    with pytest.raises(OutputFailedError):
        player.final_summary("总结")
    assert player.messages == before and len(runner.calls) == 2


def test_cache_consumed_once_and_restored_with_history_by_checkpoint():
    runner = fake_task_runner([bid_reply(2, 6), bid_reply(1)])
    player = make_player(runner)
    player.plan_strategy("规划", "opening_plan")
    checkpoint = player.checkpoint()
    history = deepcopy(player.messages)
    for _ in range(2):
        assert player.decide_bid(make_ai_obs()) == (2, 6)
        player.rollback(checkpoint)
        assert player.messages == history and player._pending_day1_bid == (2, 6)
    assert player.decide_bid(make_ai_obs()) == (2, 6)
    assert len(runner.calls) == 1
    assert player.decide_bid(make_ai_obs()) == (1, None)


@pytest.mark.parametrize("fails", [False, True])
def test_replan_transaction_and_round_rollback(fails):
    replies = [bid_reply("bad"), bid_reply(99)] if fails else [bid_reply(4)]
    runner = fake_task_runner([bid_reply(2, 6), *replies])
    player = make_player(runner)
    player.plan_strategy("旧规划", "opening_plan")
    checkpoint = player.checkpoint()
    before = deepcopy(player.messages)
    if fails:
        with pytest.raises(OutputFailedError):
            player.plan_strategy("新规划", "next_plan")
        assert player.messages == before and player._pending_day1_bid == (2, 6)
    else:
        player.plan_strategy("新规划", "next_plan")
        assert player._pending_day1_bid == (4, None)
    player.rollback(checkpoint)
    assert player.messages == before and player._pending_day1_bid == (2, 6)


def test_rollback_new_plan_removes_new_cache():
    player = make_player(fake_task_runner([bid_reply(3)]))
    checkpoint = player.checkpoint()
    player.plan_strategy("规划", "opening_plan")
    player.rollback(checkpoint)
    assert player._pending_day1_bid is None and len(player.messages) == 1


def test_director_only_delivers_public_fields_to_other_players(tmp_path):
    from auction_game.controller import Controller
    a = make_player(fake_task_runner([speech_reply("public-A"), bid_reply(1)]))
    b = make_player(fake_task_runner([speech_reply("public-B"), bid_reply(2)]))
    b.name = "Bravo"
    director = ai_speech.AISpeechDirector([a, b], Controller(mode="continuous", announce=lambda _: None),
                                        tmp_path, game=a.game)
    director.on_opening()
    request = a.task_runner.calls[-1][-1].parts[-1].content
    assert "public-B" in request and "烟雾弹" not in request
    public_log = director.speeches_log.read_text(encoding="utf-8")
    assert "public-A" in public_log and "烟雾弹" not in public_log
