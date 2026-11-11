"""续跑入口：按场次快照组装阵容和模型，只从当前配置读渠道；续不了的场次在请求模型前说明原因。"""

import json

import pytest
import yaml

from auction_game import runners
from auction_game.configuration import load_configuration, load_llm_settings
from auction_game.game.settings import GameSettings
from auction_game.reporting.run_output import SUMMARY_TITLE, publish_speeches
from auction_game.reporting.run_paths import RunPaths


@pytest.fixture
def started_run(config_files, teams_file, monkeypatch, tmp_path):
    """用合成配置开一场团队测试模型赛，写完快照和系统提示就退出；返回场次目录。"""
    monkeypatch.setattr(runners, "load_configuration",
                        lambda **kwargs: load_configuration(*config_files, teams_path=teams_file, **kwargs))
    monkeypatch.setattr(runners, "load_llm_settings", lambda: load_llm_settings(config_files[0]))
    monkeypatch.setattr(runners, "make_run_dir", lambda mode: tmp_path / f"20261101_100000_{mode}")
    monkeypatch.setattr(runners.Controller, "start_stdin_reader", lambda self: None)
    with monkeypatch.context() as patch:
        patch.setattr(runners, "make_model_factory", lambda **kwargs: None)
        patch.setattr(runners.AIDirector, "run", lambda self: (_ for _ in ()).throw(runners.TournamentAborted))
        assert runners.run_ai(test_model=True, team=True) == 0
    return tmp_path / "20261101_100000_team-test"


def rewrite_yaml(path, change):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_resume_builds_lineup_and_models_from_snapshot_and_channels_from_current_config(
        started_run, config_files, teams_file, monkeypatch, capsys):
    paths = RunPaths(started_run)
    snapshot = paths.read_config()
    # 关掉之后改了比赛配置：阵容和模型目录删掉，比赛参数读不了；渠道换了地址，超时和并发也改了。
    teams_file.unlink()
    config_files[2].unlink()
    monkeypatch.setattr(runners, "load_configuration", lambda **kwargs: pytest.fail("续跑不读阵容和模型目录"))
    monkeypatch.setattr(runners, "load_game_settings", lambda **kwargs: pytest.fail("续跑不读当前比赛参数"))
    monkeypatch.setattr(runners, "sync_root_rules", lambda *args, **kwargs: pytest.fail("续跑不生成根目录规则"))

    def change(config):
        config["llm"]["channels"]["hub"]["base_url"] = "https://relay.invalid/v1"
        config["llm"].update(timeout_seconds=7, max_concurrency=3)
    rewrite_yaml(config_files[0], change)
    factories, directors = [], []
    monkeypatch.setattr(runners, "make_model_factory", lambda **kwargs: factories.append(kwargs))

    def stop(director):
        directors.append(director)
        raise runners.TournamentAborted
    monkeypatch.setattr(runners.AIDirector, "run", stop)
    capsys.readouterr()

    assert runners.resume_ai(started_run) == 0
    output = capsys.readouterr().out
    assert f"续跑：{started_run}（团队赛测试模型真实 AI（收费），调用真实服务，可能收费）" in output
    assert "并发上限：3" in output and "操作：回车=下一轮" in output
    assert f'已退出比赛。续跑：python main.py --resume "{started_run}"' in output
    execution = snapshot["players"][0]["execution_model"]
    assert [factory["model"] for factory in factories] == [execution["model"]] * 6 == ["test-model-c"] * 6
    assert all(factory["channel"]["base_url"] == "https://relay.invalid/v1" and factory["timeout"] == 7
               and factory["max_tokens"] == execution["max_tokens"]
               and factory["model_settings"] == execution["model_settings"] for factory in factories)
    director = directors[0]
    assert director.game == GameSettings.from_dict(snapshot["game"])
    assert director.team_of == {pc["name"]: pc["team"] for pc in snapshot["players"]}
    assert director.controller.max_concurrency == 3
    for number, player in enumerate(director.players, 1):
        logged = json.loads(paths.player(number).read_text(encoding="utf-8").splitlines()[0])["content"]
        assert player.messages[0].parts[0].content == logged
        # 系统提示没有再写一遍。
        assert paths.player(number).read_text(encoding="utf-8").count('"task": "system"') == 1


def test_resume_without_directory_takes_the_latest_unfinished_run(started_run, monkeypatch):
    monkeypatch.setattr(runners, "find_unfinished_run", lambda: started_run)
    monkeypatch.setattr(runners, "make_model_factory", lambda **kwargs: None)
    resumed = []
    monkeypatch.setattr(runners, "continue_run", lambda run_dir, *args, **kwargs: resumed.append(run_dir) or 0)
    assert runners.resume_ai() == 0 and resumed == [started_run]


def unstarted(run_dir):
    RunPaths(run_dir).config.unlink()


def one_log_missing(run_dir):
    RunPaths(run_dir).player(4).unlink()


def offline(run_dir):
    config = RunPaths(run_dir).config
    config.write_text(json.dumps({**json.loads(config.read_text(encoding="utf-8")), "run_type": "team-offline"}),
                      encoding="utf-8")


def finished(run_dir):
    publish_speeches(RunPaths(run_dir), SUMMARY_TITLE, {"甲": "总结"}, quiet=True)


def test_resume_of_a_mistyped_directory_says_it_does_not_exist(tmp_path, capsys):
    assert runners.resume_ai(tmp_path / "写错的目录") == 1
    assert f"[无法续跑] 没有这个场次目录：{tmp_path / '写错的目录'}" in capsys.readouterr().out


@pytest.mark.parametrize("damage,message", [
    (unstarted, "[无法续跑] {run} 还没开始，请重新开一场。"),
    (one_log_missing, "[无法续跑] 丁 的任务日志里还没有系统提示：这场还没开始，请重新开一场。"),
    (offline, "[无法续跑] {run} 是离线赛"),
    (finished, "[无法续跑] {run} 已经打完。"),
])
def test_runs_that_cannot_be_resumed_stop_with_the_reason_and_stay_untouched(
        damage, message, started_run, monkeypatch, capsys):
    damage(started_run)
    before = {path: path.read_bytes() for path in started_run.rglob("*") if path.is_file()}
    monkeypatch.setattr(runners, "make_model_factory", lambda **kwargs: None)
    monkeypatch.setattr(runners.AIDirector, "run", lambda self: pytest.fail("续不了的场次不该开始重放"))
    capsys.readouterr()
    assert runners.resume_ai(started_run) == 1
    assert message.format(run=started_run) in capsys.readouterr().out
    assert {path: path.read_bytes() for path in started_run.rglob("*") if path.is_file()} == before


def test_resume_reports_missing_channel_before_creating_clients(started_run, config_files, monkeypatch, capsys):
    rewrite_yaml(config_files[0], lambda config: config["llm"]["channels"].pop("hub"))
    monkeypatch.setattr(runners, "make_model_factory", lambda **kwargs: pytest.fail("配置错误时不应创建客户端"))
    capsys.readouterr()
    assert runners.resume_ai(started_run) == 1
    output = capsys.readouterr().out
    assert "[配置错误]" in output and "llm.channels 里没有本场用到的渠道 'hub'" in output


def test_resume_without_any_unfinished_run_says_so(monkeypatch, capsys):
    monkeypatch.setattr(runners, "find_unfinished_run", lambda: None)
    assert runners.resume_ai() == 1
    assert "[无法续跑] logs/ 下没有没打完的真实 AI 场次。" in capsys.readouterr().out
