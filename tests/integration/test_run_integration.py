"""真实 Bot 入口贯通比赛、日志、规则生成与离线统计。"""

from auction_game.reporting import analyze, run_output
from auction_game import runners
from auction_game.game.game_rules import GameSettings
from auction_game.game.players import BOT_NAMES
from auction_game.game import rules as rules_module
from auction_game.configuration import load_players


def test_bot_run_publishes_rules_and_replayable_statistics(tmp_path, monkeypatch, capsys):
    game = GameSettings(20, 4, (2, 4, 3), 9, 1, 5, 2, 3)
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(runners, "load_game_settings", lambda: game)
    monkeypatch.setattr(run_output, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(run_output, "LOG_DIR", log_dir)
    monkeypatch.setattr(analyze, "ROOT_DIR", tmp_path)
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
    assert len(matches) == 4
    assert "共进行 4 局（正赛 2 局 + 加赛 2 局）" in run_output_text
    expected_wins = {
        "Alpha": [0, 4, 3, 6],
        "Bravo": [0, 2, 0, 3],
        "Charlie": [0, 3, 0, 0],
        "Delta": [2, 0, 0, 0],
        "Echo": [3, 0, 4, 0],
        "Foxtrot": [4, 0, 2, 0],
    }
    for index, payload in enumerate(matches, 1):
        match = payload["match"]
        assert match["match_index"] == index
        assert match["is_playoff"] == (index > game.regular_matches)
        assert match["game"] == game.to_dict()
        assert match["wins"] == {name: wins[index - 1] for name, wins in expected_wins.items()}
        assert [day["day"] for day in match["days"]] == [1, 2, 3]
        assert [day["pool"] for day in match["days"]] == list(game.daily_pool)
        for day in match["days"]:
            assert [bid["player"] for bid in day["bids"] if not bid["is_super"]] == BOT_NAMES
        standings = {entry["player"]: entry for entry in payload["standings_after_match"]}
        assert set(standings) == set(BOT_NAMES)
        for name, wins in expected_wins.items():
            assert standings[name]["wins_per_match"] == wins[:index]
            assert standings[name]["total_wins"] == sum(wins[:index])
        markdown = (run_dir / "matches" / f"{index:03d}.md").read_text(encoding="utf-8")
        assert f"第 {index} 局（{'加赛' if match['is_playoff'] else '正赛'}）" in markdown
        assert "截至本局的累计排名" in markdown
        assert all(name in markdown for name in BOT_NAMES)
    assert {entry["player"] for entry in matches[-1]["standings_after_match"] if entry["rank"] == 1} == {"Alpha"}

    rules = (run_dir / "RULES.md").read_text(encoding="utf-8")
    assert f"6 名玩家参赛（{'、'.join(BOT_NAMES)}）" in rules
    assert "共 2 局正赛，每局 3 天" in rules
    assert "第 1 天 2 个胜场、第 2 天 4 个胜场、第 3 天 3 个胜场" in rules
    assert "每人发放 20 枚金币" in rules
    assert "每天开始时每人发放 4 枚金币" in rules
    assert "0~9的整数" in rules
    assert "固定消耗 1 枚金币" in rules
    assert "发放 5 金币悬赏" in rules
    assert "最多进行 3 局加赛" in rules

    stats = analyze.collect_player_stats(matches)
    assert stats["match_labels"] == ["1", "2", "加1", "加2"]
    assert stats["win_matrix"] == expected_wins
    rows = {row["name"]: row for row in stats["rows"]}
    for name, wins in expected_wins.items():
        assert rows[name]["total_wins"] == sum(wins)
    # Alpha 每局重置为 20、两次工资共 8；正常扣款/超级成本/悬赏独立手算：
    # 局1: 28-5-1-4-1+5=22；局2: 28-9-5=14；
    # 局3: 28-9-1=18；局4: 28-8-8-1=11（末日正常 7 未胜未撞，不扣）。
    expected_coins = [22, 14, 18, 11]
    assert [analyze.recompute_remaining_coins(payload["match"])["Alpha"] for payload in matches] == expected_coins
    assert rows["Alpha"]["end_coins"] == expected_coins

    analyze.analyze(run_dir)
    report = capsys.readouterr().out
    assert f"场次目录：{run_dir.name}" in report
    assert f"玩家：{'、'.join(BOT_NAMES)}" in report
    assert "局数：4（正赛 2 + 加赛 2）" in report
    assert "总拍卖天数：12" in report
    assert "矩阵列 = 第 1/2/加1/加2 局" in report
    assert "Alpha：0 4 3 6" in report
    assert "Alpha：胜13" in report
    assert "局末余额22/14/18/11" in report
