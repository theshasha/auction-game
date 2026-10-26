"""比赛参数和共享出价边界；不调用模型服务。"""

from dataclasses import FrozenInstanceError
import json

import pytest
import yaml

from auction_game.configuration import ConfigurationError, load_game_settings
from auction_game.game.game_rules import GameSettings, validate_submission


@pytest.fixture
def settings_data():
    return dict(
        initial_coins=11, daily_salary=4, daily_pool=[2, 1, 3], max_bid=9,
        super_cost=3, super_bounty=5, super_hit_score=2,
        regular_matches=2, max_playoffs=0,
    )


def test_immutable_settings_and_json_snapshot(settings_data):
    game = GameSettings.from_dict(settings_data)
    assert game.daily_pool == (2, 1, 3)
    assert game.max_days == 3
    assert game.auction_pool_per_match == 6
    assert game.super_hit_score == 2
    assert json.loads(json.dumps(game.to_dict())) == settings_data
    assert GameSettings.from_dict(game.to_dict()) == game
    settings_data["daily_pool"][0] = 99
    snapshot = game.to_dict()
    snapshot["daily_pool"][0] = 88
    assert game.daily_pool == (2, 1, 3)
    with pytest.raises(FrozenInstanceError):
        game.initial_coins = 0


@pytest.mark.parametrize("field,value,match", [
    ("max_bid", -1, "max_bid"), ("daily_pool", [1, 0], r"daily_pool\[1\]"), ("daily_pool", {3: 1}, "daily_pool"),
])
def test_invalid_values(settings_data, field, value, match):
    settings_data[field] = value
    with pytest.raises(ValueError, match=match):
        GameSettings.from_dict(settings_data)


def test_zero_boundaries(settings_data):
    for field in settings_data:
        if field not in ("daily_pool", "regular_matches"):
            settings_data[field] = 0
    game = GameSettings.from_dict(settings_data)
    assert game.max_bid == game.super_cost == game.max_playoffs == 0
    assert game.super_hit_score == 0
    assert validate_submission(0, None, 0, game) is None
    settings_data["regular_matches"] = 0
    with pytest.raises(ValueError, match="regular_matches"):
        GameSettings.from_dict(settings_data)


@pytest.mark.parametrize("change,field", [
    (lambda data: data.update(max_bdi=9), "max_bdi"),
    (lambda data: data.pop("max_bid"), "max_bid"),
], ids=["unknown", "missing"])
def test_unknown_and_missing_fields_name_the_field(settings_data, tmp_path, change, field):
    change(settings_data)
    path = tmp_path / "game.yaml"
    path.write_text(yaml.safe_dump(settings_data), encoding="utf-8")
    with pytest.raises(ConfigurationError, match=field) as exc:
        load_game_settings(path)
    assert str(path) in str(exc.value)


def test_test_model_overrides_apply_only_in_test_mode(settings_data, tmp_path):
    path = tmp_path / "game.yaml"
    path.write_text(yaml.safe_dump({**settings_data, "test_model_overrides": {
        "daily_pool": [5], "regular_matches": 1}}), encoding="utf-8")
    assert load_game_settings(path) == GameSettings.from_dict(settings_data)
    assert load_game_settings(path, test_model=True) == GameSettings.from_dict(
        {**settings_data, "daily_pool": [5], "regular_matches": 1})


@pytest.mark.parametrize("overrides,field", [
    ([1], "test_model_overrides"),
    ({"regular_match": 1}, "regular_match"),
    ({"regular_matches": 0}, "regular_matches"),
], ids=["not-mapping", "unknown", "invalid"])
def test_invalid_test_model_overrides_name_the_field(settings_data, tmp_path, overrides, field):
    path = tmp_path / "game.yaml"
    path.write_text(yaml.safe_dump({**settings_data, "test_model_overrides": overrides}), encoding="utf-8")
    with pytest.raises(ConfigurationError, match=field) as exc:
        load_game_settings(path, test_model=True)
    assert str(path) in str(exc.value)


@pytest.mark.parametrize("bid,super_bid,coins,reason", [
    (True, None, 11, "bid 必须是不加引号的整数，收到 True"),
    (1.5, None, 11, "bid 必须是不加引号的整数，收到 1.5"),
    (-1, None, 11, "出价 -1 超出 0~9 的范围"),
    (10, None, 11, "出价 10 超出 0~9 的范围"),
    (9, None, 8, "出价 9 超过当前余额 8"),
    (1, False, 11, "super_bid 必须是不加引号的整数，不使用超级出价时填 null，收到 False"),
    (1, "2", 11, "super_bid 必须是不加引号的整数，不使用超级出价时填 null，收到 '2'"),
    (1, -1, 11, "超级出价 -1 超出 0~9 的范围"),
    (1, 10, 11, "超级出价 10 超出 0~9 的范围"),
    (1, 1, 11, "超级出价 1 与你的正常出价相同，请选择不同的数值"),
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
