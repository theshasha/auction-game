"""共享配置夹具；普通辅助见 tests/support。"""

import json
import pytest
import yaml

from auction_game.cli.sync_models import build_snapshot
from auction_game.configuration import load_model_entries
from tests.support.factories import CHANNELS
from auction_game.cli import rules


@pytest.fixture(autouse=True)
def isolated_root_rules(tmp_path, monkeypatch):
    """生产启动同步仍真实执行，测试写入各自临时根规则。"""
    monkeypatch.setattr(rules, "RULES_PATH", tmp_path / "root-RULES.md")
    monkeypatch.setattr(rules, "TEAM_RULES_PATH", tmp_path / "root-RULES_TEAM.md")

@pytest.fixture
def config_files(tmp_path, monkeypatch):
    """合成目录覆盖不同协议/预算与无角色名的测试替身，不绑定生产名单。"""
    config_path = tmp_path / "config.yaml"
    players_path = tmp_path / "players.yaml"
    models_path = tmp_path / "models.yaml"
    monkeypatch.setattr("auction_game.configuration.CAPABILITIES_PATH", tmp_path / "model_capabilities.json")
    monkeypatch.setattr("auction_game.cli.sync_models.CAPABILITIES_PATH", tmp_path / "model_capabilities.json")
    writing_style_path = tmp_path / "writing_style.md"
    writing_style_path.write_text("【写作要求】\n合成写作要求。\n", encoding="utf-8")
    monkeypatch.setattr("auction_game.configuration.WRITING_STYLE_PATH", writing_style_path)
    config_path.write_text(yaml.safe_dump({"test_model": "substitute", "llm": {"channels": CHANNELS}}), encoding="utf-8")
    models_path.write_text(yaml.safe_dump({"models": {
        "first": dict(name="甲", provider="hub", model="test-model-a", protocol="chat",
                      models_dev=dict(provider="official", model="source-a")),
        "second": dict(name="乙", provider="anthropic", model="test-model-b", protocol="anthropic",
                       models_dev=dict(provider="official", model="source-b")),
        "substitute": dict(provider="hub", model="test-model-c", protocol="responses",
                            models_dev=dict(provider="official", model="source-c")),
    }}), encoding="utf-8")
    catalog = load_model_entries(models_path)
    data = {"official": {"models": {
        f"source-{letter}": {"id": f"source-{letter}", "limit": {"context": 1000, "output": budget},
                              "reasoning": letter == "c",
                              "reasoning_options": [{"type": "effort", "values": ["low", "medium", "high"]}] if letter == "c" else []}
        for letter, budget in zip("abc", (123, 456, 789))
    }}}
    (tmp_path / "model_capabilities.json").write_text(
        json.dumps(build_snapshot(catalog, data, "synthetic")), encoding="utf-8")
    players = [
        dict(persona_title="姐姐", persona_text="沉稳温柔", model="first", predicted_win_rate=23),
        dict(persona_title="妹妹", persona_text="活泼好奇", model="second"),
    ]
    players_path.write_text(yaml.safe_dump({"players": players}), encoding="utf-8")
    return config_path, players_path, models_path


@pytest.fixture
def teams_file(config_files):
    """和 config_files 同一目录的合成团队赛阵容：红队前两人用目录里的角色名，其余写 name。"""
    path = config_files[0].parent / "teams.yaml"

    def player(model, name=None):
        return {"model": model, "persona_title": "无", "persona_text": "无", **({"name": name} if name else {})}

    path.write_text(yaml.safe_dump({"teams": [
        {"name": "红队", "players": [player("first"), player("second"), player("substitute", "丙")]},
        {"name": "蓝队", "players": [player("first", "丁"), player("second", "戊"), player("substitute", "己")]},
    ]}, allow_unicode=True), encoding="utf-8")
    return path
