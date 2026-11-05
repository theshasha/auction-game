"""analyze.py 统计口径测试。

合成对局（两局，每天一行出价 = NAMES 顺序）：

第 1 局
  第 1 天：出价 [5,4,3,2,1,0]；乙 狙 5（命中）、丙 狙 6（落空）；
            5 被撞车（甲 扣款 5），乙 以 4 获胜。
  第 2 天：出价 [3,3,1,2,0,6]；3 互撞（甲、乙 扣款 3），己 以 6 获胜。
  拍卖分：乙 1、己 1；乙命中另得 2 分，独占最高。
第 2 局
  第 1 天：出价 [0,3,0,2,5,4]；0 互撞，戊 以 5 获胜。
  第 2 天：出价 [0,4,1,2,3,2]；2 互撞（丁、己 扣款 2），乙 以 4 获胜。
  拍卖分：乙 1、戊 1（乙/戊 各记并列最高 1 局）。

局末余额按 game（初始 7、日工资 3、超级出价成本 2、悬赏 3）手算后写进日志。
"""

import json
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart, TextPart, UserPromptPart
from pydantic_ai.usage import RequestUsage

from tests.support.factories import TEAM_OF, TEST_GAME, make_day, make_log, make_standings

from auction_game.llm.execution import CallFailedError, ExecutionResult
from auction_game.agent.log import AgentLog
from auction_game.agent.tasks import BidOutput, DiscussionOutput, SpeechOutput, SummaryOutput
from auction_game.reporting.analyze import (analyze, collect_player_stats, collect_team_stats, find_latest_ai_run,
                                             load_match_logs, main)
from auction_game.reporting.run_output import initialize_run
from auction_game.reporting.run_paths import RunPaths

ANALYZE_GAME = replace(TEST_GAME, daily_pool=(1, 1))


def make_match_1() -> dict:
    days = [
        make_day(1, [5, 4, 3, 2, 1, 0], super_bids={"乙": 5, "丙": 6}),
        make_day(2, [3, 3, 1, 2, 0, 6]),
    ]
    return make_log(match_index=1, days=days, game=ANALYZE_GAME).to_dict()


def make_match_2() -> dict:
    days = [
        make_day(1, [0, 3, 0, 2, 5, 4]),
        make_day(2, [0, 4, 1, 2, 3, 2]),
    ]
    return make_log(match_index=2, days=days, game=ANALYZE_GAME).to_dict()


def make_matches() -> list[dict]:
    return [
        {"match": make_match_1(), "standings_after_match": []},
        {"match": make_match_2(), "standings_after_match": []},
    ]


def rows_by_name(stats: dict) -> dict:
    return {row["name"]: row for row in stats["rows"]}


def test_score_matrices_and_top_match_counts():
    stats = collect_player_stats(make_matches())
    assert stats["match_labels"] == ["正赛第 1 局", "正赛第 2 局"]
    assert stats["auction_score_matrix"]["乙"] == [1, 1]
    assert stats["score_matrix"]["乙"] == [3, 1]
    assert stats["score_matrix"]["己"] == [1, 0]
    rows = rows_by_name(stats)
    # 首局乙命中后独占最高，次局乙/戊并列最高。
    assert rows["乙"]["tied_top_matches"] == 1
    assert rows["乙"]["solo_top_matches"] == 1
    assert rows["己"]["tied_top_matches"] == 0
    assert rows["戊"]["tied_top_matches"] == 1
    assert rows["甲"]["tied_top_matches"] == 0
    assert sum(row["solo_top_matches"] for row in stats["rows"]) == 1
    # 排序：总分降序，同分按名字（己 U+5DF1 < 戊 U+620A）
    assert [row["name"] for row in stats["rows"]] == ["乙", "己", "戊", "丁", "丙", "甲"]


@pytest.mark.parametrize("all_void", [False, True])
def test_solo_top_and_all_zero_tied_matches(all_void, tmp_path, capsys):
    days = [make_day(day, [2, 2] if all_void else [2, 1]) for day in (1, 2)]
    match = make_log(days=days, game=ANALYZE_GAME).to_dict()
    rows = rows_by_name(collect_player_stats([{"match": match}]))
    assert rows["甲"]["solo_top_matches"] == (0 if all_void else 1)
    assert rows["乙"]["solo_top_matches"] == 0
    assert rows["甲"]["tied_top_matches"] == int(all_void)
    assert rows["乙"]["tied_top_matches"] == int(all_void)
    if all_void:
        assert rows["甲"]["avg_bid"] is None
        write_matches(tmp_path, [{"match": match}])
        analyze(tmp_path)
        output = capsys.readouterr().out
        assert output.count("单局最高总分：独占0局，并列1局") == 2
        assert output.count("平均正常出价（不含流局日）：—") == 2


def test_player_detail_metrics():
    rows = rows_by_name(collect_player_stats(make_matches()))
    # 乙：超 1 用 1 中；撞车扣款 = 3（第 1 局第 2 天互撞）；均出 = (4+3+3+4)/4
    assert rows["乙"]["total_score"] == 4
    assert rows["乙"]["auction_score"] == 2
    assert rows["乙"]["super_score"] == 2
    assert rows["乙"]["super_uses"] == 1 and rows["乙"]["super_hits"] == 1
    assert rows["乙"]["super_net_coins"] == 1
    assert rows["乙"]["collision_burn"] == 3
    assert rows["乙"]["normal_collision_burn"] == 3
    assert rows["乙"]["unique_sniped_burn"] == 0
    assert rows["乙"]["avg_bid"] == 3.5
    assert rows["乙"]["end_coins"] == [4, 6]
    assert rows["乙"]["zero_bids"] == 0
    # 甲：没用超级出价；撞车扣款 = 5+3；均出 = (5+3+0+0)/4；出 0 两次
    assert rows["甲"]["super_uses"] == 0 and rows["甲"]["super_hits"] == 0
    assert rows["甲"]["super_net_coins"] == 0
    assert rows["甲"]["collision_burn"] == 8
    assert rows["甲"]["normal_collision_burn"] == 3
    assert rows["甲"]["unique_sniped_burn"] == 5
    assert rows["甲"]["avg_bid"] == 2.0
    assert rows["甲"]["zero_bids"] == 2
    # 丙：超级出价 1 次，命中 0 次（狙 6 落空）；无撞车扣款；出 0 一次
    assert rows["丙"]["super_uses"] == 1 and rows["丙"]["super_hits"] == 0
    assert rows["丙"]["super_net_coins"] == -2
    assert rows["丙"]["collision_burn"] == 0
    assert rows["丙"]["avg_bid"] == 1.25
    assert rows["丙"]["zero_bids"] == 1
    # 丁：没用超级出价；撞车扣款 = 2（第 2 局第 2 天互撞）
    assert rows["丁"]["super_uses"] == 0 and rows["丁"]["super_hits"] == 0
    assert rows["丁"]["collision_burn"] == 2
    assert rows["丁"]["avg_bid"] == 2.0
    assert rows["丁"]["zero_bids"] == 0
    # 戊 / 己：各 1 胜；戊 出 0 一次、己 撞车扣款 2 且出 0 一次
    assert rows["戊"]["total_score"] == 1 and rows["戊"]["zero_bids"] == 1
    assert rows["己"]["total_score"] == 1 and rows["己"]["collision_burn"] == 2
    assert rows["己"]["avg_bid"] == 3.0 and rows["己"]["zero_bids"] == 1


def test_analyze_prints_player_section(tmp_path, capsys):
    run_dir = tmp_path / "20260909_120000_ai"
    run_dir.mkdir()
    write_matches(run_dir, make_matches())
    analyze(run_dir)
    output = capsys.readouterr().out
    sections = ["比赛概况", "经济与超级出价", "玩家明细", "特殊情况"]
    assert [output.index(section) for section in sections] == sorted(output.index(section) for section in sections)
    assert "各局顺序：正赛第 1 局 / 正赛第 2 局" in output
    yi = output.split("\n乙\n", 1)[1].split("\n\n", 1)[0]
    for text in ["总分：4分（拍卖2分，命中2分）", "各局总分：3 / 1", "各局拍卖分：1 / 1",
                 "单局最高总分：独占1局，并列1局",
                 "超级出价：使用1次，命中1次（100%）", "超级出价净金币变化：+1金币",
                 "撞车损失：3金币（正常出价互撞3，唯一正常出价被狙0）",
                 "平均正常出价（不含流局日）：3.50", "各局结束余额：4 / 6 金币",
                 "正常出价为零：0次"]:
        assert text in yi
    jia = output.split("\n甲\n", 1)[1].split("\n\n", 1)[0]
    assert "超级出价：使用0次，命中0次\n" in jia
    assert "正常出价为零：2次" in jia
    assert "撞车损失：8金币（正常出价互撞3，唯一正常出价被狙5）" in jia
    assert "撞车损失：15 金币（正常出价互撞 10，唯一正常出价被狙 5；只计正常出价）" in output
    assert "局末平均剩余金币：7.08（最低 2，最高 10）" in output
    assert "成本消耗：4 金币" in output
    assert "悬赏发放：3 金币" in output
    assert "超级出价净金币变化：-1 金币" in output
    assert "超级出价净金币变化：-2金币" in output
    assert "命中原本互撞价 0 次（按超级出价笔数计，包含流局）" in output
    assert "使用超级出价：1 天" in output
    assert "改变当日结果：1 天" in output
    assert "含正常出价及超级出价的价位" in output
    assert "获胜价平均" in output
    assert "模型用量" not in output  # 离线赛的用量是假模型估算的，不输出
    assert "阵营" not in output


@pytest.mark.parametrize("bids,super_bids,winner,winning_bid,collisions,changed,collision_hits,"
                         "normal_burn,sniped_burn", [
    ([5, 4, 3], {"乙": (6, False)}, "甲", 5, [], 0, 0, 0, 0),  # 落空
    ([5, 4, 3], {"乙": (3, True)}, "甲", 5, [3], 0, 0, 0, 3),  # 命中低价
    ([5, 5, 3], {"丙": (5, True)}, "丙", 3, [5], 0, 1, 10, 0),  # 原本就互撞
    ([5, 5, 3, 2], {"丙": (5, True), "丁": (5, True)}, "丙", 3, [5], 0, 2, 10, 0),
    ([5, 5, 3, 1], {"丙": (5, True), "丁": (3, True)}, "丁", 1, [3, 5], 1, 1, 10, 3),
    ([5, 4, 3], {"乙": (5, True), "丙": (5, True)}, "乙", 4, [5], 1, 0, 0, 5),
    ([5, 3, 1], {"甲": (3, True), "乙": (1, True), "丙": (5, True)},
     None, None, [1, 3, 5], 1, 0, 0, 9),  # 全部被狙后流局
    ([2, 2, 1, 1], {"甲": (5, False)}, None, None, [1, 2], 0, 0, 6, 0),
    ([0, 2, 2], {"乙": (0, True)}, None, None, [0, 2], 1, 0, 4, 0),  # 移除后 0 也能获胜
])
def test_super_result_days_compare_all_supers_once_without_changing_logs(
    bids, super_bids, winner, winning_bid, collisions, changed, collision_hits, normal_burn, sniped_burn,
):
    days = [
        make_day(1, bids, super_bids={name: value for name, (value, _) in super_bids.items()}),
        make_day(2, [0] * len(bids)),
    ]
    # 场景本身由引擎结算，这里确认它就是参数描述的那种情况。
    assert (days[0].winner, days[0].winning_bid, days[0].collision_values) == (winner, winning_bid, collisions)
    assert {result.player: result.super_hit for result in days[0].players if result.super_bid is not None} == {
        name: hit for name, (_, hit) in super_bids.items()}
    matches = [{"match": make_log(days=days, game=ANALYZE_GAME).to_dict()}]
    original = deepcopy(matches)
    stats = collect_player_stats(matches)
    assert stats["super_days"] == 1
    assert stats["super_changed_days"] == changed
    assert stats["super_collision_hits"] == collision_hits
    assert sum(row["normal_collision_burn"] for row in stats["rows"]) == normal_burn
    assert sum(row["unique_sniped_burn"] for row in stats["rows"]) == sniped_burn
    assert sum(row["collision_burn"] for row in stats["rows"]) == normal_burn + sniped_burn
    for row in stats["rows"]:
        assert row["collision_burn"] == row["normal_collision_burn"] + row["unique_sniped_burn"]
    assert matches == original


def test_super_result_days_without_super_usage(tmp_path, capsys):
    matches = [{"match": make_match_2()}]
    matches[0]["match"]["match_index"] = 1
    stats = collect_player_stats(matches)
    assert stats["super_days"] == 0
    assert stats["super_changed_days"] == 0
    assert stats["super_collision_hits"] == 0
    assert all(row["super_net_coins"] == 0 for row in stats["rows"])
    write_matches(tmp_path, matches)
    analyze(tmp_path)
    output = capsys.readouterr().out
    assert "超级出价：使用 0 次，命中 0 次\n" in output


def test_super_collision_hits_include_void_days(tmp_path, capsys):
    days = [
        make_day(1, [0, 0, 3, 3], super_bids={"乙": 3, "丙": 0, "丁": 0}),
        make_day(2, [0, 0, 0, 0]),
    ]
    matches = [{"match": make_log(days=days, game=ANALYZE_GAME).to_dict()}]
    stats = collect_player_stats(matches)
    assert stats["super_collision_hits"] == 3  # 同一天、两个价位，按三笔使用分别计数。
    assert stats["super_changed_days"] == 0
    write_matches(tmp_path, matches)
    analyze(tmp_path)
    output = capsys.readouterr().out
    assert "命中原本互撞价 3 次（按超级出价笔数计，包含流局）" in output
    assert "撞车损失：6 金币（正常出价互撞 6，唯一正常出价被狙 0；只计正常出价）" in output


def test_super_result_days_do_not_simulate_future():
    game = replace(ANALYZE_GAME, daily_salary=0)
    days = [
        make_day(1, [0, 5, 3], super_bids={"甲": 6}, game=game),
        make_day(2, [0, 0, 3], game=game),
    ]
    # 移除首日超级出价后只重判首日，不重算次日余额或出价。
    log = make_log(days=days, game=game).to_dict()
    stats = collect_player_stats([{"match": log}])
    assert stats["super_days"] == 1
    assert stats["super_changed_days"] == 0


def write_matches(run_dir, matches, *, run_type="offline", players=None, team_of=None):
    """team_of 是团队赛的玩家到阵营，写进快照和每局的累计排名。"""
    first = matches[0]["match"]
    roster = deepcopy(players) if players is not None else [{"name": name} for name in first["auction_scores"]]
    for player in roster:
        player.setdefault("identity_model", {"model": "synthetic-identity"})
        player.setdefault("execution_model", {"protocol": "chat", "model": "synthetic-execution"})
        if team_of is not None:
            player["team"] = team_of[player["name"]]
    paths = initialize_run(run_dir, {"run_type": run_type,
                                     "mode": "test_model" if run_type.endswith("-test") else "regular",
                                     "game": first["game"], "players": roster}, "test rules")
    # 模型玩家创建时就写下系统提示，每人都有任务日志。
    for pc in paths.read_config()["players"]:
        player_log(run_dir, pc).system_prompt("test system prompt")
    auction_scores_history = {name: [] for name in first["auction_scores"]}
    scores_history = {name: [] for name in first["scores"]}
    for payload in sorted(matches, key=lambda payload: payload["match"]["match_index"]):
        # 用生产排名函数准备磁盘元数据；各局分数取自场景的局日志。
        for name in auction_scores_history:
            auction_scores_history[name].append(payload["match"]["auction_scores"].get(name, 0))
            scores_history[name].append(payload["match"]["scores"].get(name, 0))
        payload["standings_after_match"] = [asdict(entry) for entry in make_standings(
            auction_scores_history, scores_history=scores_history, team_of=team_of)]
        paths.match(payload["match"]["match_index"]).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


def player_log(run_dir, pc):
    """用生产日志写入器追加到场次里这名玩家的任务日志。"""
    paths = RunPaths(run_dir)
    return AgentLog(pc["name"], paths.player(pc["number"]), readable_path=paths.player(pc["number"], readable=True))


def model_reply(input_tokens, output_tokens, cache_read_tokens=0):
    return ModelResponse(parts=[TextPart("回复")], usage=RequestUsage(
        input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cache_read_tokens))


def request_error(attempt):
    return {"request": 1, "attempt": attempt, "type": "ModelHTTPError", "message": "status_code: 524"}


def test_usage_sums_every_reply_by_player_and_task(tmp_path, capsys):
    run_dir = tmp_path / "20260909_120000_ai"
    write_matches(run_dir, make_matches(), run_type="ai")
    jia, yi = RunPaths(run_dir).read_config()["players"][:2]
    bid_entry = {"title": "第 1 局第 2 天出价", "task": "bid", "context": "第 1 局第 2 天"}
    bid = BidOutput("策略", 1, None, [])
    jia_log = player_log(run_dir, jia)
    jia_log.success({"title": "开局发言", "task": "speech", "context": "开局发言"},
                    ExecutionResult(SpeechOutput("策略", "发言", []), [ModelRequest(parts=[UserPromptPart("请发言")]),
                                             model_reply(100, 10, 40)], []))
    # 纠错往返的两条回复都计入，重发前失败的请求只计次数。
    jia_log.success(bid_entry, ExecutionResult(bid, [
        ModelRequest(parts=[UserPromptPart("请出价")]), model_reply(200, 20),
        ModelRequest(parts=[RetryPromptPart("格式错误")]), model_reply(300, 30, 150),
    ], [request_error(1)]))
    yi_log = player_log(run_dir, yi)
    yi_log.failure(bid_entry, CallFailedError("请求失败", messages=[ModelRequest(parts=[UserPromptPart("请出价")])],
                                              request_errors=[request_error(1), request_error(2)]))
    yi_log.success(bid_entry, ExecutionResult(bid, [ModelRequest(parts=[UserPromptPart("请出价")]),
                                                  model_reply(250, 25)], []))
    yi_log.success({"title": "观众总结（仅观众可见）", "task": "summary", "context": "观众总结（仅观众可见）"},
                   ExecutionResult(SummaryOutput("总结"), [ModelRequest(parts=[UserPromptPart("请总结")]),
                                                          model_reply(1000, 5)], []))

    analyze(run_dir)
    output = capsys.readouterr().out
    usage = output.split("模型用量（token）\n", 1)[1]
    assert usage.splitlines() == [
        "  输入含缓存命中，输出含推理；失败的请求没有用量记录，只计次数",
        "  甲：请求 3 次，输入 600（缓存命中 190，32%），输出 60；另有 1 次请求失败",
        "  乙：请求 2 次，输入 1,250（缓存命中 0，0%），输出 30；另有 2 次请求失败",
        "  丙：请求 0 次，输入 0，输出 0",
        "  丁：请求 0 次，输入 0，输出 0",
        "  戊：请求 0 次，输入 0，输出 0",
        "  己：请求 0 次，输入 0，输出 0",
        "  按任务：",
        "  发言：请求 1 次，输入 100（缓存命中 40，40%），输出 10；最大单次输入 100，来自 甲 开局发言",
        "  出价：请求 3 次，输入 750（缓存命中 150，20%），输出 75；另有 3 次请求失败；"
        "最大单次输入 300，来自 甲 第 1 局第 2 天",
        "  观众总结：请求 1 次，输入 1,000（缓存命中 0，0%），输出 5；最大单次输入 1,000，来自 乙 观众总结（仅观众可见）",
    ]


def test_usage_lists_discussion_after_speech(tmp_path, capsys):
    run_dir = tmp_path / "20260909_120000_team"
    write_matches(run_dir, make_matches(), run_type="team", team_of=TEAM_OF)
    log = player_log(run_dir, RunPaths(run_dir).read_config()["players"][0])
    for title, task, output in (("开局讨论第 1 轮", "discussion", DiscussionOutput("策略", "信")),
                                ("开局发言", "speech", SpeechOutput("策略", "发言", []))):
        log.success({"title": title, "task": task, "context": title},
                    ExecutionResult(output, [ModelRequest(parts=[UserPromptPart("请求")]), model_reply(100, 10)], []))
    analyze(run_dir)
    usage = capsys.readouterr().out.split("  按任务：\n", 1)[1]
    assert usage.splitlines()[:2] == [
        "  发言：请求 1 次，输入 100（缓存命中 0，0%），输出 10；最大单次输入 100，来自 甲 开局发言",
        "  讨论：请求 1 次，输入 100（缓存命中 0，0%），输出 10；最大单次输入 100，来自 甲 开局讨论第 1 轮",
    ]


def test_usage_lists_failed_only_task_without_largest_input(tmp_path, capsys):
    run_dir = tmp_path / "20260909_120000_ai-test"
    write_matches(run_dir, make_matches(), run_type="ai-test")
    jia = RunPaths(run_dir).read_config()["players"][0]
    player_log(run_dir, jia).failure(
        {"title": "观众总结（仅观众可见）", "task": "summary", "context": "观众总结（仅观众可见）"},
        CallFailedError("请求失败", messages=[ModelRequest(parts=[UserPromptPart("请总结")])],
                        request_errors=[request_error(1)]))
    analyze(run_dir)
    assert "  观众总结：请求 0 次，输入 0，输出 0；另有 1 次请求失败\n" in capsys.readouterr().out


@pytest.mark.parametrize("latest_type,older_type", [
    ("ai-test", "ai"), ("ai", "ai-test"), ("team", "ai"), ("team-test", "team"), ("ai", "team-test"),
])
def test_default_analysis_selects_latest_real_ai(tmp_path, monkeypatch, latest_type, older_type):
    monkeypatch.setattr("auction_game.reporting.analyze.LOG_DIR", tmp_path)
    write_matches(tmp_path / f"20261016_110000_{older_type}", make_matches(), run_type=older_type)
    latest = tmp_path / f"20261016_120000_{latest_type}"
    write_matches(latest, make_matches(), run_type=latest_type)
    write_matches(tmp_path / "20261016_130000_offline", make_matches())
    write_matches(tmp_path / "20261016_130000_team-offline", make_matches(), run_type="team-offline")
    (tmp_path / "20261016_140000_ai").mkdir()  # 还没有完成的局
    write_matches(tmp_path / "archive_20261016_160000_ai", make_matches(), run_type="ai")
    assert find_latest_ai_run() == latest


@pytest.mark.parametrize("run_type", ["ai", "ai-test"])
def test_default_analysis_orders_collision_suffix_numerically(tmp_path, monkeypatch, run_type):
    monkeypatch.setattr("auction_game.reporting.analyze.LOG_DIR", tmp_path)
    for suffix in ("", "_2", "_10"):
        write_matches(tmp_path / f"20261016_120000_{run_type}{suffix}", make_matches(), run_type=run_type)
    assert find_latest_ai_run() == tmp_path / f"20261016_120000_{run_type}_10"


def test_default_cli_keeps_test_run_type_and_actual_models(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("auction_game.reporting.analyze.LOG_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["analyze"])
    players = [{"name": name, "identity_model": {"model": f"identity-{index}"},
                "execution_model": {"protocol": "responses", "model": "actual-test-model"}}
               for index, name in enumerate(make_match_1()["auction_scores"], 1)]
    write_matches(tmp_path / "20261016_120000_ai-test", make_matches(), run_type="ai-test", players=players)
    main()
    output = capsys.readouterr().out
    assert "运行类型：测试模型真实 AI（收费）" in output
    assert "测试运行结果（非身份模型成绩）" in output
    assert "甲：身份 identity-1 → 实际 responses / actual-test-model" in output


def test_default_analysis_rejects_only_offline(tmp_path, monkeypatch):
    monkeypatch.setattr("auction_game.reporting.analyze.LOG_DIR", tmp_path)
    write_matches(tmp_path / "20261016_120000_offline", make_matches())
    write_matches(tmp_path / "20261016_120000_team-offline", make_matches(), run_type="team-offline")
    with pytest.raises(SystemExit, match="没有.*ai、ai-test、team 或 team-test"):
        find_latest_ai_run()


def test_team_run_reports_team_totals_and_usage(tmp_path, capsys):
    # 红队甲乙丙：第 1 局乙 3 分，第 2 局乙 1 分；蓝队丁戊己：第 1 局己 1 分，第 2 局戊 1 分。
    write_matches(tmp_path / "20260909_120000_team", make_matches(), run_type="team", team_of=TEAM_OF)
    analyze(tmp_path / "20260909_120000_team")
    output = capsys.readouterr().out
    assert ("\n\n阵营总分\n  各局顺序：正赛第 1 局 / 正赛第 2 局\n"
            "  红队：4 分（拍卖 2 分，命中 2 分），各局 3 / 1\n"
            "  蓝队：2 分（拍卖 2 分，命中 0 分），各局 1 / 1\n\n阵营配合\n") in output
    assert "运行类型：团队赛常规真实 AI" in output and "模型用量（token）" in output


def make_team_match() -> dict:
    """按阵营结算的一局，甲乙丙红队、丁戊己蓝队。

    第 1 天：出价 [3,3,5,5,5,1]；甲乙同出 3，红队队内撞车；丁戊同出 5，另有红队丙，蓝队也算队内撞车。
              丙狙 3 只狙到队友，丁狙 6 是空位，己狙 5 命中丙；己以 1 获胜。
    第 2 天：出价 [2,2,2,0,3,6]；红队三人同出 2，算一次队内撞车。甲狙 3 命中戊，戊狙 6 只狙到队友己；己以 6 获胜。
    """
    days = [
        make_day(1, [3, 3, 5, 5, 5, 1], super_bids={"丙": 3, "丁": 6, "己": 5}, game=ANALYZE_GAME, team_of=TEAM_OF),
        make_day(2, [2, 2, 2, 0, 3, 6], super_bids={"甲": 3, "戊": 6}, game=ANALYZE_GAME, team_of=TEAM_OF),
    ]
    return make_log(days=days, game=ANALYZE_GAME).to_dict()


def test_team_run_reports_team_play(tmp_path, capsys):
    match = make_team_match()
    # 场景本身由引擎按阵营结算，这里确认它就是 make_team_match 描述的那种情况。
    assert [{result["player"]: result["super_hit"] for result in day["players"] if result["super_bid"] is not None}
            for day in match["days"]] == [{"丙": False, "丁": False, "己": True}, {"甲": True, "戊": False}]
    assert [day["winner"] for day in match["days"]] == ["己", "己"]
    assert collect_team_stats([{"match": match}], TEAM_OF) == {
        "红队": Counter(team_collisions=2, super_uses=2, super_hits=1, teammate_only_misses=1),
        "蓝队": Counter(team_collisions=1, super_uses=3, super_hits=1, teammate_only_misses=1),
    }
    write_matches(tmp_path, [{"match": match}], run_type="team-offline", team_of=TEAM_OF)
    analyze(tmp_path)
    # 阵营顺序同阵营总分：蓝队己两天获胜并命中一次，排在前面。
    assert ("\n\n阵营配合\n"
            "  队内撞车按天和价位计次：同一价位有 2 笔及以上队友的正常出价就算一次，另有对方出价也算\n"
            "  蓝队：队内撞车 1 次；超级出价 3 次，命中 1 次（33%），只狙到队友 1 次\n"
            "  红队：队内撞车 2 次；超级出价 2 次，命中 1 次（50%），只狙到队友 1 次\n"
            "\n经济与超级出价\n") in capsys.readouterr().out


def test_team_play_without_supers_omits_hit_rate(tmp_path, capsys):
    match = make_log(days=[make_day(1, [0, 1, 2, 3, 4, 5], game=ANALYZE_GAME, team_of=TEAM_OF)],
                     game=ANALYZE_GAME).to_dict()
    write_matches(tmp_path, [{"match": match}], run_type="team-offline", team_of=TEAM_OF)
    analyze(tmp_path)
    assert "  红队：队内撞车 0 次；超级出价 0 次，命中 0 次，只狙到队友 0 次\n" in capsys.readouterr().out


def test_cli_reports_run_without_matches(tmp_path, monkeypatch):
    initialize_run(tmp_path, {"run_type": "offline", "mode": "regular", "game": ANALYZE_GAME.to_dict(),
                              "players": [{"name": "甲"}, {"name": "乙"}]}, "test rules")
    monkeypatch.setattr("sys.argv", ["analyze", str(tmp_path)])
    with pytest.raises(SystemExit, match="没有 data/matches"):
        main()


def test_nondefault_snapshot_drives_economy_cap_and_playoff_label(tmp_path, capsys, monkeypatch):
    game = replace(TEST_GAME, initial_coins=20, daily_salary=4, daily_pool=(2, 4, 3),
                   max_bid=9, super_cost=1, super_bounty=5, regular_matches=2, max_playoffs=4)
    days = [
        make_day(1, [9, 7, 5], pool=2, super_bids={"乙": 9}, game=game),
        make_day(2, [2, 2, 0], pool=4, game=game),
        make_day(3, [9, 3, 1], pool=3, game=game),
    ]
    log = make_log(match_index=3, is_playoff=True, game=game, days=days).to_dict()
    stats = collect_player_stats([{"match": log}])
    assert stats["match_labels"] == ["加赛第 1 局（总第 3 局）"]
    assert rows_by_name(stats)["丙"]["tied_top_matches"] == 1
    assert rows_by_name(stats)["乙"]["avg_bid"] == 4
    disk_matches = [{"match": {**deepcopy(log), "match_index": index, "is_playoff": index == 3}}
                    for index in (1, 2, 3)]
    write_matches(tmp_path, disk_matches)
    # 分析不能依赖当前配置；当前目录甚至放置无效的 game.yaml。
    (tmp_path / "game.yaml").write_text("not: valid game settings", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    analyze(tmp_path)
    output = capsys.readouterr().out
    assert "获胜价=9（出价上限）占比 33.3%" in output
    assert "成本消耗：3 金币" in output
    assert "悬赏发放：15 金币" in output
    assert "超级出价净金币变化：+12 金币" in output
    assert "加赛第 1 局（总第 3 局）" in output
    assert rows_by_name(stats)["乙"]["super_net_coins"] == 4
    assert "局末平均剩余金币：19.67（最低 8，最高 28）" in output
    assert "全天总体：平均 4.70（30 笔）" in output  # 聚合均价包含超级出价数值


def test_loading_allows_completed_matches_from_unfinished_run(tmp_path, capsys):
    match = make_match_1()
    match["game"]["regular_matches"] = 5
    matches = [{"match": match}]
    write_matches(tmp_path, matches)
    assert load_match_logs(tmp_path) == matches
    analyze(tmp_path)
    assert "局数：1（正赛 1 + 加赛 0）" in capsys.readouterr().out


def test_loading_orders_matches_by_file_number(tmp_path):
    matches = make_matches()
    matches = [{"match": {**deepcopy(matches[index % 2]["match"]), "match_index": index + 1}}
               for index in range(10)]
    write_matches(tmp_path, list(reversed(matches)))
    assert load_match_logs(tmp_path) == matches


def test_mean_bids_exclude_void_days(tmp_path, capsys):
    days = [
        make_day(1, [2, 2, 2, 2, 2, 2]),
        make_day(2, [0, 3, 0, 2, 5, 4]),
    ]
    match = make_log(days=days, game=ANALYZE_GAME).to_dict()
    rows = rows_by_name(collect_player_stats([{"match": match}]))
    assert rows["甲"]["avg_bid"] == 0
    assert rows["乙"]["avg_bid"] == 3
    assert rows["甲"]["collision_burn"] == 2  # 流局日撞车仍然扣币
    assert rows["甲"]["normal_collision_burn"] == 2
    assert rows["甲"]["unique_sniped_burn"] == 0
    write_matches(tmp_path, [{"match": match}])
    analyze(tmp_path)
    output = capsys.readouterr().out
    assert "第 1 天：出价 —（全流局），奖池 1.00" in output
    assert "第 2 天：出价 2.33（6 笔），奖池 1.00" in output
    assert "全天总体：平均 2.33（6 笔）" in output


def test_aggregate_zero_bids_include_super_but_player_zero_bids_only_normal(tmp_path, capsys):
    days = [
        make_day(1, [0, 0, 1, 1, 0, 0], super_bids={"丙": 0}),
        make_day(2, [1, 0, 2, 3, 4, 5], super_bids={"甲": 0}),
    ]
    match = make_log(days=days, game=ANALYZE_GAME).to_dict()
    rows = rows_by_name(collect_player_stats([{"match": match}]))
    assert rows["甲"]["zero_bids"] == 1  # 流局日正常 0 计入，两笔狙 0 的超级出价不计
    assert rows["乙"]["zero_bids"] == 2
    write_matches(tmp_path, [{"match": match}])
    analyze(tmp_path)
    assert "出 0 共 7 笔（含流局日和超级出价），其中 0 价获胜 0 次" in capsys.readouterr().out


def test_total_score_ranking_can_reverse_auction_score_ranking():
    game = replace(ANALYZE_GAME, daily_pool=(1, 1), super_hit_score=3)
    days = [
        make_day(1, [5, 4, 3], super_bids={"乙": 3}, game=game),
        make_day(2, [5, 1, 0], game=game),
    ]
    match = make_log(days=days, game=game).to_dict()
    stats = collect_player_stats([{"match": match}])
    assert [row["name"] for row in stats["rows"]] == ["乙", "甲", "丙"]
    assert rows_by_name(stats)["乙"]["solo_top_matches"] == 1
    assert stats["auction_score_matrix"]["乙"] == [0]
    assert stats["score_matrix"]["乙"] == [3]


def test_offline_run_reports_fake_execution_and_configured_model(tmp_path, capsys):
    players = [{"name": name, "identity_model": {"model": "identity"},
                "execution_model": {"protocol": "responses", "model": "configured-model"}}
               for name in make_match_1()["auction_scores"]]
    write_matches(tmp_path, make_matches(), run_type="offline", players=players)
    analyze(tmp_path)
    output = capsys.readouterr().out
    assert "实际执行为假模型" in output
    assert "配置模型 responses / configured-model" in output
    assert "常规阵容结果" not in output
    assert "→ 实际 responses" not in output
    assert "模型用量" not in output
