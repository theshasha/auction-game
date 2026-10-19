"""运行输出与本场日志展示。"""

from types import SimpleNamespace
from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from auction_game.ai import prompts
from auction_game.game.rules import render_rules
from auction_game.game.players import ScriptedPlayer
from auction_game.game.tournament import run_tournament
from auction_game.reporting.run_output import initialize_run, make_publish, print_day_result, print_final_result
from tests.support.factories import NAMES, TEST_GAME, bids_by_day, make_standings, run_scenario


def test_daily_report_shows_winner_hits_void_pool_discard_and_settled_balances(capsys):
    game = replace(TEST_GAME, initial_coins=20, max_bid=9, super_bounty=5,
                   super_hit_score=3, daily_pool=(1, 2, 3), regular_matches=1, max_playoffs=0)
    strategy = bids_by_day({1: [4, 4, 9, 2, 1, 0], 2: [2, 2, 1, 1, 0, 0], 3: [0] * 6})
    supers = {"丁": lambda obs: {1: 4, 2: 2}.get(obs.day),
              "己": lambda obs: 4 if obs.day == 1 else None}
    players = [ScriptedPlayer(name, strategy, supers.get(name)) for name in NAMES]
    reports = []

    def report(log, standings, coins):
        before = deepcopy(log.to_dict())
        print_day_result(log, standings, coins)
        assert log.to_dict() == before
        reports.append(capsys.readouterr().out)

    run_tournament(players, game, on_day_end=report)
    first, second, last = reports
    assert "第 1 局（正赛）第 1 天" in first
    assert "奖池 1 分；丙 以 9 金币获胜。" in first
    assert "超级命中：丁 +3 分；己 +3 分。" in first
    assert "| 排名 | 玩家 | 累计总分 | 金币（结算后） |" in first
    assert "| 1 | 丁 | 3 | 23 |" in first
    assert "| 1 | 己 | 3 | 23 |" in first
    assert "| 3 | 丙 | 1 | 11 |" in first
    assert first.index("| 1 | 丁") < first.index("| 3 | 丙")
    assert "奖池 2 分；流局，奖池作废。" in second
    assert "超级命中：丁 +3 分。" in second
    assert "奖池 3 分；流局，奖池作废。" in last
    assert "超级命中：无。" in last
    assert all(word not in "".join(reports) for word in ["争冠", "上限", "锁冠", "私有分析"])


@pytest.mark.parametrize("tied", [False, True])
def test_final_result_counts_actual_regular_matches_and_reports_scores(capsys, tied):
    standings = make_standings(
        {"甲": [3, 3, 2], "乙": [1, 1, 0]},
        scores_history={"甲": [12, 12, 8], "乙": [12, 12, 8 if tied else 9]},
    )
    result = SimpleNamespace(
        match_logs=[SimpleNamespace(is_playoff=value) for value in (False, False, True)],
        playoff_count=1,
        playoff_cap_hit=tied,
        final_standings=standings,
    )
    print_final_result(result)
    output = capsys.readouterr().out
    assert "共进行 3 局（正赛 2 局 + 加赛 1 局）" in output
    assert prompts.render_standings(standings) in output
    if tied:
        names = "、".join(entry.player for entry in standings)
        assert f"并列冠军: {names}（32 分）" in output
        assert "加赛已达上限，保留并列冠军" in output
    else:
        assert "冠军: 乙（33 分）" in output
        assert "加赛已达上限" not in output


@pytest.mark.parametrize("run_type,mode,expected", [
    ("offline", "test_model", "全员使用假模型，无真实调用。"),
    ("bot", "regular", "全员使用本地 Bot，无模型调用。"),
    ("ai-test", "test_model", "全员调用测试模型，角色身份不变。"),
    ("ai", "regular", "各玩家使用配置中的模型。"),
])
def test_readme_describes_actual_model_usage(tmp_path, run_type, mode, expected):
    paths = initialize_run(
        tmp_path / "run",
        {"run_type": run_type, "mode": mode, "game": TEST_GAME.to_dict(),
         "players": [{"name": name, "identity_model": {"model": "identity"},
                      "execution_model": {"protocol": "chat", "model": "execution"}} for name in NAMES]},
        render_rules(TEST_GAME, NAMES),
    )
    assert expected in (paths.root / "README.md").read_text(encoding="utf-8")


def test_match_report_is_concise_and_json_keeps_full_settlement(tmp_path):
    game = replace(TEST_GAME, initial_coins=20, max_bid=9, super_bounty=5, daily_pool=(1, 2, 3))
    log = run_scenario(
        {1: [4, 4, 9, 2, 1, 0], 2: [2, 2, 1, 1, 0, 0], 3: [0, 0, 1, 1, 2, 2]},
        super_strategies={
            "乙": lambda obs: 3 if obs.day == 1 else None,
            "丁": lambda obs: {1: 4, 2: 2}.get(obs.day),
        },
        game=game,
    )
    standings = make_standings(
        {name: [score] for name, score in log.auction_scores.items()},
        scores_history={name: [score] for name, score in log.scores.items()},
    )
    expected = deepcopy({"match": log.to_dict(), "standings_after_match": [asdict(e) for e in standings]})
    paths = initialize_run(
        tmp_path / "run",
        {"run_type": "bot", "mode": "regular", "game": game.to_dict(),
         "players": [{"name": name} for name in NAMES]},
        render_rules(game, NAMES),
    )
    make_publish(paths.root)(log, standings)

    report = paths.match(1, readable=True).read_text(encoding="utf-8")
    assert "[本场规则](../RULES.md)" in report
    assert (paths.match(1, readable=True).parent / "../RULES.md").resolve() == paths.root / "RULES.md"
    assert "奖池和得分的单位为分，正常出价的单位为金币。\n\n| 天 | 奖池 | 甲 | 乙 | 丙 | 丁 | 戊 | 己 | 结果 | 撞车价位 | 当日得分 |" in report
    assert "4（撞车） | 4（撞车）；超级出价 3（落空） | 9（获胜） | 2（退款）；超级出价 4（命中）" in report
    assert report.count("| 流局（奖池作废） | 0、1、2 |") == 2
    assert "| 流局（奖池作废） | 0、1、2 | 丁 2 |" in report
    assert "| 3 | 3 |" in report
    for text in [report, prompts.match_end_speech_request(log, standings),
                 prompts.final_speech_request(standings, log)]:
        assert prompts.render_match_log(log) in text
        assert all(word not in text for word in ["，扣", "成本", "悬赏", "奖池并入次日"])

    payload = json.loads(paths.match(1).read_text(encoding="utf-8"))
    assert payload == expected
    assert log.to_dict() == expected["match"]
    assert payload["match"]["game"]["super_cost"] == 2
    assert payload["match"]["game"]["super_bounty"] == 5
    assert payload["match"]["scores"] == {"甲": 0, "乙": 0, "丙": 1, "丁": 4, "戊": 0, "己": 0}
    shared_standings = prompts.render_standings(standings)
    assert f"截至本局的累计排名：\n\n{shared_standings}" in report
    assert shared_standings in (paths.root / "README.md").read_text(encoding="utf-8")
    assert all(entry["total_score"] == log.scores[entry["player"]]
               for entry in payload["standings_after_match"])
    days = payload["match"]["days"]
    assert days[0]["collision_values"] == [4]
    assert days[0]["super_uses"] == [
        {"player": "乙", "value": 3, "hit": False},
        {"player": "丁", "value": 4, "hit": True},
    ]
    assert len(days[0]["bids"]) == 8
    assert days[2]["bids"][0] == {"player": "甲", "bid": 0, "is_super": False}
    assert days[1]["is_void"] and days[2]["is_void"]
    assert [day["pool"] for day in days] == [1, 2, 3]
    assert [day["auction_score"] for day in days] == [1, 0, 0]
    assert days[1]["super_uses"] == [{"player": "丁", "value": 2, "hit": True}]
