"""运行输出与本场日志展示。"""

from types import SimpleNamespace
from copy import deepcopy
from dataclasses import asdict, replace
import json

from auction_game.ai import prompts
from auction_game.game.rules import render_rules
from auction_game.reporting.run_output import initialize_run, make_publish, print_final_result
from tests.support.factories import NAMES, TEST_GAME, make_standings, run_scenario


def test_final_result_counts_actual_regular_matches(capsys):
    result = SimpleNamespace(
        match_logs=[SimpleNamespace(is_playoff=value) for value in (False, False, True)],
        playoff_count=1,
        playoff_cap_hit=False,
        final_standings=[SimpleNamespace(rank=1, player="甲", total_wins=8, wins_per_match=[3, 3, 2])],
    )
    print_final_result(result)
    assert "共进行 3 局（正赛 2 局 + 加赛 1 局）" in capsys.readouterr().out


def test_match_report_is_concise_and_json_keeps_full_settlement(tmp_path):
    game = replace(TEST_GAME, initial_coins=20, max_bid=9, super_bounty=5, daily_pool=(1, 2, 3))
    log = run_scenario(
        {1: [4, 4, 9, 2, 1, 0], 2: [2, 2, 1, 1, 0, 0], 3: [-1, 0, 1, 1, 2, 2]},
        super_strategies={
            "乙": lambda obs: 3 if obs.day == 1 else None,
            "丁": lambda obs: 4 if obs.day == 1 else None,
        },
        game=game,
    )
    standings = make_standings({name: [wins] for name, wins in log.wins.items()})
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
    assert "| 天 | 奖池 | 甲 | 乙 | 丙 | 丁 | 戊 | 己 | 结果 | 撞车价位 |" in report
    assert "4（撞车） | 4（撞车）+超3（落空） | 9（获胜） | 2（退款）+超4（命中）" in report
    assert "| 流局 | 0、1、2 |" in report
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
