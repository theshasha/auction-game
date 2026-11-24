"""开赛预估：任务次数和整场流程一致，单次用量取历史场次，按单价和倍率计价。"""

from collections import Counter
from dataclasses import replace

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RequestUsage

from auction_game.agent.tasks import BidOutput, DiscussionOutput, MemoryOutput, SpeechOutput, SummaryOutput
from auction_game.cli.offline_demo import generate_demo
from auction_game.llm.execution import ExecutionResult
from auction_game.reporting.cost import dollars
from auction_game.reporting.analyze import load_match_logs, played_runs
from auction_game.reporting.estimate import SeatModel, collect_history, estimate, format_estimate, task_counts
from auction_game.reporting.run_paths import RunPaths
from tests.support.analyze_runs import execution_model, make_matches, player_log, write_matches
from tests.support.factories import TEAM_OF, TEST_GAME, task_entries
from tests.support.offline import OFFLINE_GAME, configure_offline_demo

OUTPUTS = {"speech": SpeechOutput("策略", "发言", []), "discussion": DiscussionOutput("策略", "信"),
           "plan": BidOutput("策略", 1, None, []), "bid": BidOutput("策略", 1, None, []),
           "memory": MemoryOutput("记忆"), "summary": SummaryOutput("总结")}
# 每百万 token 的美元价
COST = {"input": 1, "output": 10, "cache_read": 0.1}


@pytest.mark.parametrize("team", [False, True])
def test_task_counts_match_a_whole_offline_run(team, tmp_path, monkeypatch, config_files, teams_file):
    configure_offline_demo(monkeypatch, config_files, teams_file, team=team)
    run_dir = tmp_path / "run"
    generate_demo(run_dir, test_model=True, team=team)
    played = len(load_match_logs(run_dir))
    # 离线赛打平后有加赛，按实际打的局数算，和正赛打满的次数是同一个公式。
    expected = task_counts(replace(OFFLINE_GAME, regular_matches=played), team=team)
    for pc in RunPaths(run_dir).read_config()["players"]:
        assert Counter(entry["task"] for entry in task_entries(RunPaths(run_dir).player(pc["number"]))) == expected


def reply(input_tokens, output_tokens, cache_read=0):
    return ModelResponse(parts=[TextPart("回复")], usage=RequestUsage(
        input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cache_read))


def write_history(run_dir, run_type, models, usage, tasks=tuple(OUTPUTS)):
    """一场历史：models 是六个座位的执行模型，usage 是目录键到 tasks 里每类任务一次成功记录的用量。

    用量是每条回复的 (输入, 输出, 缓存命中)，可以再加一项回复条数，多出的是纠错往返。
    """
    players = [{"name": name, "execution_model": model} for name, model in zip(TEAM_OF, models)]
    write_matches(run_dir, make_matches(), run_type=run_type, players=players,
                  team_of=TEAM_OF if run_type.startswith("team") else None)
    for pc in RunPaths(run_dir).read_config()["players"]:
        tokens = usage.get(pc["execution_model"]["key"])
        if tokens is None:
            continue
        log = player_log(run_dir, pc)
        input_tokens, output_tokens, cache_read, replies = (*tokens, 1)[:4]
        for task in tasks:
            messages = [message for _ in range(replies)
                        for message in (ModelRequest(parts=[UserPromptPart("请求")]),
                                        reply(input_tokens, output_tokens, cache_read))]
            log.success({"title": task, "task": task, "context": task}, ExecutionResult(OUTPUTS[task], messages, []))


def model(key, seats=3, cost=COST, multiplier=1):
    return SeatModel(key, "default", f"{key}-model", seats, cost, multiplier)


@pytest.fixture
def history(tmp_path):
    """logs/ 下一场团队赛：known 每类任务输入 1000、命中 400、输出 100，other 输入 3000、输出 300。
    另有一场离线赛的 known 用量大得多，不该计入。"""
    logs = tmp_path / "logs"
    known, other = execution_model(key="known"), execution_model(key="other")
    write_history(logs / "20261101_100000_team", "team", [known] * 3 + [other] * 3,
                  {"known": (1000, 100, 400), "other": (3000, 300, 0)})
    write_history(logs / "20261101_110000_team-offline", "team-offline", [known] * 6,
                  {"known": (90000, 9000, 0)})
    return collect_history(played_runs())


def test_known_model_uses_its_own_history(history):
    [result] = estimate([model("known")], TEST_GAME, team=True, history=history)
    # 每次 (600 × 1 + 400 × 0.1 + 100 × 10) ÷ 10^6 美元，3 席
    expected = 3 * sum(task_counts(TEST_GAME, team=True).values()) * 1640 / 1_000_000
    assert (result.low, result.high, result.runs, result.borrowed, result.guessed) == (
        pytest.approx(expected), pytest.approx(expected), 1, False, ())
    lines = format_estimate([result], TEST_GAME, team=True)
    assert lines[1] == f"  known-model（3 席）：{dollars(expected)}（依据 1 场）"
    assert lines[2] == f"  全场合计：{dollars(expected)}"


def test_individual_run_borrows_team_history_and_applies_multiplier(history):
    [result] = estimate([model("known", seats=2, multiplier=0.5)], TEST_GAME, team=False, history=history)
    counts = task_counts(TEST_GAME, team=False)
    assert "discussion" not in counts
    expected = 2 * sum(counts.values()) * 1640 * 0.5 / 1_000_000
    assert (result.low, result.high, result.borrowed) == (pytest.approx(expected), pytest.approx(expected), True)
    assert format_estimate([result], TEST_GAME, team=False)[1] == (
        f"  known-model（2 席）：{dollars(expected)}（依据 1 场，借用团队赛数据）")


def test_model_without_history_gets_a_range_from_all_models(history):
    [result] = estimate([model("fresh")], TEST_GAME, team=True, history=history)
    # 输入取两个模型的中位数 2000，命中率取 40% 和 0% 的中位数 20%；输出在 100 到 300 之间。
    tasks = sum(task_counts(TEST_GAME, team=True).values())
    low = 3 * tasks * (1600 * 1 + 400 * 0.1 + 100 * 10) / 1_000_000
    high = 3 * tasks * (1600 * 1 + 400 * 0.1 + 300 * 10) / 1_000_000
    assert (result.low, result.high, result.runs) == (pytest.approx(low), pytest.approx(high), 0)
    assert set(result.guessed) == set(task_counts(TEST_GAME, team=True))
    assert format_estimate([result], TEST_GAME, team=True)[1] == (
        f"  fresh-model（3 席）：{dollars(low)}–{dollars(high)}（无历史，区间）")


def test_unpriced_model_is_left_out_of_the_total(history):
    results = estimate([model("known"), model("other", cost=None)], TEST_GAME, team=True, history=history)
    lines = format_estimate(results, TEST_GAME, team=True)
    assert lines[2] == "  other-model（3 席）：无单价"
    assert lines[3] == f"  全场合计：{dollars(results[0].low)}（不含 other-model）"
    assert lines[4].startswith("  含平均纠错，不含加赛、请求失败重发和补跑，只看量级")


def test_tiers_are_judged_per_reply_and_corrections_multiply(tmp_path):
    # 每次任务两条回复（一次纠错），每条输入 600：合起来超过 1000 的档，单条不超过，按基础单价计。
    write_history(tmp_path / "logs" / "20261101_100000_team", "team", [execution_model(key="retry")] * 6,
                  {"retry": (600, 50, 0, 2)})
    cost = {"input": 1, "output": 10, "tiers": [{"tier": {"type": "context", "size": 1000}, "input": 100,
                                                 "output": 1000}]}
    [result] = estimate([model("retry", seats=1, cost=cost)], TEST_GAME, team=True,
                        history=collect_history(played_runs()))
    # 每次任务 2 × (600 × 1 + 50 × 10) ÷ 10^6 美元
    expected = sum(task_counts(TEST_GAME, team=True).values()) * 2 * 1100 / 1_000_000
    assert result.low == result.high == pytest.approx(expected)


def test_task_nobody_has_played_is_left_out(tmp_path):
    # 只有个人赛的历史，团队赛的讨论谁都没有。
    write_history(tmp_path / "logs" / "20261101_100000_ai", "ai", [execution_model(key="known")] * 6,
                  {"known": (1000, 100, 400)}, tasks=[task for task in OUTPUTS if task != "discussion"])
    [result] = estimate([model("known")], TEST_GAME, team=True, history=collect_history(played_runs()))
    counts = task_counts(TEST_GAME, team=True)
    expected = 3 * (sum(counts.values()) - counts["discussion"]) * 1640 / 1_000_000
    assert (result.low, result.high, result.skipped, result.borrowed) == (
        pytest.approx(expected), pytest.approx(expected), ("discussion",), True)
    assert format_estimate([result], TEST_GAME, team=True)[1] == (
        f"  known-model（3 席）：{dollars(expected)}（依据 1 场，借用个人赛数据；讨论无法预估，不计入）")
