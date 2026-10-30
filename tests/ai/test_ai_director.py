"""各轮编排、私信送达、公开与私有边界、规划缓存及观众总结。"""

from copy import deepcopy
from dataclasses import replace
import threading
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter

from auction_game.ai import ai_director
from auction_game.ai.agent_execution import OutputFailedError
from tests.support.factories import (TEST_GAME, bid_reply, continuous_controller, fake_task_runner, make_ai_obs,
                                     make_log, make_obs, make_player, make_standings, request_text, speech_reply,
                                     text_reply, task_entries)
from auction_game.game.game_rules import GameSettings


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
        return f"发言{self.name}", []

    def plan_strategy(self, text, phase, context):
        self.calls.append((phase, text))
        return []

    def save_memory(self, text, phase, context):
        self.calls.append((phase, text))


def test_director_runs_rounds_in_order_and_passes_game_to_plans(monkeypatch, tmp_path):
    game = GameSettings(
        initial_coins=13, daily_salary=4, daily_pool=(2, 1, 3), max_bid=9,
        super_cost=1, super_bounty=5, super_hit_score=2,
        regular_matches=2, max_playoffs=0,
    )
    players = [RecordingPlayer(name) for name in ("甲", "乙")]
    director = ai_director.AIDirector(players, continuous_controller(), tmp_path, game=game)

    def opening_plan_request(others, actual_game, *, received):
        assert actual_game is game
        return f"规划 {others}"

    def next_match_plan_request(logs, standings, actual_game):
        assert actual_game is game
        return f"规划 {[log.match_index for log in logs]}"

    monkeypatch.setattr(ai_director.prompts, "opening_speech_request", lambda: "开局")
    monkeypatch.setattr(ai_director.prompts, "opening_plan_request", opening_plan_request)
    monkeypatch.setattr(ai_director.prompts, "next_match_plan_request", next_match_plan_request)
    monkeypatch.setattr(ai_director.prompts, "match_end_speech_request",
                        lambda log, standings, *, received: f"局末 {log.match_index}")
    monkeypatch.setattr(ai_director.prompts, "memory_request",
                        lambda others, index, *, received: f"记忆 {others} {index}")
    director.on_opening()
    director.on_match_end([SimpleNamespace(match_index=1), SimpleNamespace(match_index=2)], [])
    assert players[0].calls == [
        ("opening_speech", "开局"), ("opening_plan", "规划 {'乙': '发言乙'}"),
        ("match_2_speech", "局末 2"), ("match_2_memory", "记忆 {'乙': '发言乙'} 2"), ("match_2_plan", "规划 [1, 2]"),
    ]
    assert players[1].calls[1] == ("opening_plan", "规划 {'甲': '发言甲'}")


def test_director_orders_speeches_and_letters_by_seat_not_completion(monkeypatch, tmp_path):
    last_done = threading.Event()
    letters = {"乙": [{"to": "甲", "text": "乙的信"}],
               "甲": [{"to": "丙", "text": "甲的第一封"}, {"to": "乙", "text": "甲的第二封"}],
               "丙": [{"to": "甲", "text": "丙的信"}]}

    class OrderedPlayer(RecordingPlayer):
        def give_speech(self, text, phase, context):
            if self.name == "乙":
                assert last_done.wait(timeout=5)
            speech, _ = super().give_speech(text, phase, context)
            if self.name == "丙":
                last_done.set()
            return speech, letters[self.name]

    players = [OrderedPlayer(name) for name in ("乙", "甲", "丙")]
    director = ai_director.AIDirector(players, continuous_controller(), tmp_path, game=TEST_GAME)
    monkeypatch.setattr(ai_director.prompts, "opening_plan_request",
                        lambda others, game, *, received: f"规划 {list(others)} 收信 {list(received)}")
    director.on_opening()
    public_log = director.paths.speeches.read_text(encoding="utf-8")
    assert public_log.index("【乙】") < public_log.index("【甲】") < public_log.index("【丙】")
    assert players[1].calls[-1] == ("opening_plan", "规划 ['乙', '丙'] 收信 ['乙', '丙']")
    assert director.paths.letters.read_text(encoding="utf-8").endswith(
        "\n## 开局发言\n\n【乙 → 甲】乙的信\n\n【甲 → 丙】甲的第一封\n\n【甲 → 乙】甲的第二封\n\n【丙 → 甲】丙的信\n")


def test_only_public_speech_returned_without_hard_truncation(tmp_path):
    speech = '第一段说"6 真香"。\n\n第二段。' + "很" * 600
    runner = fake_task_runner([speech_reply(speech)])
    player = make_player(runner, tmp_path)
    assert player.give_speech("请发言", "opening_speech") == (speech, [])
    assert len(runner.calls) == 1
    entry = task_entries(player.log.path)[-1]
    assert entry["result"] == {**speech_reply(speech), "letters": []}
    assert "烟雾弹" in ModelMessagesTypeAdapter.dump_json(player.messages).decode()
    md = player.log.readable_path.read_text(encoding="utf-8")
    assert speech in md and "私有策略" in md


@pytest.mark.parametrize("invalid", [speech_reply("  "), "missing sections"])
def test_speech_schema_failure_gets_one_correction(invalid, tmp_path):
    runner = fake_task_runner([invalid, speech_reply("补上的发言")])
    player = make_player(runner, tmp_path)
    assert player.give_speech("请发言", "opening_speech") == ("补上的发言", [])
    assert len(runner.calls) == 2


def test_failed_speech_leaves_history_unchanged(tmp_path):
    runner = fake_task_runner(["private analysis", "more private"])
    player = make_player(runner, tmp_path)
    before = deepcopy(player.messages)
    with pytest.raises(OutputFailedError):
        player.give_speech("请发言", "opening_speech")
    assert player.messages == before


def test_failed_task_audit_after_prior_history_keeps_both_attempts(tmp_path):
    from pydantic_ai.messages import TextPart
    runner = fake_task_runner([bid_reply(1), speech_reply(" "), speech_reply(" ")])
    player = make_player(runner, tmp_path)
    player.plan_strategy("plan", "opening_plan")
    with pytest.raises(OutputFailedError):
        player.give_speech("speech", "match_1_speech")
    entry = task_entries(player.log.path)[-1]
    audit = ModelMessagesTypeAdapter.validate_python(entry["messages"])
    # SDK 合并历史末尾请求后，失败审计仍只包含本任务的两次原文。
    assert [p.content for m in audit for p in m.parts if isinstance(p, TextPart)] == [
        text_reply(speech_reply(" ")), text_reply(speech_reply(" "))]
    assert audit[0].parts[-1].content.startswith("speech\n\n请按以下格式提交完整结果")


def test_blank_summary_gets_output_correction(tmp_path):
    runner = fake_task_runner(["  \n", "给观众的话"])
    player = make_player(runner, tmp_path)
    before = deepcopy(player.messages)
    assert player.final_summary("总结") == "给观众的话"
    assert len(runner.calls) == 2 and player.messages == before
    assert "retry-prompt" in ModelMessagesTypeAdapter.dump_json(runner.calls[-1]).decode()


def test_double_blank_summary_fails_and_keeps_history(tmp_path):
    runner = fake_task_runner([" ", " \n"])
    player = make_player(runner, tmp_path)
    before = deepcopy(player.messages)
    with pytest.raises(OutputFailedError):
        player.final_summary("总结")
    assert player.messages == before and len(runner.calls) == 2


def test_cache_consumed_once_and_restored_with_history_by_checkpoint(tmp_path):
    runner = fake_task_runner([bid_reply(2, 6), bid_reply(1)])
    player = make_player(runner, tmp_path)
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
def test_replan_transaction_and_round_rollback(fails, tmp_path):
    replies = [bid_reply("bad"), bid_reply(99)] if fails else [bid_reply(4)]
    runner = fake_task_runner([bid_reply(2, 6), *replies])
    player = make_player(runner, tmp_path)
    player.plan_strategy("旧规划", "opening_plan")
    checkpoint = player.checkpoint()
    before = deepcopy(player.messages)
    if fails:
        with pytest.raises(OutputFailedError):
            player.plan_strategy("新规划", "match_1_plan")
        assert player.messages == before and player._pending_day1_bid == (2, 6)
    else:
        player.plan_strategy("新规划", "match_1_plan")
        assert player._pending_day1_bid == (4, None)
    player.rollback(checkpoint)
    assert player.messages == before and player._pending_day1_bid == (2, 6)


def test_rollback_new_plan_removes_new_cache(tmp_path):
    player = make_player(fake_task_runner([bid_reply(3)]), tmp_path)
    checkpoint = player.checkpoint()
    player.plan_strategy("规划", "opening_plan")
    player.rollback(checkpoint)
    assert player._pending_day1_bid is None and len(player.messages) == 1


def test_director_opening_plan_preserves_public_boundary_and_one_use_bid_cache(tmp_path):
    game = replace(TEST_GAME, initial_coins=13, daily_pool=(2, 1, 3), max_bid=9,
                   super_cost=1, super_hit_score=2, regular_matches=1)
    a = make_player(fake_task_runner([speech_reply("public-A"), bid_reply(2, 6), bid_reply(1)]), tmp_path / "a", game=game)
    b = make_player(fake_task_runner([speech_reply("public-B"), bid_reply(3, 6), bid_reply(0)]), tmp_path / "b",
                    game=game, name="Bravo")
    director = ai_director.AIDirector([a, b], continuous_controller(), tmp_path, game=a.game)
    director.on_opening()
    request = request_text(a.task_runner, -1)
    assert "public-B" in request and "烟雾弹" not in request
    public_log = director.paths.speeches.read_text(encoding="utf-8")
    assert "public-A" in public_log and "烟雾弹" not in public_log
    for player, planned, fallback in ((a, (2, 6), (1, None)), (b, (3, 6), (0, None))):
        assert len(player.task_runner.calls) == 2
        plan_input = request_text(player.task_runner, -1)
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


def test_director_final_speech_format_has_no_letters(monkeypatch, tmp_path):
    from auction_game.ai.text_output import output_requirement

    player = make_player(fake_task_runner([speech_reply("感言"), "总结"]), tmp_path)
    director = ai_director.AIDirector([player], continuous_controller(), tmp_path, game=TEST_GAME)
    monkeypatch.setattr(ai_director.prompts, "final_speech_request",
                        lambda standings, log, *, received: "请发表最终感言")
    monkeypatch.setattr(ai_director.prompts, "final_summary_request", lambda others: "请总结")
    director.on_final([], None)
    assert request_text(player.task_runner, 0) == "请发表最终感言\n\n" + output_requirement("speech")


def to_bravo(reply, text, to="Bravo"):
    return {**reply, "letters": [{"to": to, "text": text}]}


def test_letters_arrive_in_recipients_next_request_only(capsys, tmp_path):
    game = replace(TEST_GAME, daily_pool=(4, 4, 4), regular_matches=2)
    texts = ["开局信", "规划信", "次日信", "末日信", "局末信"]
    # 第 2 天改写给 Charlie：Bravo 第 3 天什么也收不到，之前的信不会再送一次。
    alpha = make_player(fake_task_runner([
        to_bravo(speech_reply("A"), "开局信"), to_bravo(bid_reply(1), "规划信"),
        to_bravo(bid_reply(1), "次日信", to="Charlie"), to_bravo(bid_reply(1), "末日信"),
        to_bravo(speech_reply("A"), "局末信"), {"memory": "记忆"}, bid_reply(1),
    ]), tmp_path, game=game, name="Alpha")
    bravo, charlie = (make_player(fake_task_runner([
        speech_reply(name), bid_reply(1), bid_reply(1), bid_reply(1), speech_reply(name), {"memory": "记忆"},
        bid_reply(1),
    ]), tmp_path, game=game, name=name) for name in ("Bravo", "Charlie"))
    players = [alpha, bravo, charlie]
    director = ai_director.AIDirector(players, continuous_controller(), tmp_path, game=game)
    director.on_opening()
    for day in (1, 2, 3):
        director.collect_bids([(p, make_obs(my_name=p.name, day=day, game=game)) for p in players])
    director.on_match_end([make_log(game=game)], make_standings({p.name: [0] for p in players}))
    # 请求依次是开局发言、规划、第 2 天和第 3 天出价、局末发言、整理记忆、下一局规划；第 1 天出价取规划缓存。
    # 收信段标题写明信是在哪一环节写的；规划请求同时交了第 1 天的出价。
    received = {("Bravo", 1): ("开局信", "在开局发言时"), ("Bravo", 2): ("规划信", "昨天出价时"),
                ("Charlie", 3): ("次日信", "昨天出价时"), ("Bravo", 4): ("末日信", "在本局最后一天出价时"),
                ("Bravo", 5): ("局末信", "在局末发言时")}
    assert [len(p.task_runner.calls) for p in players] == [7, 7, 7]
    for player in players:
        for index in range(7):
            request = request_text(player.task_runner, index)
            text, written = received.get((player.name, index), (None, None))
            assert [letter for letter in texts if letter in request] == ([text] if text else [])
            if text:
                assert f"以下是其他玩家{written}写给你的私信：\n\n【Alpha】{text}" in request
    # 只有写了信的轮次有一节，节标题是写信的那一轮。
    assert director.paths.letters.read_text(encoding="utf-8") == (
        "# 私信\n\n每节是这一环节里写的信，收件人在下一次行动时收到。\n"
        "\n## 开局发言\n\n【Alpha → Bravo】开局信\n"
        "\n## 第 1 局策略规划（含首日出价）\n\n【Alpha → Bravo】规划信\n"
        "\n## 第 1 局第 2 天出价\n\n【Alpha → Charlie】次日信\n"
        "\n## 第 1 局第 3 天出价\n\n【Alpha → Bravo】末日信\n"
        "\n## 第 1 局局末发言\n\n【Alpha → Bravo】局末信\n"
    )
    # 终端只打印公开发言。
    terminal = capsys.readouterr().out
    assert "【Alpha】A" in terminal and not [text for text in texts if text in terminal]


@pytest.mark.parametrize("days, final", [(1, False), (1, True), (2, True)],
                         ids=["single-day-match-end", "single-day-final", "last-day-final"])
def test_last_letter_of_a_match_arrives_at_the_next_speech(days, final, tmp_path):
    game = replace(TEST_GAME, daily_pool=(4,) * days, regular_matches=2)
    # 规划和第 2 天起的出价各一次请求，最后一次写信；一天一局时就是规划。
    bids = [bid_reply(1)] * (days - 1) + [to_bravo(bid_reply(1), "最后一封")]
    ending = ["总结"] if final else [{"memory": "记忆"}, bid_reply(1)]
    alpha = make_player(fake_task_runner([speech_reply("A"), *bids, speech_reply("A"), *ending]),
                        tmp_path, game=game, name="Alpha")
    bravo = make_player(fake_task_runner([speech_reply("B"), *[bid_reply(1)] * days, speech_reply("B"), *ending]),
                        tmp_path, game=game, name="Bravo")
    director = ai_director.AIDirector([alpha, bravo], continuous_controller(), tmp_path, game=game)
    director.on_opening()
    for day in range(1, days + 1):
        director.collect_bids([(p, make_obs(my_name=p.name, day=day, game=game)) for p in (alpha, bravo)])
    log, standings = make_log(game=game), make_standings({"Alpha": [0], "Bravo": [0]})
    if final:
        director.on_final(standings, log)
    else:
        director.on_match_end([log], standings)
    speech_request = request_text(bravo.task_runner, days + 1)
    assert ("最终感言" if final else "局末发言") in speech_request
    # 一天一局时这封是规划时写的，规划请求交的就是最后一天的出价。
    assert "以下是其他玩家在本局最后一天出价时写给你的私信：\n\n【Alpha】最后一封" in speech_request
    assert all("最后一封" not in request_text(bravo.task_runner, index) for index in range(days + 1))
