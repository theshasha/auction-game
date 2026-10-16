"""分离配置读取、报错及两个入口的参数传递；不调用真实服务或写入项目日志。"""

from types import SimpleNamespace

import pytest
import yaml

import check_llm
import runners
from configuration import ConfigurationError, load_configuration
from game_rules import GameSettings

CHANNELS = {
    "hub": {"base_url": "https://offline.invalid/v1", "api_key_env": "FAKE_KEY"},
    "anthropic": {"base_url": "https://offline.invalid/anthropic", "api_key_env": "FAKE_KEY"},
}


@pytest.fixture
def config_files(tmp_path):
    config_path = tmp_path / "config.yaml"
    players_path = tmp_path / "players.yaml"
    config_path.write_text(yaml.safe_dump({"llm": {"channels": CHANNELS}}), encoding="utf-8")
    players = [
        dict(name="甲", persona_title="姐姐", persona_text="沉稳温柔", provider="hub",
              model="test-model-a", protocol="chat", max_tokens=123, predicted_win_rate=23),
        dict(name="乙", persona_title="妹妹", persona_text="活泼好奇", provider="anthropic",
              model="test-model-b", protocol="anthropic", max_tokens=456),
    ]
    players_path.write_text(yaml.safe_dump({"players": players}), encoding="utf-8")
    return config_path, players_path


def test_defaults_and_preserved_values(config_files):
    config, players = load_configuration(*config_files)
    assert config == {"llm": {"timeout_seconds": 60, "channels": CHANNELS}}
    assert [player["predicted_win_rate"] for player in players] == [23, 0]
    assert [p["protocol"] for p in players] == ["chat", "anthropic"]


def test_default_paths_do_not_depend_on_cwd(tmp_path, monkeypatch):
    expected = load_configuration()
    monkeypatch.chdir(tmp_path)
    assert load_configuration() == expected
    assert load_configuration("config.yaml", "players.yaml") == expected


@pytest.mark.parametrize("timeout", [True, 0, float("nan"), "60", None])
def test_invalid_timeout(config_files, timeout):
    config_path, _ = config_files
    config_path.write_text(yaml.safe_dump({"llm": {"timeout_seconds": timeout}}), encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc:
        load_configuration(*config_files)
    assert str(config_path) in str(exc.value)
    assert "llm.timeout_seconds" in str(exc.value)


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
    ("name", " "), ("persona_title", None), ("persona_text", ""),
    ("provider", 12), ("model", None), ("protocol", []), ("protocol", " "),
    ("protocol", None), ("protocol", "gemini"), ("protocol", "modal"),
    ("max_tokens", None), ("max_tokens", True), ("max_tokens", 0),
    ("max_tokens", 2.5), ("max_tokens", "123"),
])
def test_invalid_player_fields(config_files, field, value):
    _, players_path = config_files
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
    _, players_path = config_files
    data = yaml.safe_load(players_path.read_text(encoding="utf-8"))
    data["players"][1]["name"] = data["players"][0]["name"]
    players_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigurationError, match=r"players\[1\]\.name.*重复"):
        load_configuration(*config_files)


def test_missing_file(tmp_path):
    path = tmp_path / "missing.yaml"
    with pytest.raises(ConfigurationError) as exc:
        load_configuration(path, path)
    assert str(path) in str(exc.value)
    assert "读取失败" in str(exc.value)


@pytest.mark.parametrize("entry", [runners, check_llm])
def test_entries_use_shared_configuration(config_files, monkeypatch, tmp_path, entry):
    config_path, _ = config_files
    config_path.write_text(yaml.safe_dump({"llm": {"timeout_seconds": 900.5, "channels": CHANNELS}}), encoding="utf-8")
    loaded = []
    factories = []
    chats = []

    def load():
        loaded.append(True)
        return load_configuration(*config_files)

    def make_model_factory(**kwargs):
        factories.append(kwargs)
        from conftest import fake_task_runner
        runner = fake_task_runner(["测试回复"])
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
        monkeypatch.setattr(runners, "make_run_dir", lambda mode: tmp_path)
        monkeypatch.setattr(runners, "Controller", lambda mode: SimpleNamespace(start_stdin_reader=lambda: None))

        def tournament(players, actual_game, **kwargs):
            assert actual_game is game
            assert kwargs["director"].game is game
            assert [p.name for p in players] == ["甲", "乙"]
            assert "沉稳温柔" in players[0].messages[0].parts[0].content
            assert "活泼好奇" in players[1].messages[0].parts[0].content
            for player in players:
                assert player.game is game
                assert player.log_path.parent == tmp_path
                assert player.final_summary("测试总结") == "测试回复"
            raise runners.TournamentAborted

        monkeypatch.setattr(runners, "run_tournament", tournament)
        assert runners.run_ai() == 0
        assert games_loaded == [True]
        assert (tmp_path / "RULES.md").read_text(encoding="utf-8") == runners.render_rules(game, ["甲", "乙"])
    else:
        assert check_llm.main() == 0
        for runner in chats:
            parts = [p for m in runner.calls[0] for p in m.parts]
            assert parts[0] == check_llm.PING_HISTORY[0].parts[0]
            assert parts[1].content == check_llm.PING_PROMPT
    assert loaded == [True]
    assert len(chats) == 2
    assert factories == [
        dict(provider="hub", channel=CHANNELS["hub"], model="test-model-a", timeout=900.5,
             protocol="chat", max_tokens=123),
        dict(provider="anthropic", channel=CHANNELS["anthropic"], model="test-model-b", timeout=900.5,
             protocol="anthropic", max_tokens=456),
    ]


@pytest.mark.parametrize("entry", [runners, check_llm])
def test_entries_report_configuration_errors(config_files, monkeypatch, capsys, entry):
    config_path, _ = config_files
    config_path.write_text("llm: null", encoding="utf-8")
    monkeypatch.setattr(entry, "load_configuration", lambda: load_configuration(*config_files))
    monkeypatch.setattr(entry, "make_model_factory", lambda **kwargs: pytest.fail("配置错误时不应创建客户端"))
    if entry is runners:
        monkeypatch.setattr(runners, "make_run_dir", lambda mode: pytest.fail("配置错误时不应创建日志"))
        assert runners.run_ai() == 1
    else:
        assert check_llm.main() == 1
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
])
def test_invalid_channels(config_files, channel, field):
    config_path, _ = config_files
    config_path.write_text(yaml.safe_dump({"llm": {"channels": channel}}), encoding="utf-8")
    with pytest.raises(ConfigurationError, match=field):
        load_configuration(*config_files)


def test_unknown_channel(config_files):
    _, path = config_files
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["players"][0]["provider"] = "modal"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigurationError, match=r"players\[0\].provider.*未知渠道"):
        load_configuration(*config_files)


def test_actual_six_player_routes_and_budgets():
    config, players = load_configuration()
    assert [(p["model"], p["provider"], p["protocol"], p["max_tokens"]) for p in players] == [
        ("kimi-k3", "hub_anthropic", "anthropic", 131072),
        ("deepseek-v4-pro", "hub", "chat", 384000),
        ("gemini-3.1-pro-preview", "hub", "responses", 65536),
        ("gpt-5.6-sol", "hub", "responses", 128000),
        ("gemini-3.7-flash", "hub", "responses", 65536),
        ("deepseek-v4-flash", "hub", "chat", 384000),
    ]
    assert config["llm"]["channels"] == {
        "hub": {"base_url": "https://hub.oaifree.com/v1", "api_key_env": "HUB_API_KEY"},
        "hub_anthropic": {"base_url": "https://hub.oaifree.com/anthropic", "api_key_env": "HUB_API_KEY"},
    }


def test_missing_key_stops_before_logs_without_reading_dotenv(config_files, monkeypatch, capsys):
    import llm_client
    monkeypatch.setattr(runners, "load_configuration", lambda: load_configuration(*config_files))
    monkeypatch.setattr(llm_client, "load_dotenv", lambda path: None)
    monkeypatch.delenv("FAKE_KEY", raising=False)
    monkeypatch.setattr(runners, "make_run_dir", lambda mode: pytest.fail("缺密钥不应创建日志"))
    assert runners.run_ai() == 1
    assert "FAKE_KEY" in capsys.readouterr().out
