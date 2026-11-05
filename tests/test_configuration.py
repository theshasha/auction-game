"""配置读取、报错和 AI 比赛启动提示。"""

import json

import pytest
import yaml

from auction_game.cli import check_llm
from auction_game import runners
from auction_game.configuration import ConfigurationError, load_configuration
from auction_game.game.lineup import Lineup
from auction_game.model_capabilities import DEFAULT_MAX_TOKENS
from auction_game.reporting.run_paths import RunPaths
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
    assert [player.profile.predicted_win_rate for player in players] == [23, 0]
    assert [p.execution.protocol for p in players] == ["chat", "anthropic"]


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
    ("model", "missing"), ("predicted_win_rate", 101),
])
def test_invalid_player_fields(config_files, field, value):
    players_path = config_files[1]
    rewrite(players_path, lambda data: data["players"][0].update({field: value}))
    with pytest.raises(ConfigurationError) as exc:
        load_configuration(*config_files)
    assert str(players_path) in str(exc.value)
    assert f"players[0].{field}" in str(exc.value)


def test_unknown_player_field_rejected(config_files):
    rewrite(config_files[1], lambda data: data["players"][0].update(nickname="昵称"))
    with pytest.raises(ConfigurationError, match="不支持字段.*nickname"):
        load_configuration(*config_files)


def test_lineup_name_replaces_catalog_name_and_keeps_identity(config_files):
    _, original = load_configuration(*config_files)
    rewrite(config_files[1], lambda data: data["players"][0].update(name="小红"))
    _, renamed = load_configuration(*config_files)
    _, testing = load_configuration(*config_files, test_model=True)
    assert [player.profile.name for player in renamed] == [player.profile.name for player in testing] == ["小红", "乙"]
    assert [player.identity for player in renamed] == [player.identity for player in original]


def test_same_model_plays_twice_with_separate_names(config_files):
    def add_twin(data):
        data["players"][0]["name"] = "甲一"
        data["players"].append({**data["players"][0], "name": "甲二"})

    rewrite(config_files[1], add_twin)
    _, players = load_configuration(*config_files)
    assert [(player.profile.name, player.identity.key) for player in players] == [
        ("甲一", "first"), ("乙", "second"), ("甲二", "first")]


def test_catalog_entry_without_name_needs_lineup_name(config_files):
    players_path = config_files[1]
    rewrite(players_path, lambda data: data["players"][1].update(model="substitute"))
    with pytest.raises(ConfigurationError) as exc:
        load_configuration(*config_files)
    assert f"{players_path}：players[1] 没有名字：在这里写 name，或给 models.substitute 加 name" == str(exc.value)
    rewrite(players_path, lambda data: data["players"][1].update(name="丙"))
    _, players = load_configuration(*config_files)
    assert (players[1].profile.name, players[1].identity.key) == ("丙", "substitute")


@pytest.mark.parametrize("name,reason", [
    ("", "必须是非空字符串"),
    (7, "必须是非空字符串"),
    ("小 红", "不能含空白和 <、>、/：'小 红'"),
    ("小　红", "不能含空白和 <、>、/"),
    ("小\n红", "不能含空白和 <、>、/"),
    ("<小红>", "不能含空白和 <、>、/：'<小红>'"),
    ("甲/乙", "不能含空白和 <、>、/：'甲/乙'"),
    ("strategy", "不能是 strategy、decision、speech、队内信件区：'strategy'"),
    ("decision", "不能是 strategy、decision、speech、队内信件区：'decision'"),
    ("speech", "不能是 strategy、decision、speech、队内信件区：'speech'"),
    ("队内信件区", "不能是 strategy、decision、speech、队内信件区：'队内信件区'"),
])
def test_invalid_lineup_names(config_files, name, reason):
    players_path = config_files[1]
    rewrite(players_path, lambda data: data["players"][0].update(name=name))
    with pytest.raises(ConfigurationError) as exc:
        load_configuration(*config_files)
    assert str(exc.value).startswith(f"{players_path}：players[0].name {reason}")


def test_catalog_name_is_checked_only_when_used(config_files):
    _, players_path, models_path = config_files

    def rename(data):
        data["models"]["second"]["name"] = "乙 二"
        data["models"]["substitute"]["name"] = "<替身>"

    rewrite(models_path, rename)
    with pytest.raises(ConfigurationError) as exc:
        load_configuration(*config_files)
    assert str(exc.value) == f"{players_path}：players[1] 的名字取自 models.second.name，不能含空白和 <、>、/：'乙 二'"
    rewrite(players_path, lambda data: data["players"][1].update(name="乙二"))
    _, players = load_configuration(*config_files)
    assert [player.profile.name for player in players] == ["甲", "乙二"]


@pytest.mark.parametrize("file_index,change", [
    (2, lambda data: data["models"]["second"].update(name="甲")),
    (1, lambda data: data["players"][1].update(name="甲")),
], ids=["catalog", "lineup"])
def test_duplicate_names(config_files, file_index, change):
    players_path = config_files[1]
    rewrite(config_files[file_index], change)
    with pytest.raises(ConfigurationError) as exc:
        load_configuration(*config_files)
    assert str(exc.value) == f"{players_path}：players[1] 名字重复：'甲'；同一个模型放多名玩家时，给每人写 name"


def test_teams_load_in_seat_order(config_files, teams_file):
    _, players = load_configuration(*config_files, team=True, teams_path=teams_file)
    assert [(player.profile.name, player.profile.team, player.identity.key) for player in players] == [
        ("甲", "红队", "first"), ("乙", "红队", "second"), ("丙", "红队", "substitute"),
        ("丁", "蓝队", "first"), ("戊", "蓝队", "second"), ("己", "蓝队", "substitute")]
    assert all(player.profile.predicted_win_rate is None for player in players)
    assert all(player.execution == player.identity for player in players)


def test_team_test_model_replaces_only_execution(config_files, teams_file):
    config, regular = load_configuration(*config_files, team=True, teams_path=teams_file)
    _, testing = load_configuration(*config_files, team=True, teams_path=teams_file, test_model=True)
    for original, replaced in zip(regular, testing, strict=True):
        assert (replaced.profile, replaced.identity) == (original.profile, original.identity)
        assert replaced.execution == config["models"]["substitute"]


@pytest.mark.parametrize("change,message", [
    (lambda data: data["teams"].pop(), "teams 必须是正好 2 个阵营的列表"),
    (lambda data: data["teams"].append({**data["teams"][1], "name": "绿队"}), "teams 必须是正好 2 个阵营的列表"),
    (lambda data: data["teams"][0]["players"].pop(), "teams[0].players 必须是正好 3 名玩家的列表"),
    (lambda data: data["teams"][1].update(name=" "), "teams[1].name 必须是非空字符串"),
    (lambda data: data["teams"][1].update(name="红队"), "teams[1].name 阵营名重复：'红队'"),
    (lambda data: data["teams"][0].update(color="red"), "teams[0] 不支持字段：color"),
    (lambda data: data["teams"][0]["players"][0].update(predicted_win_rate=10),
     "teams[0].players[0] 不支持字段：predicted_win_rate"),
    (lambda data: data["teams"][1]["players"][0].update(name="甲"), "teams[1].players[0] 名字重复：'甲'"),
], ids=["one-team", "three-teams", "two-players", "empty-team-name", "duplicate-team-name", "unknown-team-field",
        "prediction", "duplicate-player-name"])
def test_invalid_teams(config_files, teams_file, change, message):
    rewrite(teams_file, change)
    with pytest.raises(ConfigurationError) as exc:
        load_configuration(*config_files, team=True, teams_path=teams_file)
    assert str(exc.value).startswith(f"{teams_file}：{message}")


def test_default_teams_file_has_two_teams_of_three():
    _, players = load_configuration(team=True)
    assert [len(members) for members in Lineup(tuple(player.profile for player in players)).teams.values()] == [3, 3]


@pytest.mark.parametrize("field", ["reasoning_efort", "key"])
def test_unknown_catalog_field_rejected(config_files, field):
    models_path = config_files[2]
    rewrite(models_path, lambda data: data["models"]["first"].update({field: "low"}))
    with pytest.raises(ConfigurationError) as exc:
        load_configuration(*config_files)
    assert str(exc.value) == f"{models_path}：models.first 不支持字段：{field}"


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
    assert load_configuration(*config_files, test_model=True)[1][0].execution.model_settings == {
        "openai_reasoning_effort": "low"}
    rewrite(config_path, lambda data: data.update(test_model=test_key))
    config, regular = load_configuration(*config_files)
    _, testing = load_configuration(*config_files, test_model=True)
    target = config["models"][config["test_model"]]
    for original, replaced in zip(regular, testing):
        assert (replaced.profile, replaced.identity) == (original.profile, original.identity)
        assert replaced.execution == target
    if test_key == "first":
        assert all(player.execution.model_settings == {} for player in testing)
        assert config["models"]["substitute"].model_settings == {"openai_reasoning_effort": "low"}
    assert load_configuration(*config_files)[1] == regular


def test_team_entry_reports_teams_errors_before_logs(config_files, teams_file, monkeypatch, capsys):
    rewrite(teams_file, lambda data: data["teams"].pop())
    monkeypatch.setattr(runners, "load_configuration",
                        lambda **kwargs: load_configuration(*config_files, teams_path=teams_file, **kwargs))
    monkeypatch.setattr(runners, "make_model_factory", lambda **kwargs: pytest.fail("配置错误时不应创建客户端"))
    monkeypatch.setattr(runners, "make_run_dir", lambda mode: pytest.fail("配置错误时不应创建日志"))
    assert runners.run_ai(team=True) == 1
    assert f"[配置错误] {teams_file}：teams 必须是正好 2 个阵营的列表" in capsys.readouterr().out


@pytest.mark.parametrize("test_model", [False, True])
def test_team_startup_records_teams_and_hands_them_to_director(
        config_files, teams_file, monkeypatch, tmp_path, capsys, test_model):
    monkeypatch.setattr(runners, "load_configuration",
                        lambda **kwargs: load_configuration(*config_files, teams_path=teams_file, **kwargs))
    monkeypatch.setattr(runners, "make_model_factory", lambda **kwargs: None)
    monkeypatch.setattr(runners, "make_run_dir", lambda mode: tmp_path / mode)
    monkeypatch.setattr(runners.Controller, "start_stdin_reader", lambda self: None)
    directors = []

    def stop(director):
        directors.append(director)
        raise runners.TournamentAborted

    monkeypatch.setattr(runners.AIDirector, "run", stop)
    assert runners.run_ai(test_model=test_model, team=True) == 0
    assert "提示设置：赛前预测不用于团队赛，" in capsys.readouterr().out
    run_type = "team-test" if test_model else "team"
    paths = RunPaths(tmp_path / run_type)
    snapshot = paths.read_config()
    team_of = {"甲": "红队", "乙": "红队", "丙": "红队", "丁": "蓝队", "戊": "蓝队", "己": "蓝队"}
    assert snapshot["run_type"] == run_type
    assert {pc["name"]: pc["team"] for pc in snapshot["players"]} == team_of
    assert directors[0].team_of == team_of
    assert "红队有 甲、乙、丙，蓝队有 丁、戊、己" in (paths.root / "RULES.md").read_text(encoding="utf-8")
    system = json.loads(paths.player(1).read_text(encoding="utf-8").splitlines()[0])["content"]
    assert "你叫 甲，属于红队，队友是乙、丙；对方是蓝队：丁、戊、己。" in system


def test_missing_key_stops_before_logs(config_files, monkeypatch, capsys):
    from auction_game.llm import client as llm_client
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
    monkeypatch.setattr(runners.AIDirector, "run",
                        lambda self: (_ for _ in ()).throw(runners.TournamentAborted))
    assert runners.run_ai(test_model=test_model) == 0
    output = capsys.readouterr().out
    if test_model:
        assert "测试模型：substitute / test-model-c（全员使用，调用真实服务，可能收费）" in output
        assert output.count("[预算提醒] 测试模型：") == 1
    else:
        assert "常规阵容（调用真实服务，可能收费）：" in output and "甲：test-model-a" in output and "乙：test-model-b" in output
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
