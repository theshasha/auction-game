"""真实 Bot 入口贯通比赛、日志与离线统计；统计结果与引擎当场结算交叉核对。"""

from auction_game.reporting import analyze, run_output
from auction_game import runners
from auction_game.game.game_rules import GameSettings
from auction_game.game.players import BOT_NAMES


def test_bot_run_statistics_match_engine_settlement(tmp_path, monkeypatch, capsys):
    game = GameSettings(
        initial_coins=20, daily_salary=4, daily_pool=(2, 4, 3), max_bid=9,
        super_cost=1, super_bounty=5, super_hit_score=2,
        regular_matches=2, max_playoffs=3,
    )
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(runners, "load_game_settings", lambda: game)
    monkeypatch.setattr(run_output, "LOG_DIR", log_dir)
    settled = {}  # 局号 -> 当天结算后的 (金币, 拍卖分, 总分)；最后一天即局末

    def record_day(log, standings, coins):
        settled[log.match_index] = (dict(coins), dict(log.auction_scores), dict(log.scores))
        run_output.print_day_result(log, standings, coins)

    monkeypatch.setattr(runners, "print_day_result", record_day)
    assert runners.run_bots(seed=4) == 0
    capsys.readouterr()
    run_dir = next(log_dir.glob("*_bot"))
    matches = analyze.load_match_logs(run_dir)
    assert [payload["match"]["match_index"] for payload in matches] == sorted(settled)

    stats = analyze.collect_player_stats(matches)
    indices = sorted(settled)
    for name in BOT_NAMES:
        coins = [settled[index][0][name] for index in indices]
        auction_scores = [settled[index][1][name] for index in indices]
        scores = [settled[index][2][name] for index in indices]
        row = next(row for row in stats["rows"] if row["name"] == name)
        assert row["end_coins"] == coins
        assert stats["auction_score_matrix"][name] == auction_scores
        assert stats["score_matrix"][name] == scores
        assert row["total_score"] == sum(scores)
        final = next(entry for entry in matches[-1]["standings_after_match"] if entry["player"] == name)
        assert final["total_score"] == sum(scores)
        assert final["total_auction_score"] == sum(auction_scores)

    analyze.analyze(run_dir)
    report = capsys.readouterr().out
    assert f"场次目录：{run_dir.name}" in report
    assert f"玩家：{'、'.join(BOT_NAMES)}" in report
    assert f"总拍卖天数：{len(matches) * game.max_days}" in report
