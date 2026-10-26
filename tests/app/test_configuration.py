"""配置读取、报错和 AI 比赛启动提示。"""

import json

import pytest
import yaml

from auction_game.cli import check_llm
from auction_game import runners
from auction_game.configuration import ConfigurationError, EXECUTION_FIELDS, load_configuration
from auction_game.model_capabilities import DEFAULT_MAX_TOKENS
from tests.support.factories import CHANNELS


def rewrite(path, change):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_defaults_and_preserved_values(config_files):
    config, players = load_configuration(*config_files)
    assert config["llm"] == {"max_concurrency": 10, "timeout_seconds": 60, "channels": CHANNELS}
    assert config["test_model"] == "substitute"
    assert config["prompts"] == {"enable_predictions": True, "enable_personas": True,
                                 "enable_writing_style": True}
    assert config["writing_style"] == "【写作要求】\n合成写作要求。\n"
    assert [player["predicted_win_rate"] for player in players] == [23, 0]
    assert [p["protocol"] for p in players] == ["chat", "anthropic"]


def test_missing_prompt_switch_defaults_to_true(config_files):
    rewrite(config_files[0], lambda data: data.update(prompts={"enable_predictions": False}))
    config, _ = load_configuration(*config_files)
    assert config["prompts"] == {"enable_predictions": False, "enable_personas": True,
                                 "enable_writing_style": True}


def test_disabled_writing_style_skips_file(config_files, tmp_path):
    (tmp_path / "writing_style.md").unlink()
    rewrite(config_files[0], lambda data: data.update(prompts={"enable_writing_style": False}))
    config, _ = load_configuration(*config_files)
    assert config["writing_style"] is None


def test_missing_writing_style_file_is_configuration_error(config_files, tmp_path):
    path = tmp_path / "writing_style.md"
    path.unlink()
    with pytest.raises(ConfigurationError, match="写作要求读取失败") as exc:
        load_configuration(*config_files)
    assert str(path) in str(exc.value)


def test_default_paths_do_not_depend_on_cwd(tmp_path, monkeypatch):
    expected = load_configuration()
    monkeypatch.chdir(tmp_path)
    assert load_configuration() == expected
    assert load_configuration("configs/config.yaml", "configs/players.yaml") == expected


@pytest.mark.parametrize("change,field", [
    (lambda data: data.update(prompts={"enable_personas": "false"}), "prompts.enable_personas"),
    (lambda data: data["llm"].update(timeout_seconds="60"), "llm.timeout_seconds"),
    (lambda data: data["llm"].update(max_concurrency=0), "llm.max_concurrency"),
    (lambda data: data["llm"]["channels"]["hub"].update(base_url="ftp://host"), "base_url"),
    (lambda data: data.update(test_model="missing"), "test_model"),
], ids=["prompt-switch", "timeout", "concurrency", "base-url", "test-model"])
def test_invalid_config_values(config_files, change, field):
    rewrite(config_files[0], change)
    with pytest.raises(ConfigurationError, match=field) as exc:
        load_configuration(*config_files)
    assert str(config_files[0]) in str(exc.value)


@pytest.mark.parametrize("file_index,text,field", [
    (0, "[]", "根节点"),
    (0, "llm: []", "llm"),
    (1, "players: []", "至少两名玩家"),
])
def test_invalid_structure(config_files, file_index, text, field):
    path = config_files[file_index]
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc:
        load_configuration(*config_files)
    assert str(path) in str(exc.value)
    assert field in str(exc.value)


@pytest.mark.parametrize("field,value", [
    ("model", "substitute"), ("predicted_win_rate", 101),
])
def test_invalid_player_fields(config_files, field, value):
    players_path = config_files[1]
    rewrite(players_path, lambda data: data["players"][0].update({field: value}))
    with pytest.raises(ConfigurationError) as exc:
        load_configuration(*config_files)
    assert str(players_path) in str(exc.value)
    assert f"players[0].{field}" in str(exc.value)


def test_unknown_player_field_rejected(config_files):
    rewrite(config_files[1], lambda data: data["players"][0].update(name="重复维护名字"))
    with pytest.raises(ConfigurationError, match="不支持字段.*name"):
        load_configuration(*config_files)


def test_duplicate_names(config_files):
    rewrite(config_files[2], lambda data: data["models"]["second"].update(name="甲"))
    with pytest.raises(ConfigurationError, match=r"players\[1\].*名字重复"):
        load_configuration(*config_files)


def test_invalid_catalog_protocol(config_files):
    rewrite(config_files[2], lambda data: data["models"]["first"].update(protocol="invalid"))
    with pytest.raises(ConfigurationError, match="models.first.protocol"):
        load_configuration(*config_files)


def test_unknown_channel(config_files):
    rewrite(config_files[2], lambda data: data["models"]["first"].update(provider="missing"))
    with pytest.raises(ConfigurationError, match=r"models.first.provider.*未知渠道"):
        load_configuration(*config_files)


def test_missing_file(tmp_path):
    path = tmp_path / "missing.yaml"
    with pytest.raises(ConfigurationError) as exc:
        load_configuration(path, path)
    assert str(path) in str(exc.value)
    assert "读取失败" in str(exc.value)


@pytest.mark.parametrize("entry", [runners, check_llm])
def test_entries_report_configuration_errors(config_files, monkeypatch, capsys, entry):
    config_path = config_files[0]
    config_path.write_text("llm: null", encoding="utf-8")
    monkeypatch.setattr(entry, "load_configuration", lambda **kwargs: load_configuration(*config_files, **kwargs))
    monkeypatch.setattr(entry, "make_model_factory", lambda **kwargs: pytest.fail("配置错误时不应创建客户端"))
    if entry is runners:
        monkeypatch.setattr(runners, "make_run_dir", lambda mode: pytest.fail("配置错误时不应创建日志"))
        assert runners.run_ai() == 1
    else:
        assert check_llm.main([]) == 1
    output = capsys.readouterr().out
    assert str(config_path) in output
    assert "llm" in output
    assert "错误" in output


@pytest.mark.parametrize("test_key", ["substitute", "first"])
def test_test_model_replaces_entire_execution_without_changing_identity(config_files, test_key):
    config_path, _, models_path = config_files
    rewrite(models_path, lambda data: data["models"]["substitute"].update(reasoning_effort="low"))
    # 先加载带 low 覆盖的替身，再切换目录键，检查旧配置不污染下一次启动。
    assert load_configuration(*config_files, test_model=True)[1][0]["model_settings"] == {
        "openai_reasoning_effort": "low"}
    rewrite(config_path, lambda data: data.update(test_model=test_key))
    config, regular = load_configuration(*config_files)
    _, testing = load_configuration(*config_files, test_model=True)
    target = config["models"][config["test_model"]]
    for original, replaced in zip(regular, testing):
        for key in ("name", "persona_title", "persona_text", "predicted_win_rate", "identity_model"):
            assert original[key] == replaced[key]
        for key in EXECUTION_FIELDS:
            assert replaced[key] == target[key]
        assert replaced["model_key"] == config["test_model"]
    assert testing[0] is not testing[1]
    if test_key == "first":
        assert all(player["model_settings"] == {} for player in testing)
        assert config["models"]["substitute"]["model_settings"] == {"openai_reasoning_effort": "low"}
    assert load_configuration(*config_files)[1] == regular


def test_missing_key_stops_before_logs(config_files, monkeypatch, capsys):
    from auction_game.ai import llm_client
    monkeypatch.setattr(runners, "load_configuration", lambda **kwargs: load_configuration(*config_files, **kwargs))
    monkeypatch.setattr(llm_client, "load_dotenv", lambda path: None)
    monkeypatch.delenv("FAKE_KEY", raising=False)
    monkeypatch.setattr(runners, "make_run_dir", lambda mode: pytest.fail("缺密钥不应创建日志"))
    assert runners.run_ai() == 1
    output = capsys.readouterr().out
    assert "FAKE_KEY" in output and "项目根 .env" in output


@pytest.mark.parametrize("test_model", [False, True])
def test_ai_startup_prints_lineup_and_budget_warning_once_per_model(
        config_files, monkeypatch, tmp_path, capsys, test_model):
    snapshot_path = config_files[2].parent / "model_capabilities.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    for entry in snapshot["models"].values():
        entry["limit"]["output"] = None
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    config_path = config_files[0]
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["llm"].update(max_concurrency=3, timeout_seconds=7)
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    factories = []
    monkeypatch.setattr(runners, "load_configuration", lambda **kwargs: load_configuration(*config_files, **kwargs))
    monkeypatch.setattr(runners, "make_model_factory", lambda **kwargs: factories.append(kwargs))
    monkeypatch.setattr(runners, "make_run_dir", lambda mode: tmp_path / mode)
    monkeypatch.setattr(runners.Controller, "start_stdin_reader", lambda self: None)
    monkeypatch.setattr(runners, "run_tournament",
                        lambda *args, **kwargs: (_ for _ in ()).throw(runners.TournamentAborted))
    assert runners.run_ai(test_model=test_model) == 0
    output = capsys.readouterr().out
    if test_model:
        assert "测试模型：substitute / test-model-c（全员使用，调用真实服务，可能收费）" in output
        assert output.count("[预算提醒] 测试模型：") == 1
    else:
        assert "常规阵容：" in output and "甲：test-model-a" in output and "乙：test-model-b" in output
        assert output.count("[预算提醒]") == 2
    assert f"暂用 {DEFAULT_MAX_TOKENS} tokens" in output
    assert "python -m auction_game.cli.sync_models --write" in output
    assert "操作：回车=下一轮" in output
    snapshot = json.loads((tmp_path / ("ai-test" if test_model else "ai") / "data" / "run_config.json")
                          .read_text(encoding="utf-8"))
    assert snapshot["mode"] == ("test_model" if test_model else "regular")
    assert snapshot["max_concurrency"] == 3
    first = factories[0]
    assert (first["model"], first["protocol"], first["channel"], first["timeout"]) == (
        ("test-model-c", "responses") if test_model else ("test-model-a", "chat")) + (CHANNELS["hub"], 7)
