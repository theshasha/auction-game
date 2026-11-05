"""analyze 命令的输出；合成对局见 tests/support/analyze_runs.py，计算口径见 tests/reporting/test_analyze.py。"""

from copy import deepcopy
from dataclasses import replace

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart, TextPart, UserPromptPart
from pydantic_ai.usage import RequestUsage

from auction_game.agent.tasks import BidOutput, DiscussionOutput, SpeechOutput, SummaryOutput
from auction_game.cli.analyze import analyze, main
from auction_game.llm.execution import CallFailedError, ExecutionResult
from auction_game.reporting.run_output import initialize_run
from auction_game.reporting.run_paths import RunPaths
from tests.support.analyze_runs import (ANALYZE_GAME, make_match_1, make_match_2, make_matches, make_team_match,
                                        player_log, write_matches)
from tests.support.factories import TEAM_OF, TEST_GAME, make_day, make_log


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


def test_team_run_reports_team_totals_and_usage(tmp_path, capsys):
    # 红队甲乙丙：第 1 局乙 3 分，第 2 局乙 1 分；蓝队丁戊己：第 1 局己 1 分，第 2 局戊 1 分。
    write_matches(tmp_path / "20260909_120000_team", make_matches(), run_type="team", team_of=TEAM_OF)
    analyze(tmp_path / "20260909_120000_team")
    output = capsys.readouterr().out
    assert ("\n\n阵营总分\n  各局顺序：正赛第 1 局 / 正赛第 2 局\n"
            "  红队：4 分（拍卖 2 分，命中 2 分），各局 3 / 1\n"
            "  蓝队：2 分（拍卖 2 分，命中 0 分），各局 1 / 1\n\n阵营配合\n") in output
    assert "运行类型：团队赛常规真实 AI" in output and "模型用量（token）" in output


def test_team_play_without_supers_omits_hit_rate(tmp_path, capsys):
    match = make_log(days=[make_day(1, [0, 1, 2, 3, 4, 5], game=ANALYZE_GAME, team_of=TEAM_OF)],
                     game=ANALYZE_GAME).to_dict()
    write_matches(tmp_path, [{"match": match}], run_type="team-offline", team_of=TEAM_OF)
    analyze(tmp_path)
    assert "  红队：队内撞车 0 次；超级出价 0 次，命中 0 次，只狙到队友 0 次\n" in capsys.readouterr().out


def test_cli_reports_run_without_matches(tmp_path, monkeypatch):
    initialize_run(tmp_path, {"run_type": "offline", "mode": "regular", "game": ANALYZE_GAME.to_dict(),
                              "players": [{"name": "甲", "team": None}, {"name": "乙", "team": None}]}, "test rules")
    monkeypatch.setattr("sys.argv", ["analyze", str(tmp_path)])
    with pytest.raises(SystemExit, match="没有 data/matches"):
        main()


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


def test_all_void_match_prints_tied_top_and_no_average(tmp_path, capsys):
    days = [make_day(day, [2, 2]) for day in (1, 2)]
    write_matches(tmp_path, [{"match": make_log(days=days, game=ANALYZE_GAME).to_dict()}])
    analyze(tmp_path)
    output = capsys.readouterr().out
    assert output.count("单局最高总分：独占0局，并列1局") == 2
    assert output.count("平均正常出价（不含流局日）：—") == 2


def test_no_super_usage_omits_hit_rate(tmp_path, capsys):
    write_matches(tmp_path, [{"match": {**make_match_2(), "match_index": 1}}])
    analyze(tmp_path)
    assert "超级出价：使用 0 次，命中 0 次\n" in capsys.readouterr().out


def test_collision_hits_on_void_day_are_printed(tmp_path, capsys):
    days = [
        make_day(1, [0, 0, 3, 3], super_bids={"乙": 3, "丙": 0, "丁": 0}),
        make_day(2, [0, 0, 0, 0]),
    ]
    write_matches(tmp_path, [{"match": make_log(days=days, game=ANALYZE_GAME).to_dict()}])
    analyze(tmp_path)
    output = capsys.readouterr().out
    assert "命中原本互撞价 3 次（按超级出价笔数计，包含流局）" in output
    assert "撞车损失：6 金币（正常出价互撞 6，唯一正常出价被狙 0；只计正常出价）" in output


def test_team_run_reports_team_play(tmp_path, capsys):
    write_matches(tmp_path, [{"match": make_team_match()}], run_type="team-offline", team_of=TEAM_OF)
    analyze(tmp_path)
    # 阵营顺序同阵营总分：蓝队己两天获胜并命中一次，排在前面。
    assert ("\n\n阵营配合\n"
            "  队内撞车按天和价位计次：同一价位有 2 笔及以上队友的正常出价就算一次，另有对方出价也算\n"
            "  蓝队：队内撞车 1 次；超级出价 3 次，命中 1 次（33%），只狙到队友 1 次\n"
            "  红队：队内撞车 2 次；超级出价 2 次，命中 1 次（50%），只狙到队友 1 次\n"
            "\n经济与超级出价\n") in capsys.readouterr().out


def test_nondefault_snapshot_drives_economy_cap_and_playoff_label(tmp_path, capsys, monkeypatch):
    game = replace(TEST_GAME, initial_coins=20, daily_salary=4, daily_pool=(2, 4, 3),
                   max_bid=9, super_cost=1, super_bounty=5, regular_matches=2, max_playoffs=4)
    days = [
        make_day(1, [9, 7, 5], pool=2, super_bids={"乙": 9}, game=game),
        make_day(2, [2, 2, 0], pool=4, game=game),
        make_day(3, [9, 3, 1], pool=3, game=game),
    ]
    log = make_log(match_index=3, is_playoff=True, game=game, days=days).to_dict()
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
    assert "局末平均剩余金币：19.67（最低 8，最高 28）" in output
    assert "全天总体：平均 4.70（30 笔）" in output  # 聚合均价包含超级出价数值


def test_unfinished_run_reports_completed_matches(tmp_path, capsys):
    match = make_match_1()
    match["game"]["regular_matches"] = 5
    write_matches(tmp_path, [{"match": match}])
    analyze(tmp_path)
    assert "局数：1（正赛 1 + 加赛 0）" in capsys.readouterr().out


def test_void_day_prints_no_mean_bid(tmp_path, capsys):
    days = [
        make_day(1, [2, 2, 2, 2, 2, 2]),
        make_day(2, [0, 3, 0, 2, 5, 4]),
    ]
    write_matches(tmp_path, [{"match": make_log(days=days, game=ANALYZE_GAME).to_dict()}])
    analyze(tmp_path)
    output = capsys.readouterr().out
    assert "第 1 天：出价 —（全流局），奖池 1.00" in output
    assert "第 2 天：出价 2.33（6 笔），奖池 1.00" in output
    assert "全天总体：平均 2.33（6 笔）" in output


def test_zero_bids_are_printed(tmp_path, capsys):
    days = [
        make_day(1, [0, 0, 1, 1, 0, 0], super_bids={"丙": 0}),
        make_day(2, [1, 0, 2, 3, 4, 5], super_bids={"甲": 0}),
    ]
    write_matches(tmp_path, [{"match": make_log(days=days, game=ANALYZE_GAME).to_dict()}])
    analyze(tmp_path)
    assert "出 0 共 7 笔（含流局日和超级出价），其中 0 价获胜 0 次" in capsys.readouterr().out
