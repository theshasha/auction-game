"""共用场次布局、目录冲突与默认分析类型边界。"""

import json
from dataclasses import asdict, replace
from datetime import datetime
from types import SimpleNamespace

import pytest

from auction_game.reporting import analyze, run_output
from auction_game.game.engine import compute_standings
from auction_game.reporting.run_paths import FORMAT_VERSION, RunPaths, validate_run_config
from tests.support.factories import TEST_GAME, make_log


def snapshot(run_type="ai", *, game=TEST_GAME, names=("甲", "乙")):
    config = {"format_version": FORMAT_VERSION, "run_type": run_type, "mode": "regular",
              "game": game.to_dict(), "players": [{"number": index, "name": name}
                                                   for index, name in enumerate(names, 1)]}
    if run_type != "bot":
        config["timeout_seconds"] = 3
        for pc in config["players"]:
            pc.update(identity_model={"model": "identity"}, execution_model={
                "protocol": "chat", "model": "execution", "base_url": "https://offline.invalid/v1",
                "max_tokens": 100})
    return config


def test_collision_and_default_analysis_only_valid_ai(tmp_path, monkeypatch):
    monkeypatch.setattr(run_output, "LOG_DIR", tmp_path)
    monkeypatch.setattr(analyze, "LOG_DIR", tmp_path)
    monkeypatch.setattr(run_output, "datetime", SimpleNamespace(now=lambda: datetime(2026, 10, 16, 12)))
    first = run_output.make_run_dir("ai")
    sentinel = first / "unchanged.txt"
    sentinel.write_text("keep", encoding="utf-8")
    second = run_output.make_run_dir("ai")
    assert second.name == first.name + "_2" and sentinel.read_text() == "keep"
    auction_scores = {"甲": 1, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 0}
    log = make_log(game=replace(TEST_GAME, daily_pool=(1,)), auction_scores=auction_scores,
                   scores=auction_scores)
    payload = {"match": log.to_dict(), "standings_after_match": [asdict(entry) for entry in compute_standings(
        {name: [score] for name, score in auction_scores.items()},
        scores_history={name: [score] for name, score in auction_scores.items()})]}
    for kind in ("offline", "ai-test", "bot"):
        path = run_output.make_run_dir(kind)
        paths = run_output.initialize_run(path, snapshot(kind, game=log.game, names=log.auction_scores), "rules")
        paths.match(1).write_text(json.dumps(payload), encoding="utf-8")
        assert analyze.load_match_logs(path)
    assert analyze.find_latest_ai_run() == tmp_path / "20261016_120000_ai-test"
    paths = run_output.initialize_run(second, snapshot(game=log.game, names=log.auction_scores), "rules")
    paths.match(1).write_text(json.dumps(payload), encoding="utf-8")
    assert analyze.find_latest_ai_run() == second
    with pytest.raises(FileExistsError):
        run_output.initialize_run(second, {}, "overwrite")
    with pytest.raises(ValueError, match="旧格式"):
        analyze.load_match_logs(first)
    with pytest.raises(FileExistsError):
        run_output.initialize_run(first, {}, "overwrite")


@pytest.mark.parametrize("mutation,error", [
    (lambda config: config.update(format_version=2), "旧格式"),
    (lambda config: config["game"].pop("super_hit_score"), "super_hit_score"),
    (lambda config: config["game"].update(win_score=4), "win_score"),
    (lambda config: config["game"].update(max_bid=True), "max_bid"),
    (lambda config: config["players"][0].update(number=True), "玩家编号"),
])
def test_snapshot_validation_shared_by_reader_and_writer(tmp_path, mutation, error):
    config = {"format_version": FORMAT_VERSION, "run_type": "bot", "mode": "regular",
              "game": TEST_GAME.to_dict(), "players": [{"number": 1, "name": "甲"}, {"number": 2, "name": "乙"}]}
    mutation(config)
    paths = RunPaths(tmp_path)
    paths.prepare()
    paths.config.write_text(json.dumps(config), encoding="utf-8")
    for validate in (lambda: validate_run_config(config), paths.read_config):
        with pytest.raises(ValueError, match=error):
            validate()


def test_initialize_rejects_invalid_snapshot_before_writing(tmp_path):
    snapshot = {"run_type": "bot", "mode": "regular", "game": TEST_GAME.to_dict(),
                "players": [{"name": "甲"}, {"name": "乙"}]}
    snapshot["game"]["win_score"] = 4
    with pytest.raises(ValueError, match="win_score"):
        run_output.initialize_run(tmp_path, snapshot, "rules")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("run_type", ["ai", "ai-test", "offline", "bot"])
def test_snapshot_requires_only_consumed_model_fields(run_type):
    config = snapshot(run_type)
    assert validate_run_config(config) is config


@pytest.mark.parametrize("run_type", ["ai", "ai-test", "offline"])
@pytest.mark.parametrize("field", ["timeout_seconds", "identity_model", "execution_model"])
def test_model_run_rejects_missing_source_before_read_or_write(tmp_path, run_type, field):
    config = snapshot(run_type)
    (config if field == "timeout_seconds" else config["players"][0]).pop(field)
    with pytest.raises(ValueError, match=field):
        run_output.initialize_run(tmp_path, config, "rules")
    assert list(tmp_path.iterdir()) == []
    paths = RunPaths(tmp_path)
    paths.prepare()
    paths.config.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match=field):
        paths.read_config()


def test_old_ai_snapshot_without_any_model_source_is_explicitly_rejected():
    config = snapshot("bot")
    config["run_type"] = "ai"
    with pytest.raises(ValueError, match="timeout_seconds"):
        validate_run_config(config)


@pytest.mark.parametrize("section,field,value", [
    (None, "timeout_seconds", value) for value in (None, True, "3", 0, -1, float("inf"), float("nan"))
] + [
    ("identity_model", "model", value) for value in (None, True, "", " ")
] + [
    ("execution_model", "protocol", value) for value in (None, True, "unknown", [])
] + [
    ("execution_model", "model", value) for value in (None, True, "", " ")
] + [
    ("execution_model", "base_url", value) for value in (
        None, True, "", "ftp://offline.invalid", "https://", "https://offline.invalid:bad",
        "https://user:secret@offline.invalid", "https://offline.invalid?key=secret", "https://bad host")
] + [
    ("execution_model", "max_tokens", value) for value in (None, True, "100", 1.5, 0, -1)
])
def test_model_snapshot_rejects_invalid_consumed_fields(tmp_path, section, field, value):
    config = snapshot()
    target = config if section is None else config["players"][0][section]
    target[field] = value
    paths = RunPaths(tmp_path)
    paths.prepare()
    paths.config.write_text(json.dumps(config), encoding="utf-8")
    for validate in (lambda: validate_run_config(config), paths.read_config):
        with pytest.raises(ValueError, match=field):
            validate()


@pytest.mark.parametrize("section,field", [
    ("identity_model", "model"), ("execution_model", "protocol"), ("execution_model", "model"),
    ("execution_model", "base_url"), ("execution_model", "max_tokens"),
])
def test_model_snapshot_rejects_missing_nested_fields(section, field):
    config = snapshot()
    config["players"][0][section].pop(field)
    with pytest.raises(ValueError, match=f"{section}.{field}"):
        validate_run_config(config)


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
def test_model_snapshot_accepts_supported_protocol_and_fractional_timeout(protocol):
    config = snapshot()
    config["timeout_seconds"] = 0.5
    config["players"][0]["execution_model"]["protocol"] = protocol
    assert validate_run_config(config) is config
