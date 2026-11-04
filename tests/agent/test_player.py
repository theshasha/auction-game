"""AI 玩家的任务事务、输出纠错和日志写入失败。"""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter, TextPart, RetryPromptPart, SystemPromptPart

from auction_game.llm.execution import OutputFailedError
from auction_game.agent.text_output import BRIEF_BID_FORMAT, DECISION, LetterNames, SPEECH, output_requirement
from tests.support.factories import (ALL_NAMES, TEST_GAME, TEST_LINEUP, bid_reply, fake_task_runner, make_ai_obs,
                                     make_player, request_text, speech_reply, text_reply, task_entries)
from auction_game.agent import prompts

LETTERS = LetterNames("Alpha", tuple(ALL_NAMES[1:]))


def retry_text(runner, index):
    return next(part.content for message in runner.calls[index] for part in message.parts
                if isinstance(part, RetryPromptPart))


def test_system_prompt_is_sent_and_opens_both_histories(tmp_path):
    runner = fake_task_runner([bid_reply(1)])
    player = make_player(runner, tmp_path)
    assert player.bid(make_ai_obs(), {}, "第 1 局第 2 天出价").submission == (1, None)
    expected = prompts.build_system_prompt(TEST_LINEUP.profiles[0], TEST_LINEUP, TEST_GAME)
    sent = [part.content for message in runner.calls[0] for part in message.parts
            if isinstance(part, SystemPromptPart)]
    assert sent == [expected]
    for history in (player.messages, player.review_messages):
        assert [(type(part), part.content) for part in history[0].parts] == [(SystemPromptPart, expected)]


@pytest.mark.parametrize("invalid", [
    bid_reply("2"), "<strategy>missing standalone tags</strategy>", dict(bid_reply(2), extra=1),
])
def test_invalid_output_uses_one_shared_correction(invalid, tmp_path):
    runner = fake_task_runner([invalid, bid_reply(2)])
    player = make_player(runner, tmp_path)
    assert player.bid(make_ai_obs(coins=4), {}, "第 1 局第 2 天出价").submission == (2, None)
    assert len(runner.calls) == 2
    assert any(isinstance(p, RetryPromptPart) for m in runner.calls[-1] for p in m.parts)
    assert any(isinstance(p, RetryPromptPart) for m in player.messages for p in m.parts)


def test_format_then_business_error_does_not_reset_budget(tmp_path):
    runner = fake_task_runner([bid_reply("2"), bid_reply(7)])
    player = make_player(runner, tmp_path)
    before = deepcopy(player.messages)
    with pytest.raises(OutputFailedError):
        player.bid(make_ai_obs(day=2, coins=3), {}, "第 1 局第 2 天出价")
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
    assert player.plan_strategy(prompts.opening_plan_request({}, game), "第 1 局策略规划（含首日出价）").submission == (1, 9)
    assert player.bid(make_ai_obs(coins=10, game=game), {}, "第 1 局第 2 天出价").submission == (7, 9)
    assert len(runner.calls) == 4
    assert "超过当前余额 4" in ModelMessagesTypeAdapter.dump_json(runner.calls[1]).decode()
    assert "合计超过当前余额 10" in ModelMessagesTypeAdapter.dump_json(runner.calls[3]).decode()


def test_business_history_and_original_audit_survive_rollback(tmp_path):
    player = make_player(fake_task_runner([bid_reply(1), bid_reply(2)]), tmp_path)
    prefix = deepcopy(player.messages)
    player.bid(make_ai_obs(), {}, "第 1 局第 2 天出价")
    assert player.messages[:len(prefix)] == prefix
    checkpoint = player.checkpoint()
    middle = deepcopy(player.messages)
    player.bid(make_ai_obs(day=2), {}, "第 1 局第 2 天出价")
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
        player.bid(make_ai_obs(), {}, "第 1 局第 2 天出价")
    assert len(runner.calls) == 1 and player.messages == before


@pytest.mark.parametrize("suffix,valid_output", [(".jsonl", True), (".md", False)])
def test_log_write_failure_propagates_and_preserves_history(tmp_path, monkeypatch, suffix, valid_output):
    runner = fake_task_runner([bid_reply(1), bid_reply(2) if valid_output else bid_reply("bad"), bid_reply("bad")])
    player = make_player(runner, tmp_path)
    player.plan_strategy("initial plan", "第 1 局策略规划（含首日出价）")
    before = deepcopy(player.messages)
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
        player.plan_strategy("replacement", "第 2 局策略规划（含首日出价）")
    assert caught.value is failure
    assert player.messages == before
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


def test_plan_has_full_format_and_bids_get_one_line(tmp_path):
    runner = fake_task_runner([bid_reply(1), bid_reply(1)])
    player = make_player(runner, tmp_path)
    player.plan_strategy("请规划", "第 1 局策略规划（含首日出价）")
    player.bid(make_ai_obs(), {}, "第 1 局第 2 天出价")
    assert request_text(runner, 0) == "请规划\n\n" + output_requirement(DECISION, LETTERS)
    assert request_text(runner, 1).endswith("\n\n" + BRIEF_BID_FORMAT)
    # 只附一句时也写明送达时机。
    assert "收件人下一次行动时才收到" in request_text(runner, 1)


def test_format_error_after_one_line_prompt_gets_full_format(tmp_path):
    runner = fake_task_runner(["随手写的出价", bid_reply(1)])
    player = make_player(runner, tmp_path)
    assert player.bid(make_ai_obs(day=2), {}, "第 1 局第 2 天出价").submission == (1, None)
    assert retry_text(runner, 1) == ("必须先依次提交 strategy 与 decision 两块，标签各占一行且各出现一次，私信块放在最后。\n"
                                     + output_requirement(DECISION, LETTERS))


def test_speech_letters_are_parsed_and_final_speech_cannot_write_letters(tmp_path):
    letter = {"to": "Bravo", "text": "只给你看"}
    runner = fake_task_runner([{**speech_reply("开局"), "letters": [letter]},
                               {**speech_reply("感言"), "letters": [letter]}, speech_reply("感言")])
    player = make_player(runner, tmp_path)
    opening = player.give_speech("请发言", "开局发言")
    assert (opening.speech, opening.letters) == ("开局", [letter])
    final = player.give_speech("请发表感言", "最终感言", letters=False)
    assert (final.speech, final.letters) == ("感言", [])
    assert request_text(runner, 0) == "请发言\n\n" + output_requirement(SPEECH, LETTERS)
    assert request_text(runner, 1) == "请发表感言\n\n" + output_requirement(SPEECH)
    assert retry_text(runner, 2).startswith("协议块外只允许空白，请直接提交两块正文。\n")
    opening, final = task_entries(player.log.path)
    assert opening["result"]["letters"] == [letter]
    assert final["result"]["letters"] == []


def test_bid_shows_received_letters_and_returns_written_ones(tmp_path):
    letter = {"to": "Bravo", "text": "明天我出 3"}
    runner = fake_task_runner([{**bid_reply(2), "letters": [letter]}])
    player = make_player(runner, tmp_path)
    output = player.bid(make_ai_obs(day=2), {"Charlie": "我会狙 2"}, "第 1 局第 2 天出价")
    assert (output.submission, output.letters) == ((2, None), [letter])
    assert f"以下是其他玩家昨天出价时写给你的私信：\n\n【Charlie】我会狙 2\n\n{prompts.BID_HINT}" in request_text(runner, 0)


def test_plan_returns_day_one_bid_and_letters(tmp_path):
    letter = {"to": "Bravo", "text": "规划时写的"}
    runner = fake_task_runner([{**bid_reply(1, 3), "letters": [letter]}])
    player = make_player(runner, tmp_path)
    output = player.plan_strategy("请规划", "第 1 局策略规划（含首日出价）")
    assert (output.submission, output.letters) == ((1, 3), [letter])
    assert len(runner.calls) == 1
