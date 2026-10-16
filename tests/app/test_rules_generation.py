"""在隔离目录验收规则命令的真实配置加载、生成与过期检查。"""

import sys

import pytest
import yaml

from auction_game import configuration
from auction_game.game import rules
from auction_game import runners
from auction_game.game import benchmark


@pytest.mark.parametrize("changed_input", ["game.yaml", "players.yaml", "models.yaml", "RULES.template.md"])
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
        "正赛：{regular_matches}，加赛：{max_playoffs}，天数：{max_days}\n"
        "{pool_description}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rules, "TEMPLATE_PATH", template_path)
    monkeypatch.setattr(rules, "RULES_PATH", document_path)
    monkeypatch.setattr(configuration, "ROOT_DIR", tmp_path)
    # 路径默认值在函数定义时绑定；直接调整默认值，保留真实加载与校验。
    monkeypatch.setattr(configuration.load_game_settings, "__defaults__", (game_path,))
    monkeypatch.setattr(configuration.load_players, "__defaults__", (players_path, models_path))
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
        players.reverse()
        path.write_text(yaml.safe_dump({"players": players}), encoding="utf-8")
    elif changed_input == "models.yaml":
        catalog[players[0]["model"]]["name"] = "隔离新玩家"
        path.write_text(yaml.safe_dump({"models": catalog}), encoding="utf-8")
    else:
        path.write_text("新规则\n{player_names}\n", encoding="utf-8")
    assert rules.main() == 1
    assert document_path.read_bytes() == generated


@pytest.mark.parametrize("entry,args", [(runners.run_bots, (1,)), (runners.run_ai, ()),
                                         (benchmark.run_benchmark, (1,))])
def test_sync_failure_stops_all_public_starts_before_logs_or_models(entry, args, tmp_path, monkeypatch):
    template = tmp_path / "bad-template.md"
    template.write_text("{unknown}", encoding="utf-8")
    monkeypatch.setattr(rules, "TEMPLATE_PATH", template)
    monkeypatch.setattr(runners, "make_run_dir", lambda *args: pytest.fail("同步失败后创建了日志"))
    monkeypatch.setattr(runners, "make_model_factory", lambda **kwargs: pytest.fail("同步失败后请求了密钥"))
    assert entry(*args) == 1


def test_sync_atomic_failure_preserves_old_document_and_unchanged_mtime(tmp_path, monkeypatch):
    document = rules.RULES_PATH
    assert rules.sync_root_rules() is True
    content, mtime = document.read_bytes(), document.stat().st_mtime_ns
    assert rules.sync_root_rules() is False
    assert document.stat().st_mtime_ns == mtime
    template = tmp_path / "changed-template.md"
    template.write_text("changed {player_names}", encoding="utf-8")
    monkeypatch.setattr(rules, "TEMPLATE_PATH", template)
    monkeypatch.setattr(rules.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk failure")))
    with pytest.raises(configuration.ConfigurationError, match="disk failure"):
        rules.sync_root_rules()
    assert document.read_bytes() == content
