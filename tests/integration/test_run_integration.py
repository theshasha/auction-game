"""真实 Bot 入口贯通比赛、日志、规则生成与离线统计。"""

from auction_game.reporting import analyze, run_output
from auction_game import runners
from auction_game.game.game_rules import GameSettings
from auction_game.game.players import BOT_NAMES
from auction_game.game import rules as rules_module
from auction_game.configuration import load_players


def test_bot_run_publishes_rules_and_replayable_statistics(tmp_path, monkeypatch, capsys):
    game = GameSettings(
        initial_coins=20, daily_salary=4, daily_pool=(2, 4, 3), max_bid=9,
        super_cost=1, super_bounty=5, super_hit_score=2,
        regular_matches=2, max_playoffs=3,
    )
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(runners, "load_game_settings", lambda: game)
    monkeypatch.setattr(run_output, "LOG_DIR", log_dir)
    monkeypatch.setattr(analyze, "LOG_DIR", log_dir)

    runners.run_bots(seed=4)
    root_rules = rules_module.RULES_PATH.read_text(encoding="utf-8")
    assert all(player["name"] in root_rules for player in load_players())
    assert "（" + "、".join(BOT_NAMES) + "）" not in root_rules
    run_output_text = capsys.readouterr().out
    run_dirs = [path for path in log_dir.glob("*_bot") if path.is_dir()]
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    matches = analyze.load_match_logs(run_dir)
    assert len(matches) == 2
    assert "共进行 2 局（正赛 2 局 + 加赛 0 局）" in run_output_text
    expected_auction_scores = {
        "Alpha": [0, 4],
        "Bravo": [0, 2],
        "Charlie": [0, 3],
        "Delta": [2, 0],
        "Echo": [3, 0],
        "Foxtrot": [4, 0],
    }
    expected_scores = {
        "Alpha": [2, 4],
        "Bravo": [0, 2],
        "Charlie": [2, 3],
        "Delta": [4, 0],
        "Echo": [3, 0],
        "Foxtrot": [4, 0],
    }
    for index, payload in enumerate(matches, 1):
        match = payload["match"]
        assert match["match_index"] == index
        assert match["is_playoff"] == (index > game.regular_matches)
        assert match["game"] == game.to_dict()
        assert match["auction_scores"] == {name: scores[index - 1] for name, scores in expected_auction_scores.items()}
        assert match["scores"] == {name: scores[index - 1] for name, scores in expected_scores.items()}
        assert [day["day"] for day in match["days"]] == [1, 2, 3]
        assert [day["pool"] for day in match["days"]] == list(game.daily_pool)
        assert [day["auction_score"] for day in match["days"]] == [2, 4, 3]
        for day in match["days"]:
            assert [bid["player"] for bid in day["bids"] if not bid["is_super"]] == BOT_NAMES
        standings = {entry["player"]: entry for entry in payload["standings_after_match"]}
        assert set(standings) == set(BOT_NAMES)
        for name, scores in expected_auction_scores.items():
            assert standings[name]["auction_scores_per_match"] == scores[:index]
            assert standings[name]["total_auction_score"] == sum(scores[:index])
            assert standings[name]["scores_per_match"] == expected_scores[name][:index]
            assert standings[name]["total_score"] == sum(expected_scores[name][:index])
        markdown = (run_dir / "matches" / f"{index:03d}.md").read_text(encoding="utf-8")
        assert f"第 {index} 局（{'加赛' if match['is_playoff'] else '正赛'}）" in markdown
        assert "截至本局的累计排名" in markdown
        assert all(name in markdown for name in BOT_NAMES)
    assert {entry["player"] for entry in matches[-1]["standings_after_match"] if entry["rank"] == 1} == {"Alpha"}
    # 两人拍卖分同为4；Alpha 首局命中的2分令总分唯一领先，正赛即结束。
    assert sum(expected_auction_scores["Alpha"]) == sum(expected_auction_scores["Foxtrot"]) == 4
    assert sum(expected_scores["Alpha"]) == 6
    assert sum(expected_scores["Foxtrot"]) == 4

    rules = (run_dir / "RULES.md").read_text(encoding="utf-8")
    assert f"6 名玩家参赛（{'、'.join(BOT_NAMES)}）" in rules
    assert "共 2 局正赛，每局 3 天" in rules
    assert "第 1 天 2 分、第 2 天 4 分、第 3 天 3 分" in rules
    assert "拍卖奖池直接计分" in rules
    assert "并获 2 分" in rules
    assert "每人发放 20 枚金币" in rules
    assert "每天开始时每人发放 4 枚金币" in rules
    assert "0~9 的整数" in rules
    assert "固定花费 1 金币" in rules
    assert "发放 5 金币悬赏" in rules
    assert "最多加赛 3 局" in rules

    stats = analyze.collect_player_stats(matches)
    assert stats["match_labels"] == ["正赛第 1 局", "正赛第 2 局"]
    assert stats["score_matrix"] == expected_scores
    assert stats["auction_score_matrix"] == expected_auction_scores
    rows = {row["name"]: row for row in stats["rows"]}
    for name, scores in expected_auction_scores.items():
        assert rows[name]["auction_score"] == sum(scores)
        assert rows[name]["total_score"] == sum(expected_scores[name])
        assert rows[name]["super_score"] == sum(expected_scores[name]) - sum(scores)
    assert (rows["Alpha"]["solo_top_matches"], rows["Alpha"]["tied_top_matches"]) == (1, 0)
    assert (rows["Delta"]["solo_top_matches"], rows["Delta"]["tied_top_matches"]) == (0, 1)
    assert (rows["Foxtrot"]["solo_top_matches"], rows["Foxtrot"]["tied_top_matches"]) == (0, 1)
    # Alpha 每局重置为 20、两次工资共 8；正常扣款/超级成本/悬赏独立手算：
    # 局1: 28-5-1-4-1+5=22；局2: 28-9-5=14。
    expected_coins = [22, 14]
    assert [analyze.recompute_remaining_coins(payload["match"])["Alpha"] for payload in matches] == expected_coins
    assert rows["Alpha"]["end_coins"] == expected_coins

    analyze.analyze(run_dir)
    report = capsys.readouterr().out
    assert f"场次目录：{run_dir.name}" in report
    assert f"玩家：{'、'.join(BOT_NAMES)}" in report
    assert "局数：2（正赛 2 + 加赛 0）" in report
    assert "总拍卖天数：6" in report
    assert "各局顺序：正赛第 1 局 / 正赛第 2 局" in report
    assert "\nAlpha\n  总分：6分（拍卖4分，超级命中2分）\n  各局总分：2 / 4\n  各局拍卖分：0 / 4\n" in report
    assert "各局结束余额：22 / 14 金币" in report
