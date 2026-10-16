"""分离配置读取、报错及两个入口的参数传递；不调用真实服务或写入项目日志。"""

from types import SimpleNamespace
import json

import pytest
import yaml

from auction_game.cli import check_llm
from auction_game import runners
from auction_game.configuration import ConfigurationError, load_configuration
from auction_game.game.game_rules import GameSettings
from tests.support.factories import CHANNELS


def test_defaults_and_preserved_values(config_files):
    config, players = load_configuration(*config_files)
    assert config["llm"] == {"max_concurrency": 10, "timeout_seconds": 60, "channels": CHANNELS}
    assert config["test_model"] == "substitute"
    assert [player["predicted_win_rate"] for player in players] == [23, 0]
    assert [p["protocol"] for p in players] == ["chat", "anthropic"]


def test_default_paths_do_not_depend_on_cwd(tmp_path, monkeypatch):
    expected = load_configuration()
    monkeypatch.chdir(tmp_path)
    assert load_configuration() == expected
    assert load_configuration("configs/config.yaml", "configs/players.yaml") == expected


@pytest.mark.parametrize("timeout", [True, 0, float("nan"), "60", None])
def test_invalid_timeout(config_files, timeout):
    config_path = config_files[0]
    config_path.write_text(yaml.safe_dump({"llm": {"timeout_seconds": timeout}}), encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc:
        load_configuration(*config_files)
    assert str(config_path) in str(exc.value)
    assert "llm.timeout_seconds" in str(exc.value)


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, 2.0, "10", None])
def test_invalid_max_concurrency(config_files, value):
    path = config_files[0]
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["llm"]["max_concurrency"] = value
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigurationError, match=r"llm.max_concurrency.*正整数") as exc:
        load_configuration(*config_files)
    assert str(path) in str(exc.value)


@pytest.mark.parametrize("value", [1, 2, 10, 100])
def test_valid_max_concurrency(config_files, value):
    path = config_files[0]
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["llm"]["max_concurrency"] = value
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    config, _ = load_configuration(*config_files)
    assert config["llm"]["max_concurrency"] == value


@pytest.mark.parametrize("file_index,text,field", [
    (0, "[]", "根节点"),
    (0, "llm: []", "llm"),
    (0, "players: []", "players"),
    (1, "[]", "根节点"),
    (1, "{}", "players"),
    (1, "players: []", "players"),
    (1, "players: {}", "players"),
    (1, "players: [null]", "players[0]"),
    (1, "players: [", "读取失败"),
])
def test_invalid_structure(config_files, file_index, text, field):
    path = config_files[file_index]
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc:
        load_configuration(*config_files)
    assert str(path) in str(exc.value)
    assert field in str(exc.value)


@pytest.mark.parametrize("field,value", [
    ("persona_title", None), ("persona_text", ""), ("model", None),
    ("model", "missing"), ("model", "substitute"),
    ("predicted_win_rate", True), ("predicted_win_rate", float("nan")),
    ("predicted_win_rate", -1), ("predicted_win_rate", 101),
])
def test_invalid_player_fields(config_files, field, value):
    players_path = config_files[1]
    data = yaml.safe_load(players_path.read_text(encoding="utf-8"))
    if value is None:
        del data["players"][0][field]
    else:
        data["players"][0][field] = value
    players_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc:
        load_configuration(*config_files)
    assert str(players_path) in str(exc.value)
    assert f"players[0].{field}" in str(exc.value)


def test_duplicate_names(config_files):
    path = config_files[2]
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["models"]["second"]["name"] = "甲"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigurationError, match=r"models.second.name.*重复"):
        load_configuration(*config_files)


def test_missing_file(tmp_path):
    path = tmp_path / "missing.yaml"
    with pytest.raises(ConfigurationError) as exc:
        load_configuration(path, path)
    assert str(path) in str(exc.value)
    assert "读取失败" in str(exc.value)


@pytest.mark.parametrize("entry", [runners, check_llm])
@pytest.mark.parametrize("test_model", [False, True])
def test_entries_use_shared_configuration(config_files, monkeypatch, tmp_path, entry, test_model):
    config_path = config_files[0]
    config_path.write_text(yaml.safe_dump({"test_model": "substitute", "llm": {"max_concurrency": 2, "timeout_seconds": 900.5, "channels": CHANNELS}}), encoding="utf-8")
    loaded = []
    factories = []
    chats = []

    def load(**kwargs):
        loaded.append(True)
        return load_configuration(*config_files, **kwargs)

    def make_model_factory(**kwargs):
        factories.append(kwargs)
        from tests.support.factories import fake_task_runner
        runner = fake_task_runner(["测试回复"] if entry is runners else [{"reply": "OK"}])
        chats.append(runner)
        return runner.executor.factory

    monkeypatch.setattr(entry, "load_configuration", load)
    monkeypatch.setattr(entry, "make_model_factory", make_model_factory)
    if entry is runners:
        game = GameSettings(13, 4, (2, 1, 3), 9, 1, 5, 2, 0)
        games_loaded = []

        def load_game():
            games_loaded.append(True)
            return game

        monkeypatch.setattr(runners, "load_game_settings", load_game)
        monkeypatch.setattr(runners, "make_run_dir", lambda mode: tmp_path / "run")
        monkeypatch.setattr(runners, "Controller", lambda mode: SimpleNamespace(start_stdin_reader=lambda: None))

        def tournament(players, actual_game, **kwargs):
            assert actual_game is game
            assert kwargs["director"].game is game
            assert kwargs["director"].controller.max_concurrency == 2
            assert [p.name for p in players] == ["甲", "乙"]
            assert "沉稳温柔" in players[0].messages[0].parts[0].content
            assert "活泼好奇" in players[1].messages[0].parts[0].content
            assert players[0].messages is not players[1].messages
            assert players[0].task_runner is not players[1].task_runner
            assert players[0].messages[0] is not players[1].messages[0]
            assert all("test-model-c" not in p.messages[0].parts[0].content for p in players)
            for player in players:
                assert player.game is game
                assert player.log_path.parent == tmp_path / "run" / "data" / "players"
                assert player.final_summary("测试总结") == "测试回复"
            raise runners.TournamentAborted

        monkeypatch.setattr(runners, "run_tournament", tournament)
        assert runners.run_ai(test_model=test_model) == 0
        assert games_loaded == [True]
        snapshot = json.loads((tmp_path / "run" / "data" / "run_config.json").read_text(encoding="utf-8"))
        assert snapshot["max_concurrency"] == 2
        assert (tmp_path / "run" / "RULES.md").read_text(encoding="utf-8") == runners.render_rules(game, ["甲", "乙"])
    else:
        assert check_llm.main(["--test-model"] if test_model else []) == 0
        for runner in chats:
            parts = [p for m in runner.calls[0] for p in m.parts]
            assert parts[0] == check_llm.PING_HISTORY[0].parts[0]
            assert parts[1].content == check_llm.PING_PROMPT
    assert loaded == [True]
    assert len(chats) == (1 if test_model and entry is check_llm else 2)
    expected = [
        dict(provider="hub", channel=CHANNELS["hub"], model="test-model-a", timeout=900.5,
             protocol="chat", max_tokens=123, model_settings={}),
        dict(provider="anthropic", channel=CHANNELS["anthropic"], model="test-model-b", timeout=900.5,
              protocol="anthropic", max_tokens=456, model_settings={}),
    ]
    if test_model:
        expected = [dict(provider="hub", channel=CHANNELS["hub"], model="test-model-c", timeout=900.5,
                         protocol="responses", max_tokens=789,
                         model_settings={"openai_reasoning_effort": "high"})] * len(chats)
    if entry is check_llm:
        for item in expected:
            item["request_attempts"] = 1
    assert factories == expected


@pytest.mark.parametrize("entry", [runners, check_llm])
def test_entries_report_configuration_errors(config_files, monkeypatch, capsys, entry):
    config_path = config_files[0]
    config_path.write_text("llm: null", encoding="utf-8")
    monkeypatch.setattr(entry, "load_configuration", lambda: load_configuration(*config_files))
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


@pytest.mark.parametrize("channel,field", [
    (None, "channels"), ({}, "channels"), ({"hub": None}, "hub"),
    ({"hub": {"base_url": "ftp://host", "api_key_env": "KEY"}}, "base_url"),
    ({"hub": {"base_url": "https://user:password@host", "api_key_env": "KEY"}}, "base_url"),
    ({"hub": {"base_url": "https://host?key=x", "api_key_env": "KEY"}}, "base_url"),
    ({"hub": {"base_url": "https://host:bad", "api_key_env": "KEY"}}, "base_url"),
    ({"hub": {"base_url": "https://host", "api_key_env": "bad-name"}}, "api_key_env"),
    ({"hub": {"base_url": "https://host", "api_key_env": None}}, "api_key_env"),
    ({"hub": {**CHANNELS["hub"], "responses_profile": None}}, "responses_profile"),
    ({"hub": {**CHANNELS["hub"], "responses_profile": {"supports_forced_tool_choice": "false"}}}, "responses_profile"),
    ({"hub": {**CHANNELS["hub"], "responses_profile": {"supports_tools": False}}}, "responses_profile"),
])
def test_invalid_channels(config_files, channel, field):
    config_path = config_files[0]
    config_path.write_text(yaml.safe_dump({"llm": {"channels": channel}}), encoding="utf-8")
    with pytest.raises(ConfigurationError, match=field):
        load_configuration(*config_files)


def test_unknown_channel(config_files):
    path = config_files[2]
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["models"]["first"]["provider"] = "missing"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigurationError, match=r"models.first.provider.*未知渠道"):
        load_configuration(*config_files)


def test_test_model_replaces_entire_execution_without_changing_identity(config_files):
    config, regular = load_configuration(*config_files)
    _, testing = load_configuration(*config_files, test_model=True)
    target = config["models"][config["test_model"]]
    for original, replaced in zip(regular, testing):
        for key in ("name", "persona_title", "persona_text", "predicted_win_rate", "identity_model"):
            assert original[key] == replaced[key]
        for key in ("model", "provider", "protocol", "max_tokens", "model_settings", "capabilities"):
            assert replaced[key] == target[key]
        assert replaced["model_key"] == config["test_model"]
    assert load_configuration(*config_files)[1] == regular


def test_missing_key_stops_before_logs_without_reading_dotenv(config_files, monkeypatch, capsys):
    from auction_game.ai import llm_client
    monkeypatch.setattr(runners, "load_configuration", lambda: load_configuration(*config_files))
    monkeypatch.setattr(llm_client, "load_dotenv", lambda path: None)
    monkeypatch.delenv("FAKE_KEY", raising=False)
    monkeypatch.setattr(runners, "make_run_dir", lambda mode: pytest.fail("缺密钥不应创建日志"))
    assert runners.run_ai() == 1
    assert "FAKE_KEY" in capsys.readouterr().out


@pytest.mark.parametrize("field,value", [
    ("model", ""), ("provider", 12), ("protocol", None), ("protocol", "invalid"),
    ("max_tokens", True), ("max_tokens", 0), ("max_tokens", 2.5), ("max_tokens", "123"),
    ("models_dev", None), ("models_dev", {"provider": "official"}),
    ("name", "../escape"), ("name", " "),
])
def test_invalid_catalog_fields(config_files, field, value):
    path = config_files[2]
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["models"]["first"][field] = value
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigurationError, match=f"models.first.{field}"):
        load_configuration(*config_files)


@pytest.mark.parametrize("target", [None, "missing", [], True])
def test_invalid_test_model_reference(config_files, target):
    path = config_files[0]
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["test_model"] = target
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="test_model"):
        load_configuration(*config_files)


def test_probe_selected_catalog_aliases_deduplicate_and_reject_unknown(config_files, monkeypatch):
    path = config_files[2]
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["models"]["alias"] = dict(data["models"]["substitute"])
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    snapshot_path = path.parent / "model_capabilities.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["models"]["alias"] = snapshot["models"]["substitute"]
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(check_llm, "load_configuration", lambda: load_configuration(*config_files))
    from tests.support.factories import fake_task_runner
    runner = fake_task_runner([{"reply": "OK"}])
    calls = []
    def factory(**kwargs):
        calls.append(kwargs)
        return runner.executor.factory
    monkeypatch.setattr(check_llm, "make_model_factory", factory)
    assert check_llm.main(["--model", "substitute", "--model", "alias"]) == 0
    assert len(calls) == len(runner.calls) == 1
    assert calls[0]["protocol"] == "responses" and calls[0]["max_tokens"] == 789
    assert check_llm.main(["--model", "absent"]) == 1
    assert len(calls) == 1


def test_old_player_format_and_repeated_identity_rejected(config_files):
    path = config_files[1]
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["players"][0]["name"] = "重复维护名字"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="不支持字段.*name"):
        load_configuration(*config_files)
    del data["players"][0]["name"]
    data["players"][1]["model"] = "first"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="名字重复"):
        load_configuration(*config_files)
