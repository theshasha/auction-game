"""共享配置夹具；普通辅助见 tests/support。"""

import json
import pytest
import yaml

from auction_game.cli.sync_models import build_snapshot
from tests.support.factories import CHANNELS
from auction_game.game import rules


@pytest.fixture(autouse=True)
def isolated_root_rules(tmp_path, monkeypatch):
    """生产启动同步仍真实执行，测试写入各自临时根规则。"""
    monkeypatch.setattr(rules, "RULES_PATH", tmp_path / "root-RULES.md")

@pytest.fixture
def config_files(tmp_path, monkeypatch):
    """合成目录覆盖不同协议/预算与无角色名的测试替身，不绑定生产名单。"""
    config_path = tmp_path / "config.yaml"
    players_path = tmp_path / "players.yaml"
    models_path = tmp_path / "models.yaml"
    monkeypatch.setattr("auction_game.configuration.CAPABILITIES_PATH", tmp_path / "model_capabilities.json")
    monkeypatch.setattr("auction_game.cli.sync_models.CAPABILITIES_PATH", tmp_path / "model_capabilities.json")
    config_path.write_text(yaml.safe_dump({"test_model": "substitute", "llm": {"channels": CHANNELS}}), encoding="utf-8")
    models_path.write_text(yaml.safe_dump({"models": {
        "first": dict(name="甲", provider="hub", model="test-model-a", protocol="chat",
                      models_dev=dict(provider="official", model="source-a")),
        "second": dict(name="乙", provider="anthropic", model="test-model-b", protocol="anthropic",
                       models_dev=dict(provider="official", model="source-b")),
        "substitute": dict(provider="hub", model="test-model-c", protocol="responses",
                            models_dev=dict(provider="official", model="source-c")),
    }}), encoding="utf-8")
    catalog = yaml.safe_load(models_path.read_text(encoding="utf-8"))["models"]
    data = {"official": {"models": {
        f"source-{letter}": {"id": f"source-{letter}", "limit": {"context": 1000, "output": budget},
                              "reasoning": letter == "c", "tool_call": True,
                              "reasoning_options": [{"type": "effort", "values": ["low", "high"]}] if letter == "c" else []}
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
