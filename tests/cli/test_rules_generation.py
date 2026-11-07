"""在隔离目录验收规则命令的真实配置加载、生成与过期检查。"""

import sys
from dataclasses import replace

import pytest
import yaml

from auction_game import configuration
from auction_game.game import rules
from auction_game.cli import rules as rules_command
from auction_game import runners
from auction_game.cli import offline_demo
from auction_game.game.lineup import PlayerProfile
from tests.support.factories import TEST_GAME, make_lineup, make_team_lineup


def configured_players(names):
    """替换 load_players 的返回值：每个座位的人设和目录键。"""
    return [(PlayerProfile(name, "人设", "正文", 0), "unused") for name in names]


PARAMETER_LINES = (
    "金币：{initial_coins}/{daily_salary}，出价：{max_bid}，超级：{super_cost}/{super_bounty}\n"
    "命中分：{super_hit_score}\n"
    "正赛：{regular_matches}，加赛：{max_playoffs}，天数：{max_days}\n"
    "{pool_description}\n"
)
RENDERED_PARAMETERS = (
    "金币：11/4，出价：9，超级：3/5\n"
    "命中分：2\n"
    "正赛：2，加赛：4，天数：3\n"
    "各日奖池依次为：第 1 天 2 分、第 2 天 1 分、第 3 天 3 分。"
    "每局奖池合计 6 分。\n"
)


@pytest.mark.parametrize("changed_input,stale", [
    ("game.yaml", "RULES.md、RULES_TEAM.md"),
    ("daily_pool", "RULES.md、RULES_TEAM.md"),
    ("super_hit_score", "RULES.md、RULES_TEAM.md"),
    ("models.yaml", "RULES.md、RULES_TEAM.md"),
    ("players.yaml", "RULES.md"),
    ("RULES.template.md", "RULES.md"),
    ("teams.yaml", "RULES_TEAM.md"),
    ("RULES_TEAM.template.md", "RULES_TEAM.md"),
])
def test_generate_and_check_isolated_rules(tmp_path, monkeypatch, capsys, changed_input, stale):
    game_path = tmp_path / "game.yaml"
    players_path = tmp_path / "players.yaml"
    teams_path = tmp_path / "teams.yaml"
    models_path = tmp_path / "models.yaml"
    template_path = tmp_path / "RULES.template.md"
    team_template_path = tmp_path / "RULES_TEAM.template.md"
    document_path = tmp_path / "RULES.md"
    team_document_path = tmp_path / "RULES_TEAM.md"
    game = {
        "initial_coins": 11,
        "daily_salary": 4,
        "daily_pool": [2, 1, 3],
        "max_bid": 9,
        "super_cost": 3,
        "super_bounty": 5,
        "super_hit_score": 2,
        "regular_matches": 2,
        "max_playoffs": 4,
    }
    players = [
        {"persona_title": "测试", "persona_text": "测试人设", "model": name}
        for name in ["隔离甲", "隔离乙", "隔离丙"]
    ]
    game_path.write_text(yaml.safe_dump(game), encoding="utf-8")
    players_path.write_text(yaml.safe_dump({"players": players}), encoding="utf-8")
    teams = [{"name": "甲队", "players": players},
             {"name": "乙队", "players": [{**player, "name": name} for player, name in zip(
                 players, ["隔离丁", "隔离戊", "隔离己"])]}]
    teams_path.write_text(yaml.safe_dump({"teams": teams}), encoding="utf-8")
    catalog = {p["model"]: {"name": p["model"], "model": "unused", "provider": "offline",
                            "protocol": "chat", "models_dev": {"provider": "offline", "model": "source"}}
               for p in players}
    models_path.write_text(yaml.safe_dump({"models": catalog}), encoding="utf-8")
    template_path.write_text("{player_count}人：{player_names}\n" + PARAMETER_LINES, encoding="utf-8")
    team_template_path.write_text("{player_count}人分两队：{team_lineup}，每队 {team_size} 人\n" + PARAMETER_LINES,
                                  encoding="utf-8")
    monkeypatch.setattr(rules, "TEMPLATE_PATH", template_path)
    monkeypatch.setattr(rules, "TEAM_TEMPLATE_PATH", team_template_path)
    monkeypatch.setattr(rules_command, "RULES_PATH", document_path)
    monkeypatch.setattr(rules_command, "TEAM_RULES_PATH", team_document_path)
    monkeypatch.setattr(rules_command, "load_game_settings", lambda: configuration.load_game_settings(game_path))
    monkeypatch.setattr(rules_command, "load_players",
                        lambda: configuration.load_players(players_path, configuration.load_model_entries(models_path)))
    monkeypatch.setattr(rules_command, "load_teams",
                        lambda: configuration.load_teams(teams_path, configuration.load_model_entries(models_path)))
    monkeypatch.setattr(sys, "argv", ["rules.py"])

    assert rules_command.main() == 0
    assert document_path.read_text(encoding="utf-8") == (
        rules.GENERATED_NOTE + "3人：隔离甲、隔离乙、隔离丙\n" + RENDERED_PARAMETERS)
    assert team_document_path.read_text(encoding="utf-8") == (
        rules.TEAM_GENERATED_NOTE
        + "6人分两队：甲队有 隔离甲、隔离乙、隔离丙，乙队有 隔离丁、隔离戊、隔离己，每队 3 人\n" + RENDERED_PARAMETERS)
    generated = document_path.read_bytes(), team_document_path.read_bytes()
    monkeypatch.setattr(sys, "argv", ["rules.py", "--check"])
    assert rules_command.main() == 0
    assert (document_path.read_bytes(), team_document_path.read_bytes()) == generated
    capsys.readouterr()

    path = tmp_path / changed_input
    if changed_input == "game.yaml":
        game["initial_coins"] = 12
        path.write_text(yaml.safe_dump(game), encoding="utf-8")
    elif changed_input == "daily_pool":
        game["daily_pool"][0] += 1
        game_path.write_text(yaml.safe_dump(game), encoding="utf-8")
    elif changed_input == "super_hit_score":
        game[changed_input] += 1
        game_path.write_text(yaml.safe_dump(game), encoding="utf-8")
    elif changed_input == "players.yaml":
        players.reverse()
        path.write_text(yaml.safe_dump({"players": players}), encoding="utf-8")
    elif changed_input == "teams.yaml":
        teams.reverse()
        path.write_text(yaml.safe_dump({"teams": teams}), encoding="utf-8")
    elif changed_input == "models.yaml":
        catalog[players[0]["model"]]["name"] = "隔离新玩家"
        path.write_text(yaml.safe_dump({"models": catalog}), encoding="utf-8")
    else:
        path.write_text("新规则\n{player_names}\n", encoding="utf-8")
    assert rules_command.main() == 1
    assert capsys.readouterr().err.startswith(f"{stale} 已过期")
    assert (document_path.read_bytes(), team_document_path.read_bytes()) == generated


@pytest.mark.parametrize("entry,kwargs,module,boundary", [
    (runners.run_ai, {}, runners, "make_model_factory"),
    (runners.run_ai, {"test_model": True}, runners, "make_model_factory"),
    (offline_demo.generate_demo, {}, offline_demo, "make_run_dir"),
    (offline_demo.generate_demo, {"test_model": True}, offline_demo, "make_run_dir"),
    (runners.run_ai, {"team": True}, runners, "make_model_factory"),
    (runners.run_ai, {"team": True, "test_model": True}, runners, "make_model_factory"),
])
def test_all_starts_sync_rules_before_logs_or_models(entry, kwargs, module, boundary, tmp_path, monkeypatch):
    """根规则始终按生产参数生成，测试模式的覆盖参数只用于本场；团队赛同步的是 RULES_TEAM.md。"""
    team = kwargs.get("team", False)
    document = tmp_path / "RULES.md"
    document.write_text("旧规则", encoding="utf-8")
    lineup = make_team_lineup() if team else make_lineup(["配置甲", "配置乙"])
    monkeypatch.setattr(rules_command, "TEAM_RULES_PATH" if team else "RULES_PATH", document)
    monkeypatch.setattr(rules_command, "load_teams" if team else "load_players",
                        lambda: [(profile, "unused") for profile in lineup.profiles])
    monkeypatch.setattr(module, "load_game_settings", lambda test_model=False:
                        replace(TEST_GAME, regular_matches=9) if test_model else TEST_GAME)

    class StartStopped(Exception):
        pass

    def stop_before_side_effect(*args, **kwargs):
        assert document.read_text(encoding="utf-8") == rules.render_rules(TEST_GAME, lineup)
        raise StartStopped

    monkeypatch.setattr(module, boundary, stop_before_side_effect)
    with pytest.raises(StartStopped):
        entry(**kwargs)
