"""分离配置读取、报错及两个入口的参数传递；不调用真实服务或写入项目日志。"""

from types import SimpleNamespace

import pytest
import yaml

import check_llm
import main
from configuration import ConfigurationError, load_configuration


@pytest.fixture
def config_files(tmp_path):
    config_path = tmp_path / "config.yaml"
    players_path = tmp_path / "players.yaml"
    config_path.write_text("{}", encoding="utf-8")
    players = [
        dict(name="甲", persona_title="姐姐", persona_text="沉稳温柔", provider="hub",
             model="test-model-a", protocol="chat", predicted_win_rate=23),
        dict(name="乙", persona_title="妹妹", persona_text="活泼好奇", provider="anthropic",
             model="test-model-b"),
    ]
    players_path.write_text(yaml.safe_dump({"players": players}), encoding="utf-8")
    return config_path, players_path


def test_defaults_and_preserved_values(config_files):
    config, players = load_configuration(*config_files)
    assert config == {"llm": {"timeout_seconds": 60}}
    assert [p["predicted_win_rate"] for p in players] == [23, 0]
    assert [p["protocol"] if "protocol" in p else None for p in players] == ["chat", None]


def test_default_paths_do_not_depend_on_cwd(tmp_path, monkeypatch):
    expected = load_configuration()
    monkeypatch.chdir(tmp_path)
    assert load_configuration() == expected
    assert load_configuration("config.yaml", "players.yaml") == expected
    assert expected[0]["llm"]["timeout_seconds"] == 900
    assert len(expected[1]) == 6


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


@pytest.mark.parametrize("entry", [main, check_llm])
def test_entries_use_shared_configuration(config_files, monkeypatch, tmp_path, entry):
    config_path, _ = config_files
    config_path.write_text("llm:\n  timeout_seconds: 900.5\n", encoding="utf-8")
    loaded = []
    factories = []
    chats = []

    def load():
        loaded.append(True)
        return load_configuration(*config_files)

    def make_chat_fn(**kwargs):
        factories.append(kwargs)

        def chat(messages):
            chats.append(messages)
            return "测试回复"

        return chat

    monkeypatch.setattr(entry, "load_configuration", load)
    monkeypatch.setattr(entry, "make_chat_fn", make_chat_fn)
    if entry is main:
        monkeypatch.setattr(main, "make_run_dir", lambda mode: tmp_path)
        monkeypatch.setattr(main, "Controller", lambda mode: SimpleNamespace(start_stdin_reader=lambda: None))

        def tournament(players, **kwargs):
            assert [p.name for p in players] == ["甲", "乙"]
            assert "沉稳温柔" in players[0].messages[0]["content"]
            assert "活泼好奇" in players[1].messages[0]["content"]
            for player in players:
                assert player.log_path.parent == tmp_path
                player.chat_fn([])
            raise main.TournamentAborted

        monkeypatch.setattr(main, "run_tournament", tournament)
        main.run_ai()
    else:
        assert check_llm.main() == 0
        assert chats == [check_llm.PING_MESSAGES, check_llm.PING_MESSAGES]
    assert loaded == [True]
    assert len(chats) == 2
    assert factories == [
        dict(provider="hub", model="test-model-a", timeout=900.5, protocol="chat"),
        dict(provider="anthropic", model="test-model-b", timeout=900.5, protocol=None),
    ]


@pytest.mark.parametrize("entry", [main, check_llm])
def test_entries_report_configuration_errors(config_files, monkeypatch, capsys, entry):
    config_path, _ = config_files
    config_path.write_text("llm: null", encoding="utf-8")
    monkeypatch.setattr(entry, "load_configuration", lambda: load_configuration(*config_files))
    monkeypatch.setattr(entry, "make_chat_fn", lambda **kwargs: pytest.fail("配置错误时不应创建客户端"))
    if entry is main:
        monkeypatch.setattr(main, "make_run_dir", lambda mode: pytest.fail("配置错误时不应创建日志"))
        main.run_ai()
    else:
        assert check_llm.main() == 1
    output = capsys.readouterr().out
    assert str(config_path) in output
    assert "llm" in output
    assert "错误" in output
