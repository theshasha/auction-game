"""在隔离目录验收规则命令的真实配置加载、生成与过期检查。"""

import sys

import pytest
import yaml

import configuration
import rules


@pytest.mark.parametrize("changed_input", ["game.yaml", "players.yaml", "RULES.template.md"])
def test_generate_and_check_isolated_rules(tmp_path, monkeypatch, changed_input):
    game_path = tmp_path / "game.yaml"
    players_path = tmp_path / "players.yaml"
    template_path = tmp_path / "RULES.template.md"
    document_path = tmp_path / "RULES.md"
    game = {
        "initial_coins": 11,
        "daily_salary": 4,
        "daily_pool": [2, 1, 3],
        "max_bid": 9,
        "super_cost": 3,
        "super_bounty": 5,
        "regular_matches": 2,
        "max_playoffs": 4,
    }
    players = [
        {"name": name, "persona_title": "测试", "persona_text": "测试人设",
         "provider": "offline", "model": "unused", "protocol": "chat", "max_tokens": 123}
        for name in ["隔离甲", "隔离乙", "隔离丙"]
    ]
    game_path.write_text(yaml.safe_dump(game), encoding="utf-8")
    players_path.write_text(yaml.safe_dump({"players": players}), encoding="utf-8")
    template_path.write_text(
        "{player_count}人：{player_names}\n"
        "金币：{initial_coins}/{daily_salary}，出价：{max_bid}，超级：{super_cost}/{super_bounty}\n"
        "正赛：{regular_matches}，加赛：{max_playoffs}，天数：{max_days}\n"
        "{pool_description}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rules, "TEMPLATE_PATH", template_path)
    monkeypatch.setattr(rules, "RULES_PATH", document_path)
    monkeypatch.setattr(configuration, "ROOT_DIR", tmp_path)
    # 路径默认值在函数定义时绑定；直接调整默认值，保留真实加载与校验。
    monkeypatch.setattr(configuration.load_game_settings, "__defaults__", (game_path,))
    monkeypatch.setattr(configuration.load_players, "__defaults__", (players_path,))
    monkeypatch.setattr(sys, "argv", ["rules.py"])

    assert rules.main() == 0
    assert document_path.read_text(encoding="utf-8") == (
        rules.GENERATED_NOTE
        + "3人：隔离甲、隔离乙、隔离丙\n"
        "金币：11/4，出价：9，超级：3/5\n"
        "正赛：2，加赛：4，天数：3\n"
        "各日基础奖池依次为：第 1 天 2 个胜场、第 2 天 1 个胜场、第 3 天 3 个胜场；"
        "每局计划共 6 个胜场。\n"
    )
    generated = document_path.read_bytes()
    monkeypatch.setattr(sys, "argv", ["rules.py", "--check"])
    assert rules.main() == 0
    assert document_path.read_bytes() == generated

    path = tmp_path / changed_input
    if changed_input == "game.yaml":
        game["initial_coins"] = 12
        path.write_text(yaml.safe_dump(game), encoding="utf-8")
    elif changed_input == "players.yaml":
        players[0]["name"] = "隔离新玩家"
        path.write_text(yaml.safe_dump({"players": players}), encoding="utf-8")
    else:
        path.write_text("新规则\n{player_names}\n", encoding="utf-8")

    assert rules.main() == 1
    assert document_path.read_bytes() == generated
