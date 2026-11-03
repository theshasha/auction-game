"""AI 玩家的任务事务、输出纠错和日志写入失败。"""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter, TextPart, RetryPromptPart, SystemPromptPart

from auction_game.llm.execution import OutputFailedError
from auction_game.agent.text_output import BRIEF_BID_FORMAT, LetterNames, output_requirement
from tests.support.factories import (ALL_NAMES, TEST_GAME, bid_reply, fake_task_runner, make_ai_obs, make_player,
                                     request_text, speech_reply, text_reply, task_entries)
from auction_game.agent import prompts

LETTERS = LetterNames("Alpha", tuple(ALL_NAMES[1:]))


def retry_text(runner, index):
    return next(part.content for message in runner.calls[index] for part in message.parts
                if isinstance(part, RetryPromptPart))


@pytest.mark.parametrize("enable_predictions", [False, True])
@pytest.mark.parametrize("enable_personas", [False, True])
@pytest.mark.parametrize("writing_style", [None, "【写作要求】独特写作要求"])
def test_player_passes_prompt_switches_into_native_request(enable_predictions, enable_personas,
                                                           writing_style, tmp_path):
    from auction_game.agent.player import AIPlayer

    runner = fake_task_runner([bid_reply(1)])
    player = AIPlayer(
        "Alpha", "独特人设标题", "独特人设正文", [("Bravo", 73)], ["Alpha", "Bravo"],
        runner, game=TEST_GAME, log_path=tmp_path / "a.jsonl", readable_path=tmp_path / "a.md",
        enable_predictions=enable_predictions, enable_personas=enable_personas,
        writing_style=writing_style,
    )
    assert player.decide_bid(make_ai_obs()) == (1, None)
    system = next(part.content for message in runner.calls[0] for part in message.parts
                  if isinstance(part, SystemPromptPart))
    for marker in ("【赛前预测】", "Bravo 73%", "专家"):
        assert (marker in system) is enable_predictions
    for marker in ("【你的人设】", "独特人设标题", "独特人设正文", "认真思考"):
        assert (marker in system) is enable_personas
    assert ("独特写作要求" in system) is (writing_style is not None)
    assert "你叫 Alpha" in system and "Bravo" in system
    assert player.review_messages[0] == player.messages[0]


@pytest.mark.parametrize("invalid", [
    bid_reply("2"), "<strategy>missing standalone tags</strategy>", dict(bid_reply(2), extra=1),
])
def test_invalid_output_uses_one_shared_correction(invalid, tmp_path):
    runner = fake_task_runner([invalid, bid_reply(2)])
    player = make_player(runner, tmp_path)
    assert player.decide_bid(make_ai_obs(coins=4)) == (2, None)
    assert len(runner.calls) == 2
    assert any(isinstance(p, RetryPromptPart) for m in runner.calls[-1] for p in m.parts)
    assert any(isinstance(p, RetryPromptPart) for m in player.messages for p in m.parts)


def test_format_then_business_error_does_not_reset_budget(tmp_path):
    runner = fake_task_runner([bid_reply("2"), bid_reply(7)])
    player = make_player(runner, tmp_path)
    before = deepcopy(player.messages)
    with pytest.raises(OutputFailedError):
        player.decide_bid(make_ai_obs(day=2, coins=3))
    assert len(runner.calls) == 2 and player.messages == before
    entry = task_entries(player.log.path)[-1]
    assert entry["status"] == "failed" and entry["error"]["type"] == "OutputFailedError"
    assert any(part["part_kind"] == "retry-prompt"
               for message in entry["messages"] for part in message["parts"])
    audit = ModelMessagesTypeAdapter.validate_python(entry["messages"])
    assert [p.content for m in audit for p in m.parts if isinstance(p, TextPart)] == [
        text_reply(bid_reply("2")), text_reply(bid_reply(7))]
    md = player.log.readable_path.read_text(encoding="utf-8")
    assert "纠错 1 次，仍失败" in md and "OutputFailedError" in md
    assert "retry-prompt" not in md


def test_custom_game_and_realtime_balance_use_shared_rules(tmp_path):
    game = replace(TEST_GAME, initial_coins=4, max_bid=9, super_cost=3)
    runner = fake_task_runner([bid_reply(5), bid_reply(1, 9), bid_reply(8, 9), bid_reply(7, 9)])
    player = make_player(runner, tmp_path, game=game)
    assert player.plan_strategy(prompts.opening_plan_request({}, game), "opening_plan") == []
    assert player.decide_bid(make_ai_obs(coins=4, game=game)) == (1, 9)
    assert player.decide_bid(make_ai_obs(day=2, coins=10, game=game)) == (7, 9)
    assert len(runner.calls) == 4
    assert "超过当前余额 4" in ModelMessagesTypeAdapter.dump_json(runner.calls[1]).decode()
    assert "合计超过当前余额 10" in ModelMessagesTypeAdapter.dump_json(runner.calls[3]).decode()


def test_business_history_and_original_audit_survive_rollback(tmp_path):
    player = make_player(fake_task_runner([bid_reply(1), bid_reply(2)]), tmp_path)
    prefix = deepcopy(player.messages)
    player.decide_bid(make_ai_obs())
    assert player.messages[:len(prefix)] == prefix
    checkpoint = player.checkpoint()
    middle = deepcopy(player.messages)
    player.decide_bid(make_ai_obs(day=2))
    assert player.messages[:len(middle)] == middle
    player.rollback(checkpoint)
    assert player.messages == middle
    parts = [p for m in player.messages for p in m.parts]
    assert [p.content for p in parts if isinstance(p, TextPart)] == [text_reply(bid_reply(1))]
    entries = task_entries(player.log.path)
    assert entries[0]["result"] == {**bid_reply(1), "letters": []}
    audit = ModelMessagesTypeAdapter.validate_python(entries[0]["messages"])
    assert [p.content for m in audit for p in m.parts if isinstance(p, TextPart)] == [text_reply(bid_reply(1))]
    assert "我的私有分析" in ModelMessagesTypeAdapter.dump_json(middle).decode()
    md = player.log.readable_path.read_text(encoding="utf-8")
    assert md.startswith("# Alpha · 私人对话") and "私有策略" in md


def test_program_errors_propagate_without_retry_or_state_change(tmp_path):
    runner = fake_task_runner([RuntimeError("program defect")])
    player = make_player(runner, tmp_path)
    before = deepcopy(player.messages)
    with pytest.raises(RuntimeError, match="program defect"):
        player.decide_bid(make_ai_obs())
    assert len(runner.calls) == 1 and player.messages == before


@pytest.mark.parametrize("suffix,valid_output", [(".jsonl", True), (".md", False)])
def test_log_write_failure_propagates_and_preserves_history_and_plan(tmp_path, monkeypatch, suffix, valid_output):
    runner = fake_task_runner([bid_reply(1), bid_reply(2) if valid_output else bid_reply("bad"), bid_reply("bad")])
    player = make_player(runner, tmp_path)
    player.plan_strategy("initial plan", "opening_plan")
    before = deepcopy(player.messages), player._pending_day1_bid
    json_before = player.log.path.read_text(encoding="utf-8")
    md_before = player.log.readable_path.read_text(encoding="utf-8")
    original_open = Path.open
    failure = OSError("audit disk failure")

    def open_file(path, mode="r", *args, **kwargs):
        if path == player.log.path.with_suffix(suffix) and mode == "a":
            raise failure
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(OSError) as caught:
        player.plan_strategy("replacement", "match_1_plan")
    assert caught.value is failure
    assert (player.messages, player._pending_day1_bid) == before
    assert len(runner.calls) == (2 if valid_output else 3)
    assert player.log.readable_path.read_text(encoding="utf-8") == md_before
    json_after = player.log.path.read_text(encoding="utf-8")
    if suffix == ".jsonl":
        assert json_after == json_before
    else:
        # 两个文件分别追加，Markdown 写入失败时 JSONL 里已有这条记录。
        entries = task_entries(player.log.path)
        assert len(entries) == 2
        assert entries[-1]["status"] == ("success" if valid_output else "failed")


def test_plan_has_full_format_and_bids_from_day_two_get_one_line(tmp_path):
    runner = fake_task_runner([bid_reply(1), bid_reply(1), bid_reply(1)])
    player = make_player(runner, tmp_path)
    player.decide_bid(make_ai_obs(day=1))  # 没有规划缓存时第 1 天也发请求
    player.plan_strategy("请规划", "opening_plan")
    player.decide_bid(make_ai_obs(day=2))
    assert request_text(runner, 0).endswith("\n\n" + output_requirement("bid", LETTERS))
    assert request_text(runner, 1) == "请规划\n\n" + output_requirement("plan", LETTERS)
    assert request_text(runner, 2).endswith("\n\n" + BRIEF_BID_FORMAT)
    # 只附一句时也写明送达时机。
    assert "收件人下一次行动时才收到" in request_text(runner, 2)


def test_format_error_after_one_line_prompt_gets_full_format(tmp_path):
    runner = fake_task_runner(["随手写的出价", bid_reply(1)])
    player = make_player(runner, tmp_path)
    assert player.decide_bid(make_ai_obs(day=2)) == (1, None)
    assert retry_text(runner, 1) == ("必须先依次提交 strategy 与 decision 两块，标签各占一行且各出现一次，私信块放在最后。\n"
                                     + output_requirement("bid", LETTERS))


def test_speech_letters_are_parsed_and_final_speech_cannot_write_letters(tmp_path):
    letter = {"to": "Bravo", "text": "只给你看"}
    runner = fake_task_runner([{**speech_reply("开局"), "letters": [letter]},
                               {**speech_reply("感言"), "letters": [letter]}, speech_reply("感言")])
    player = make_player(runner, tmp_path)
    assert player.give_speech("请发言", "opening_speech") == ("开局", [letter])
    assert player.give_speech("请发表感言", "final_speech", letters=False) == ("感言", [])
    assert request_text(runner, 0) == "请发言\n\n" + output_requirement("speech", LETTERS)
    assert request_text(runner, 1) == "请发表感言\n\n" + output_requirement("speech")
    assert retry_text(runner, 2).startswith("协议块外只允许空白，请直接提交两块正文。\n")
    opening, final = task_entries(player.log.path)
    assert opening["result"]["letters"] == [letter]
    assert final["result"]["letters"] == []


def test_bid_shows_received_letters_and_returns_written_ones(tmp_path):
    letter = {"to": "Bravo", "text": "明天我出 3"}
    runner = fake_task_runner([{**bid_reply(2), "letters": [letter]}])
    player = make_player(runner, tmp_path)
    assert player.bid(make_ai_obs(day=2), {"Charlie": "我会狙 2"}) == ((2, None), [letter])
    assert f"以下是其他玩家昨天出价时写给你的私信：\n\n【Charlie】我会狙 2\n\n{prompts.BID_HINT}" in request_text(runner, 0)


def test_plan_returns_letters_and_day_one_bid_from_cache_has_none(tmp_path):
    letter = {"to": "Bravo", "text": "规划时写的"}
    runner = fake_task_runner([{**bid_reply(1, 3), "letters": [letter]}])
    player = make_player(runner, tmp_path)
    assert player.plan_strategy("请规划", "opening_plan") == [letter]
    assert player.bid(make_ai_obs(), {}) == ((1, 3), [])
    assert len(runner.calls) == 1
