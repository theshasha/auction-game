"""比赛参数和共享出价边界；不调用模型服务。"""

from dataclasses import FrozenInstanceError
import json

import pytest
import yaml

from auction_game.configuration import ConfigurationError, load_game_settings, load_players
from auction_game.game.game_rules import GameSettings, validate_submission


@pytest.fixture
def settings_data():
    return dict(
        initial_coins=11, daily_salary=4, daily_pool=[2, 1, 3], max_bid=9,
        super_cost=3, super_bounty=5, regular_matches=2, max_playoffs=0,
    )


def test_immutable_settings_and_json_snapshot(settings_data):
    game = GameSettings.from_dict(settings_data)
    assert game.daily_pool == (2, 1, 3)
    assert game.max_days == 3
    assert game.wins_per_match == 6
    assert json.loads(json.dumps(game.to_dict())) == settings_data
    assert GameSettings.from_dict(game.to_dict()) == game
    settings_data["daily_pool"][0] = 99
    snapshot = game.to_dict()
    snapshot["daily_pool"][0] = 88
    assert game.daily_pool == (2, 1, 3)
    with pytest.raises(FrozenInstanceError):
        game.initial_coins = 0


@pytest.mark.parametrize("field", [
    "initial_coins", "daily_salary", "max_bid", "super_cost", "super_bounty",
    "regular_matches", "max_playoffs",
])
@pytest.mark.parametrize("value", [True, 1.5, -1])
def test_constructor_rejects_invalid_integer_fields(settings_data, field, value):
    settings_data["daily_pool"] = tuple(settings_data["daily_pool"])
    settings_data[field] = value
    with pytest.raises(ValueError, match=field):
        GameSettings(**settings_data)


def test_zero_boundaries(settings_data):
    for field in settings_data:
        if field not in ("daily_pool", "regular_matches"):
            settings_data[field] = 0
    game = GameSettings.from_dict(settings_data)
    assert game.max_bid == game.super_cost == game.max_playoffs == 0
    assert validate_submission(0, None, 0, game) is None
    settings_data["regular_matches"] = 0
    with pytest.raises(ValueError, match="regular_matches"):
        GameSettings.from_dict(settings_data)


@pytest.mark.parametrize("pool,field", [
    ([], "daily_pool"), ("123", "daily_pool"), ([0], r"daily_pool\[0\]"),
    ([-1], r"daily_pool\[0\]"), ([True], r"daily_pool\[0\]"),
    ([1.5], r"daily_pool\[0\]"),
])
def test_invalid_pools(settings_data, pool, field):
    settings_data["daily_pool"] = pool
    with pytest.raises(ValueError, match=field):
        GameSettings.from_dict(settings_data)


def test_constructor_requires_tuple(settings_data):
    with pytest.raises(ValueError, match="daily_pool"):
        GameSettings(**settings_data)


def test_required_and_unknown_fields(settings_data):
    with pytest.raises(ValueError, match="拼写"):
        GameSettings.from_dict({**settings_data, "max_bdi": 9})
    for field in settings_data:
        with pytest.raises(ValueError, match=field):
            GameSettings.from_dict({k: v for k, v in settings_data.items() if k != field})
    with pytest.raises(ValueError, match="根节点"):
        GameSettings.from_dict([])


@pytest.mark.parametrize("bid,super_bid,coins,reason", [
    (True, None, 11, "bid 不是整数（True）"),
    (1.5, None, 11, "bid 不是整数（1.5）"),
    (-1, None, 11, "出价 -1 超出 0~9 的范围"),
    (10, None, 11, "出价 10 超出 0~9 的范围"),
    (9, None, 8, "出价 9 超过当前余额 8"),
    (1, False, 11, "super_bid 不是整数（False）"),
    (1, "2", 11, "super_bid 不是整数（'2'）"),
    (1, -1, 11, "超级出价 -1 超出 0~9 的范围"),
    (1, 10, 11, "超级出价 10 超出 0~9 的范围"),
    (1, 1, 11, "超级出价 1 不能等于你的正常出价（同值属非法提交）"),
    (6, 9, 8, "正常出价 6 加超级出价成本 3 金币，合计超过当前余额 8"),
    (0, None, 0, None),
    (9, None, 9, None),
    (6, 9, 9, None),
    (0, 9, 3, None),
    (1, 0, 4, None),
])
def test_submission_boundaries(settings_data, bid, super_bid, coins, reason):
    assert validate_submission(
        bid, super_bid, coins, GameSettings.from_dict(settings_data)
    ) == reason


def test_load_game_paths_and_errors(settings_data, tmp_path, monkeypatch):
    expected = load_game_settings()
    monkeypatch.chdir(tmp_path)
    assert load_game_settings("configs/game.yaml") == expected
    path = tmp_path / "custom-game.yaml"
    path.write_text(yaml.safe_dump(settings_data), encoding="utf-8")
    assert load_game_settings(path) == GameSettings.from_dict(settings_data)
    settings_data["daily_salary"] = True
    path.write_text(yaml.safe_dump(settings_data), encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc:
        load_game_settings(path)
    assert str(path) in str(exc.value)
    assert "daily_salary" in str(exc.value)
    with pytest.raises(ConfigurationError, match="读取失败"):
        load_game_settings(tmp_path / "missing.yaml")


def test_load_players_is_independent_and_preserves_contract(tmp_path, monkeypatch):
    monkeypatch.setattr("auction_game.configuration.CAPABILITIES_PATH", tmp_path / "missing.json")
    path = tmp_path / "players.yaml"
    player = dict(persona_title="沉稳", persona_text="谨慎出价", model="identity")
    models_path = tmp_path / "models.yaml"
    model = dict(name="甲", provider="test", model="api-model", protocol="chat",
                 models_dev={"provider": "official", "model": "source"})
    models_path.write_text(yaml.safe_dump({"models": {"identity": model}}), encoding="utf-8")
    path.write_text(yaml.safe_dump({"players": [player]}), encoding="utf-8")
    from auction_game.model_capabilities import effective_capabilities
    model.update(effective_capabilities("identity", model, {}))
    assert load_players(path, models_path) == [{**player, **model, "model_key": "identity",
                                               "identity_model": {"key": "identity", **model},
                                               "predicted_win_rate": 0}]
