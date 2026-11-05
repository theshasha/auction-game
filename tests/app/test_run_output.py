"""运行输出与本场日志展示。"""

from types import SimpleNamespace
from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from auction_game.agent import prompts
from auction_game.game import tables
from auction_game.game.engine import compute_standings
from auction_game.game.models import MatchLog
from auction_game.game.rules import render_rules
from auction_game.reporting.run_output import (initialize_run, print_day_result, print_final_result, publish_match,
                                               write_readme)
from tests.support.factories import (NAMES, TEAM_OF, TEST_GAME, bids_by_day, make_day, make_lineup, make_log,
                                     make_standings, make_team_lineup, run_one_match, run_scenario)


def test_daily_report_shows_winner_hits_void_pool_discard_and_settled_balances(capsys):
    game = replace(TEST_GAME, initial_coins=20, max_bid=9, super_bounty=5,
                   super_hit_score=3, daily_pool=(1, 2, 3), regular_matches=1, max_playoffs=0)
    strategy = bids_by_day({1: [4, 4, 9, 2, 1, 0], 2: [2, 2, 1, 1, 0, 0], 3: [0] * 6})
    supers = {"丁": lambda obs: {1: 4, 2: 2}.get(obs.day),
              "己": lambda obs: 4 if obs.day == 1 else None}
    log = run_one_match({name: strategy for name in NAMES}, super_strategies=supers, game=game)
    reports = []
    for count in range(1, len(log.days) + 1):
        settled = MatchLog(log.match_index, log.is_playoff, game, log.days[:count])
        print_day_result(settled, compute_standings(NAMES, [], settled))
        reports.append(capsys.readouterr().out)
    first, second, last = reports
    assert "第 1 局（正赛）第 1 天" in first
    assert "奖池 1 分；丙 以 9 金币获胜。" in first
    assert "超级出价命中：丁 +3 分；己 +3 分。" in first
    assert "| 排名 | 玩家 | 累计总分 | 金币（结算后） |" in first
    assert "| 1 | 丁 | 3 | 23 |" in first
    assert "| 1 | 己 | 3 | 23 |" in first
    assert "| 3 | 丙 | 1 | 11 |" in first
    assert first.index("| 1 | 丁") < first.index("| 3 | 丙")
    assert "奖池 2 分；流局，奖池作废。" in second
    assert "超级出价命中：丁 +3 分。" in second
    assert "奖池 3 分；流局，奖池作废。" in last
    assert "超级出价命中：无。" in last


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
    assert tables.render_standings(standings) in output
    if tied:
        names = "、".join(entry.player for entry in standings)
        assert f"并列冠军: {names}（32 分）" in output
        assert "加赛已达上限，保留并列冠军" in output
    else:
        assert "冠军: 乙（33 分）" in output
        assert "加赛已达上限" not in output


def test_daily_report_lists_team_totals_before_players(capsys):
    # 甲以 5 获胜：红队 4 分，蓝队 0 分。
    log = make_log(days=[make_day(1, [5, 4, 3, 2, 1, 0], pool=4, team_of=TEAM_OF)])
    print_day_result(log, compute_standings(NAMES, [], log, team_of=TEAM_OF))
    output = capsys.readouterr().out
    assert "超级出价命中：无。\n阵营累计总分：红队 4 分、蓝队 0 分。\n| 排名 | 玩家 | 累计总分 | 金币（结算后） |" in output


@pytest.mark.parametrize("cap_hit", [False, True])
def test_final_result_announces_winning_team_or_draw(capsys, cap_hit):
    # 红队甲 3、乙 1；蓝队丁 2，己 1 或 2。
    standings = make_standings({name: [0] for name in NAMES}, team_of=TEAM_OF,
                               scores_history=dict(zip(NAMES, [[3], [1], [0], [2], [0], [2 if cap_hit else 1]])))
    result = SimpleNamespace(match_logs=[SimpleNamespace(is_playoff=False)], playoff_count=0,
                             playoff_cap_hit=cap_hit, final_standings=standings)
    print_final_result(result)
    output = capsys.readouterr().out
    assert tables.render_standings(standings) in output
    assert "冠军" not in output
    if cap_hit:
        assert "加赛已达上限，算平局。" in output and "平局: 红队、蓝队各 4 分" in output
    else:
        assert "获胜阵营: 红队（4 比 3）" in output and "加赛已达上限" not in output


@pytest.mark.parametrize("run_type,mode,expected", [
    ("offline", "test_model", "全员使用假模型，无真实调用。"),
    ("ai-test", "test_model", "全员调用测试模型，角色身份不变。"),
    ("ai", "regular", "各玩家使用配置中的模型。"),
    ("team-offline", "test_model", "全员使用假模型，无真实调用。"),
    ("team-test", "test_model", "全员调用测试模型，角色身份不变。"),
    ("team", "regular", "各玩家使用配置中的模型。"),
])
def test_readme_describes_actual_model_usage(tmp_path, run_type, mode, expected):
    paths = initialize_run(
        tmp_path / "run",
        {"run_type": run_type, "mode": mode, "game": TEST_GAME.to_dict(),
         "players": [{"name": name, "identity_model": {"model": "identity"},
                      "execution_model": {"protocol": "chat", "model": "execution"}} for name in NAMES]},
        render_rules(TEST_GAME, make_lineup()),
    )
    assert expected in (paths.root / "README.md").read_text(encoding="utf-8")


def test_team_readme_title_and_private_links_grouped_by_team(tmp_path):
    paths = initialize_run(
        tmp_path / "run",
        {"run_type": "team-offline", "mode": "regular", "game": TEST_GAME.to_dict(),
         "players": [{"name": name, "team": team} for name, team in TEAM_OF.items()]},
        render_rules(TEST_GAME, make_team_lineup()),
    )
    for number in (1, 2, 4):
        paths.player(number, readable=True).write_text("对话", encoding="utf-8")
    write_readme(paths)
    readme = (paths.root / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# 拍卖团队赛\n\n运行类型：团队赛离线模拟。\n\n全员使用假模型，无真实调用。")
    assert readme.endswith("## 私人对话（含私有策略，仅供赛后阅读）\n\n### 红队\n\n- [甲](players/001.md)\n\n"
                           "- [乙](players/002.md)\n\n### 蓝队\n\n- [丁](players/004.md)\n")
    assert "letters.md" not in readme
    paths.letters.write_text("信", encoding="utf-8")
    write_readme(paths)
    assert "\n[队内信件区](letters.md)\n" in (paths.root / "README.md").read_text(encoding="utf-8")


def test_readme_links_speeches_and_letters_once_they_exist(tmp_path):
    paths = initialize_run(
        tmp_path / "run",
        {"run_type": "offline", "mode": "regular", "game": TEST_GAME.to_dict(),
         "players": [{"name": name} for name in NAMES]},
        render_rules(TEST_GAME, make_lineup()),
    )
    readme = paths.root / "README.md"
    assert "speeches.md" not in readme.read_text(encoding="utf-8")
    assert "letters.md" not in readme.read_text(encoding="utf-8")
    paths.speeches.write_text("发言", encoding="utf-8")
    paths.letters.write_text("私信", encoding="utf-8")
    write_readme(paths)
    assert "[公开发言](speeches.md)\n\n[私信](letters.md)\n\n[逐局结果](matches.md)" in readme.read_text(encoding="utf-8")


def test_each_match_appends_its_section_to_one_report_in_order(tmp_path):
    game = replace(TEST_GAME, daily_pool=(1, 2), regular_matches=2, max_playoffs=1)
    paths = initialize_run(
        tmp_path / "run",
        {"run_type": "offline", "mode": "regular", "game": game.to_dict(),
         "players": [{"name": name} for name in NAMES]},
        render_rules(game, make_lineup()),
    )
    # 每局全员出同一个价，天天撞车流局；第 3 局是加赛。
    logs = []
    for index in (1, 2, 3):
        logs.append(run_one_match({name: lambda obs: obs.match_index for name in NAMES}, index, index > 2, game=game))
        publish_match(paths, logs[-1], compute_standings(NAMES, logs))

    sections = paths.matches_report.read_text(encoding="utf-8").split("\n## ")[1:]
    assert [section.split("\n", 1)[0] for section in sections] == [
        "第 1 局（正赛）", "第 2 局（正赛）", "第 3 局（加赛）"]
    for section, log in zip(sections, logs):
        assert tables.render_match_log(log) in section
    assert sections[-1].endswith(
        f"截至本局的累计排名：\n\n{tables.render_standings(compute_standings(NAMES, logs))}\n")
    assert "[逐局结果](matches.md)" in (paths.root / "README.md").read_text(encoding="utf-8")


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
        {"run_type": "offline", "mode": "regular", "game": game.to_dict(),
         "players": [{"name": name} for name in NAMES]},
        render_rules(game, make_lineup()),
    )
    publish_match(paths, log, standings)

    report = paths.matches_report.read_text(encoding="utf-8")
    assert "[本场规则](RULES.md)" in report
    assert (paths.matches_report.parent / "RULES.md").is_file()
    assert "奖池和得分的单位为分，正常出价的单位为金币。\n\n| 天 | 奖池 | 甲 | 乙 | 丙 | 丁 | 戊 | 己 | 结果 | 撞车价位 | 当日得分 |" in report
    assert "4（撞车） | 4（撞车）；超级出价 3（落空） | 9（获胜） | 2（退款）；超级出价 4（命中）" in report
    assert report.count("| 流局（奖池作废） | 0、1、2 |") == 2
    assert "| 流局（奖池作废） | 0、1、2 | 丁 2 |" in report
    assert "| 3 | 3 |" in report
    for text in [report, prompts.match_end_speech_request(log, standings),
                 prompts.final_speech_request(standings, log)]:
        assert tables.render_match_log(log) in text

    payload = json.loads(paths.match(1).read_text(encoding="utf-8"))
    assert payload == expected
    assert log.to_dict() == expected["match"]
    assert payload["match"]["game"]["super_cost"] == 2
    assert payload["match"]["game"]["super_bounty"] == 5
    assert payload["match"]["scores"] == {"甲": 0, "乙": 0, "丙": 1, "丁": 4, "戊": 0, "己": 0}
    shared_standings = tables.render_standings(standings)
    assert f"截至本局的累计排名：\n\n{shared_standings}" in report
    assert shared_standings in (paths.root / "README.md").read_text(encoding="utf-8")
    assert all(entry["total_score"] == log.scores[entry["player"]]
               for entry in payload["standings_after_match"])
    days = payload["match"]["days"]
    # 首日甲乙的 4 被丁狙中，乙狙 3 落空，丙以 9 获胜。
    assert days[0]["players"][:4] == [
        {"player": "甲", "bid": 4, "super_bid": None, "outcome": "collided", "sniped": True,
         "super_hit": False, "coins_delta": -4, "score": 0},
        {"player": "乙", "bid": 4, "super_bid": 3, "outcome": "collided", "sniped": True,
         "super_hit": False, "coins_delta": -6, "score": 0},
        {"player": "丙", "bid": 9, "super_bid": None, "outcome": "won", "sniped": False,
         "super_hit": False, "coins_delta": -9, "score": 1},
        {"player": "丁", "bid": 2, "super_bid": 4, "outcome": "refunded", "sniped": False,
         "super_hit": True, "coins_delta": 3, "score": 2},
    ]
    assert (days[0]["winner"], days[0]["winning_bid"]) == ("丙", 9)
    assert days[1]["winner"] is None and days[2]["winner"] is None
    assert [day["pool"] for day in days] == [1, 2, 3]
    assert [(result["player"], result["super_hit"]) for result in days[1]["players"]
            if result["super_bid"] is not None] == [("丁", True)]
