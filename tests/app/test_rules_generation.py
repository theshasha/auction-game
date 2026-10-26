"""在隔离目录验收规则命令的真实配置加载、生成与过期检查。"""

import sys
from dataclasses import replace

import pytest
import yaml

from auction_game import configuration
from auction_game.game import rules
from auction_game import runners
from auction_game.game import benchmark
from auction_game.cli import offline_demo
from tests.support.factories import TEST_GAME


@pytest.mark.parametrize("changed_input", [
    "game.yaml", "daily_pool", "super_hit_score", "players.yaml", "models.yaml", "RULES.template.md",
])
def test_generate_and_check_isolated_rules(tmp_path, monkeypatch, changed_input):
    game_path = tmp_path / "game.yaml"
    players_path = tmp_path / "players.yaml"
    models_path = tmp_path / "models.yaml"
    template_path = tmp_path / "RULES.template.md"
    document_path = tmp_path / "RULES.md"
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
    catalog = {p["model"]: {"name": p["model"], "model": "unused", "provider": "offline",
                            "protocol": "chat", "models_dev": {"provider": "offline", "model": "source"}}
               for p in players}
    models_path.write_text(yaml.safe_dump({"models": catalog}), encoding="utf-8")
    template_path.write_text(
        "{player_count}人：{player_names}\n"
        "金币：{initial_coins}/{daily_salary}，出价：{max_bid}，超级：{super_cost}/{super_bounty}\n"
        "命中分：{super_hit_score}\n"
        "正赛：{regular_matches}，加赛：{max_playoffs}，天数：{max_days}\n"
        "{pool_description}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rules, "TEMPLATE_PATH", template_path)
    monkeypatch.setattr(rules, "RULES_PATH", document_path)
    monkeypatch.setattr(rules, "load_game_settings", lambda: configuration.load_game_settings(game_path))
    monkeypatch.setattr(rules, "load_players",
                        lambda **kwargs: configuration.load_players(players_path, models_path, **kwargs))
    monkeypatch.setattr(sys, "argv", ["rules.py"])

    assert rules.main() == 0
    assert document_path.read_text(encoding="utf-8") == (
        rules.GENERATED_NOTE
        + "3人：隔离甲、隔离乙、隔离丙\n"
        "金币：11/4，出价：9，超级：3/5\n"
        "命中分：2\n"
        "正赛：2，加赛：4，天数：3\n"
        "各日奖池依次为：第 1 天 2 分、第 2 天 1 分、第 3 天 3 分。"
        "每局奖池合计 6 分。\n"
    )
    generated = document_path.read_bytes()
    body = rules.render_rules(
        configuration.load_game_settings(game_path), ["隔离甲", "隔离乙", "隔离丙"], include_note=False,
    )
    assert document_path.read_text(encoding="utf-8") == rules.GENERATED_NOTE + body
    assert "<!--" not in body
    monkeypatch.setattr(sys, "argv", ["rules.py", "--check"])
    assert rules.main() == 0
    assert document_path.read_bytes() == generated

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
    elif changed_input == "models.yaml":
        catalog[players[0]["model"]]["name"] = "隔离新玩家"
        path.write_text(yaml.safe_dump({"models": catalog}), encoding="utf-8")
    else:
        path.write_text("新规则\n{player_names}\n", encoding="utf-8")
    assert rules.main() == 1
    assert document_path.read_bytes() == generated


@pytest.mark.parametrize("entry,kwargs,module,boundary", [
    (runners.run_bots, {"seed": 1}, runners, "make_run_dir"),
    (runners.run_ai, {}, runners, "make_model_factory"),
    (runners.run_ai, {"test_model": True}, runners, "make_model_factory"),
    (benchmark.run_benchmark, {"count": 1}, benchmark, "run_tournament"),
    (offline_demo.generate_demo, {}, offline_demo, "make_run_dir"),
    (offline_demo.generate_demo, {"test_model": True}, offline_demo, "make_run_dir"),
])
def test_all_starts_sync_rules_before_logs_or_models(entry, kwargs, module, boundary, tmp_path, monkeypatch):
    """根规则始终按生产参数生成，测试模式的覆盖参数只用于本场。"""
    document = tmp_path / "RULES.md"
    document.write_text("旧规则", encoding="utf-8")
    names = ["配置甲", "配置乙"]
    monkeypatch.setattr(rules, "RULES_PATH", document)
    monkeypatch.setattr(rules, "load_players", lambda **kwargs: [{"name": name} for name in names])
    monkeypatch.setattr(module, "load_game_settings", lambda test_model=False:
                        replace(TEST_GAME, regular_matches=9) if test_model else TEST_GAME)

    class StartStopped(Exception):
        pass

    def stop_before_side_effect(*args, **kwargs):
        assert document.read_text(encoding="utf-8") == rules.render_rules(TEST_GAME, names)
        raise StartStopped

    monkeypatch.setattr(module, boundary, stop_before_side_effect)
    with pytest.raises(StartStopped):
        entry(**kwargs)
