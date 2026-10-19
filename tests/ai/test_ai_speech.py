"""公开边界、规划缓存及观众总结；任务均经过真实 Agent。"""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter

from auction_game.ai import ai_speech
from auction_game.ai.agent_execution import OutputFailedError
from tests.support.factories import TEST_GAME, bid_reply, fake_task_runner, make_ai_obs, make_player, speech_reply, text_reply, task_entries
from auction_game.game.game_rules import GameSettings
from auction_game.game.players import ScriptedPlayer


class RecordingPlayer:
    """记录收到的任务，发言返回固定文本。"""

    def __init__(self, name):
        self.name, self.calls = name, []

    def checkpoint(self):
        return None

    def rollback(self, checkpoint):
        pass

    def give_speech(self, text, phase, context):
        self.calls.append((phase, text))
        return f"发言{self.name}"

    def plan_strategy(self, text, phase, context):
        self.calls.append((phase, text))

    def save_memory(self, text, phase, context):
        self.calls.append((phase, text))


def test_director_runs_rounds_in_order_and_passes_game_to_plans(monkeypatch, tmp_path):
    from auction_game.controller import Controller

    game = GameSettings(
        initial_coins=13, daily_salary=4, daily_pool=(2, 1, 3), max_bid=9,
        super_cost=1, super_bounty=5, super_hit_score=2,
        regular_matches=2, max_playoffs=0,
    )
    players = [RecordingPlayer(name) for name in ("甲", "乙")]
    director = ai_speech.AISpeechDirector(players, Controller(mode="continuous", announce=lambda _: None),
                                          tmp_path, game=game)

    def plan_request(others, actual_game, *index):
        assert actual_game is game
        return f"规划 {others} {index}"

    monkeypatch.setattr(ai_speech.prompts, "opening_speech_request", lambda: "开局")
    monkeypatch.setattr(ai_speech.prompts, "opening_plan_request", plan_request)
    monkeypatch.setattr(ai_speech.prompts, "next_match_plan_request", plan_request)
    monkeypatch.setattr(ai_speech.prompts, "match_end_speech_request", lambda *args: "局末")
    monkeypatch.setattr(ai_speech.prompts, "memory_request", lambda others: f"记忆 {others}")
    director.on_opening()
    director.on_match_end(SimpleNamespace(match_index=1), [])
    assert players[0].calls == [
        ("opening_speech", "开局"), ("opening_plan", "规划 {'乙': '发言乙'} ()"),
        ("match_1_speech", "局末"), ("match_1_memory", "记忆 {'乙': '发言乙'}"), ("match_1_plan", "规划 [] (2,)"),
    ]
    assert players[1].calls[1] == ("opening_plan", "规划 {'甲': '发言甲'} ()")


def test_director_orders_parallel_results_by_initial_players(monkeypatch, tmp_path):
    players = [ScriptedPlayer.constant(name, 0) for name in ("乙", "甲", "丙")]
    director = ai_speech.AISpeechDirector(players, None, tmp_path, game=None)
    monkeypatch.setattr(ai_speech, "run_parallel_round",
                        lambda *args, **kwargs: {"丙": "发言丙", "甲": "发言甲", "乙": "发言乙"})

    speeches = director._run_round("开局发言", lambda player: None)

    assert list(speeches.items()) == [("乙", "发言乙"), ("甲", "发言甲"), ("丙", "发言丙")]
    assert list(director._others(speeches, "甲")) == ["乙", "丙"]
    director._publish_speeches("开局发言", speeches)
    public_log = director.speeches_log.read_text(encoding="utf-8")
    assert public_log.index("【乙】") < public_log.index("【甲】") < public_log.index("【丙】")


def test_only_public_speech_returned_without_hard_truncation(tmp_path):
    speech = '第一段说"6 真香"。\n\n第二段。' + "很" * 600
    runner = fake_task_runner([speech_reply(speech)])
    player = make_player(runner, tmp_path)
    assert player.give_speech("请发言", "opening_speech") == speech
    assert len(runner.calls) == 1
    entry = task_entries(player.log_path)[-1]
    assert entry["result"] == speech_reply(speech)
    assert "烟雾弹" in ModelMessagesTypeAdapter.dump_json(player.messages).decode()
    md = player.log_path.with_suffix(".md").read_text(encoding="utf-8")
    assert speech in md and "私有策略" in md


@pytest.mark.parametrize("invalid", [speech_reply("  "), "<speech>missing strategy</speech>",
                                          {"strategy": "  ", "speech": "public"}, "missing sections"])
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
    from pydantic_ai.messages import TextPart
    runner = fake_task_runner([bid_reply(1), speech_reply(" "), speech_reply(" ")])
    player = make_player(runner, tmp_path)
    player.plan_strategy("plan", "plan")
    with pytest.raises(OutputFailedError):
        player.give_speech("speech", "speech")
    entry = task_entries(player.log_path)[-1]
    audit = ModelMessagesTypeAdapter.validate_python(entry["messages"])
    # SDK 合并历史末尾请求后，失败审计仍只包含本任务的两次原文。
    assert [p.content for m in audit for p in m.parts if isinstance(p, TextPart)] == [
        text_reply(speech_reply(" ")), text_reply(speech_reply(" "))]
    assert audit[0].parts[-1].content.startswith("speech\n\n请按以下格式提交完整结果")


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


def test_director_opening_plan_preserves_public_boundary_and_one_use_bid_cache(tmp_path):
    from auction_game.controller import Controller
    game = replace(TEST_GAME, initial_coins=13, daily_pool=(2, 1, 3), max_bid=9,
                   super_cost=1, super_hit_score=2, regular_matches=1)
    a = make_player(fake_task_runner([speech_reply("public-A"), bid_reply(2, 6), bid_reply(1)]), game=game)
    b = make_player(fake_task_runner([speech_reply("public-B"), bid_reply(3, 6), bid_reply(0)]), game=game)
    b.name = "Bravo"
    director = ai_speech.AISpeechDirector([a, b], Controller(mode="continuous", announce=lambda _: None),
                                        tmp_path, game=a.game)
    director.on_opening()
    request = a.task_runner.calls[-1][-1].parts[-1].content
    assert "public-B" in request and "烟雾弹" not in request
    public_log = director.speeches_log.read_text(encoding="utf-8")
    assert "public-A" in public_log and "烟雾弹" not in public_log
    for player, planned, fallback in ((a, (2, 6), (1, None)), (b, (3, 6), (0, None))):
        assert len(player.task_runner.calls) == 2
        plan_input = player.task_runner.calls[-1][-1].parts[-1].content
        for fact in ("你有 13 枚金币", "今日奖池为 2 分",
                     "本局还剩 3 天（含今天），剩余拍卖奖池共 6 分",
                     "如果每天都命中，每人最多还能获得 6 命中分"):
            assert fact in plan_input
        assert plan_input.count("全员累计总分：全员 0 分。") == 1
        obs = make_ai_obs(game=game, coins=game.initial_coins)
        assert player.decide_bid(obs) == planned
        assert len(player.task_runner.calls) == 2
        assert player.decide_bid(obs) == fallback
        assert len(player.task_runner.calls) == 3
