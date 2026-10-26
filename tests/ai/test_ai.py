"""AI 玩家的任务事务、输出纠错和日志写入失败。"""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter, TextPart, RetryPromptPart, SystemPromptPart

from auction_game.ai.agent_execution import OutputFailedError
from tests.support.factories import TEST_GAME, bid_reply, fake_task_runner, make_ai_obs, make_player, text_reply, task_entries
from auction_game.ai import prompts


@pytest.mark.parametrize("enable_predictions", [False, True])
@pytest.mark.parametrize("enable_personas", [False, True])
@pytest.mark.parametrize("writing_style", [None, "【写作要求】独特写作要求"])
def test_player_passes_prompt_switches_into_native_request(enable_predictions, enable_personas,
                                                           writing_style, tmp_path):
    from auction_game.ai.ai_player import AIPlayer

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
    assert player.plan_strategy(prompts.opening_plan_request({}, game), "opening_plan") is None
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
    assert entries[0]["result"] == bid_reply(1)
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
