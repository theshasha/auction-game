"""玩家事务及生产 Agent 校验；模型边界注入，零网络。"""

from copy import deepcopy
from dataclasses import dataclass, replace
import json
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter, ToolCallPart, ToolReturnPart, RetryPromptPart
from pydantic_ai.messages import ModelRequest, SystemPromptPart
from pydantic_ai import Agent, ModelRetry, RunContext

from auction_game.ai.agent_execution import AgentExecutor, OutputFailedError
from tests.support.factories import TEST_GAME, bid_reply, fake_task_runner, make_ai_obs, make_player
from auction_game.ai import prompts
from auction_game.reporting.request_records import load_requests


@pytest.mark.parametrize("invalid", [
    {"strategy": "s", "bid": 2}, bid_reply("2"), bid_reply(True), bid_reply(2.0),
    bid_reply(2, True),
    bid_reply(2, strategy="  "), bid_reply(2, strategy=True), dict(bid_reply(2), extra=1),
])
def test_invalid_output_uses_one_shared_correction(invalid):
    runner = fake_task_runner([invalid, bid_reply(2)])
    player = make_player(runner)
    assert player.decide_bid(make_ai_obs(coins=4)) == (2, None)
    assert len(runner.calls) == 2
    assert any(isinstance(p, RetryPromptPart) for m in player.messages for p in m.parts)


def test_format_then_business_error_does_not_reset_budget(tmp_path, monkeypatch):
    runner = fake_task_runner([bid_reply("2"), bid_reply(7)])
    runner.executor.capture_requests = True
    player = make_player(runner, tmp_path)
    before = deepcopy(player.messages)
    with pytest.raises(OutputFailedError):
        player.decide_bid(make_ai_obs(day=2, coins=3))
    assert len(runner.calls) == 2 and player.messages == before
    entry = json.loads(player.log_path.read_text(encoding="utf-8"))
    assert "requests" not in entry
    load_requests(tmp_path / "capture.jsonl", [entry])
    assert entry["status"] == "failed" and entry["error"]["type"] == "OutputFailedError"
    assert [request["request"] for request in entry["requests"]] == [1, 2]
    assert any(part["part_kind"] == "retry-prompt"
               for message in entry["requests"][1]["messages"] for part in message["parts"])
    assert all(request["response"] for request in entry["requests"])
    audit = ModelMessagesTypeAdapter.validate_python(entry["messages"])
    assert sum(isinstance(p, ToolCallPart) for m in audit for p in m.parts) == 2
    md = player.log_path.with_suffix(".md").read_text(encoding="utf-8")
    assert "输出纠错 1 次，最终失败" in md and "OutputFailedError" in md
    assert "retry-prompt" not in md and "call-1" not in md
    # 同一次真实纠错审计导出后，首次无效回复与纠错输入都必须可见。
    from auction_game.reporting import replay
    from auction_game.reporting.run_paths import RunPaths
    paths = RunPaths(tmp_path)
    paths.prepare()
    paths.player(1).write_text(player.log_path.read_text(encoding="utf-8"), encoding="utf-8")
    paths.capture(1).parent.mkdir(parents=True, exist_ok=True)
    paths.capture(1).write_text((tmp_path / "capture.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
    paths.config.write_text(json.dumps({"format_version": 2, "run_type": "offline", "mode": "regular", "game": TEST_GAME.to_dict(), "timeout_seconds": 60,
        "players": [{"number": 1, "name": "Alpha", "identity_model": {"model": "identity"},
                     "execution_model": {"protocol": "chat", "model": "fake", "max_tokens": 123,
                                         "base_url": "https://offline.invalid/v1"}}]}), encoding="utf-8")
    monkeypatch.setattr(replay, "load_match_logs", lambda _: [{"standings_after_match": []}])
    readable = replay.export_replay(tmp_path, "Alpha", capture_definitions=True).read_text(encoding="utf-8")
    assert "第一次回复" in readable and "纠正后回复" in readable
    assert '"bid": "2"' in readable and '"bid": 7' in readable
    assert "渲染后的新增提示全文" in readable and "Input should be a valid integer" in readable
    assert "本次结果不提交玩家历史" in readable


def test_custom_game_and_realtime_balance_use_shared_rules():
    game = replace(TEST_GAME, initial_coins=4, max_bid=9, super_cost=3)
    runner = fake_task_runner([bid_reply(5), bid_reply(1, 9), bid_reply(8, 9), bid_reply(7, 9)])
    player = make_player(runner, game=game)
    assert player.plan_strategy(prompts.opening_plan_request({}, game), "opening_plan") is None
    assert player.decide_bid(make_ai_obs(coins=4, game=game)) == (1, 9)
    assert player.decide_bid(make_ai_obs(day=2, coins=10, game=game)) == (7, 9)
    assert len(runner.calls) == 4
    encoded = ModelMessagesTypeAdapter.dump_json(player.messages).decode()
    assert "超过当前余额 4" in encoded and "合计超过当前余额 10" in encoded


def test_history_is_append_only_and_tool_pairs_survive_rollback(tmp_path):
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
    assert [p.tool_call_id for p in parts if isinstance(p, ToolCallPart)] == [
        p.tool_call_id for p in parts if isinstance(p, ToolReturnPart)]
    entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
    assert entries[0]["result"] == bid_reply(1)
    assert ModelMessagesTypeAdapter.validate_python(entries[0]["messages"]) == middle[len(prefix):]
    md = player.log_path.with_suffix(".md").read_text(encoding="utf-8")
    assert md.startswith("# Alpha · 私人对话") and "tool-return" not in md and "私有策略" in md


def test_program_errors_propagate_without_retry_or_state_change():
    runner = fake_task_runner([RuntimeError("program defect")])
    player = make_player(runner)
    before = deepcopy(player.messages)
    with pytest.raises(RuntimeError, match="program defect"):
        player.decide_bid(make_ai_obs())
    assert len(runner.calls) == 1 and player.messages == before


@pytest.mark.parametrize("suffix", [".jsonl", ".md"])
@pytest.mark.parametrize("valid_output", [True, False])
def test_log_write_failure_propagates_and_preserves_history_and_plan(tmp_path, monkeypatch, suffix, valid_output):
    runner = fake_task_runner([bid_reply(1), bid_reply(2) if valid_output else bid_reply("bad"), bid_reply("bad")])
    player = make_player(runner, tmp_path)
    player.plan_strategy("initial plan", "opening_plan")
    before = deepcopy(player.messages), player._pending_day1_bid
    json_before = player.log_path.read_text(encoding="utf-8")
    md_before = player.log_path.with_suffix(".md").read_text(encoding="utf-8")
    original_open = Path.open
    failure = OSError("audit disk failure")

    def open_file(path, mode="r", *args, **kwargs):
        if path == player.log_path.with_suffix(suffix) and mode == "a":
            raise failure
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(OSError) as caught:
        player.plan_strategy("replacement", "next_plan")
    assert caught.value is failure
    assert (player.messages, player._pending_day1_bid) == before
    assert len(runner.calls) == (2 if valid_output else 3)
    assert player.log_path.with_suffix(".md").read_text(encoding="utf-8") == md_before
    json_after = player.log_path.read_text(encoding="utf-8")
    if suffix == ".jsonl":
        assert json_after == json_before
    else:
        # 沿用逐文件追加语义：JSONL 已成功时，Markdown 失败不会撤销它。
        entries = [json.loads(line) for line in json_after.splitlines()]
        assert len(entries) == 2
        assert entries[-1]["status"] == ("success" if valid_output else "failed")


@pytest.mark.parametrize("succeeds", [True, False])
def test_non_auction_agent_reuses_executor_with_typed_deps_and_isolated_history(succeeds):
    from tests.support.wire import Harness, reply

    @dataclass
    class Greeting:
        prefix: str

    agent = Agent(output_type=str, deps_type=Greeting, retries=1)

    @agent.output_validator
    def validate(ctx: RunContext[Greeting], output: str):
        # 修改运行快照，验证唯一隔离边界在成功及失败时都保护原历史。
        ctx.messages[0].parts[0].content = "working copy"
        if not output.startswith(ctx.deps.prefix):
            raise ModelRetry("missing greeting")
        return output

    h = Harness("chat", [reply(tool=False, text="invalid"),
                         reply(tool=False, text="hello reader" if succeeds else "invalid again")])
    history = [ModelRequest(parts=[SystemPromptPart("original")])]
    before = deepcopy(history)
    executor = AgentExecutor(h.factory)
    if succeeds:
        result = executor(agent, "greet", history, deps=Greeting("hello"))
        assert result.output == "hello reader" and result.messages
        assert not result.request_errors
    else:
        with pytest.raises(OutputFailedError) as caught:
            executor(agent, "greet", history, deps=Greeting("hello"))
        encoded = ModelMessagesTypeAdapter.dump_json(caught.value.messages).decode()
        assert "invalid again" in encoded and "missing greeting" in encoded
    assert history == before and len(h.requests) == 2 and h.closes == 1
