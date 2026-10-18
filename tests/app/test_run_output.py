"""运行输出与本场日志展示。"""

from types import SimpleNamespace
from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from auction_game.ai import prompts
from auction_game.game.rules import render_rules
from auction_game.reporting.run_output import initialize_run, make_publish, print_final_result
from tests.support.factories import NAMES, TEST_GAME, make_standings, run_scenario


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
        {"run_type": run_type, "mode": mode, "game": TEST_GAME.to_dict(), "timeout_seconds": 3,
         "players": [{"name": name, "identity_model": {"model": "identity"},
                      "execution_model": {"protocol": "chat", "model": "execution", "max_tokens": 100,
                                          "base_url": "https://offline.invalid/v1"}} for name in NAMES]},
        render_rules(TEST_GAME, NAMES),
    )
    assert expected in (paths.root / "README.md").read_text(encoding="utf-8")


def test_match_report_is_concise_and_json_keeps_full_settlement(tmp_path):
    game = replace(TEST_GAME, initial_coins=20, max_bid=9, super_bounty=5, daily_pool=(1, 2, 3))
    log = run_scenario(
        {1: [4, 4, 9, 2, 1, 0], 2: [2, 2, 1, 1, 0, 0], 3: [-1, 0, 1, 1, 2, 2]},
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
    assert "| 流局 | 0、1、2 |" in report
    assert "| 流局 | 0、1、2 | 丁 2 |" in report
    assert "| 3 | 5 |" in report and "| 流局（奖池作废） | 0、1、2 |" in report
    for text in [report, prompts.match_end_speech_request(log, standings, True),
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
    assert days[2]["bids"][0] == {
        "player": "甲", "raw_bid": -1, "bid": 0, "status": "illegal", "is_super": False,
    }
    assert days[1]["is_void"] and days[2]["is_void"]
    assert days[1]["super_uses"] == [{"player": "丁", "value": 2, "hit": True}]
