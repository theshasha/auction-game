"""中断后续跑：离线假模型赛在各种位置被强关，接着跑完的场次和一口气跑完的相同。"""

from copy import deepcopy
from dataclasses import replace
from itertools import count
import json
import shutil

import pytest

from auction_game import runners
from auction_game.cli import offline_demo
from auction_game.cli.offline_demo import generate_demo
from auction_game.configuration import load_configuration
from auction_game.flow import director as director_module
from auction_game.game.settings import GameSettings
from auction_game.reporting.run_output import SUMMARY_TITLE, is_finished
from auction_game.reporting.run_paths import RunPaths
from tests.support.resume import (Histories, Killed, KillSwitch, assert_same_run, complete_records, resume_demo,
                                  start_demo)

# 正赛打平后有一局加赛，各种轮次都会出现。
GAME = GameSettings(
    initial_coins=12, daily_salary=6, daily_pool=(3, 3, 3), max_bid=10,
    super_cost=3, super_bounty=5, super_hit_score=1,
    regular_matches=3, max_playoffs=5,
)
# 一局正赛加一局加赛，各种轮次各出现一次。
SHORT_GAME = replace(GAME, regular_matches=1)


def configure_demo(monkeypatch, config_files, teams_file, team, game=GAME):
    """离线假模型赛读合成配置：个人赛 4 人，团队赛 6 人；并发为 1，中断位置才确定。返回 generate_demo 的参数。"""
    config, configs = load_configuration(*config_files, test_model=True, team=team, teams_path=teams_file)
    config["llm"]["max_concurrency"] = 1
    if not team:
        configs = [replace(pc, profile=replace(pc.profile, name=name), identity=replace(pc.identity, name=name))
                   for name, pc in zip("甲乙丙丁", configs * 2)]
    monkeypatch.setattr(offline_demo, "load_configuration", lambda **_: deepcopy((config, configs)))
    monkeypatch.setattr(offline_demo, "load_game_settings", lambda **_: game)
    return {"test_model": True, "team": team}


@pytest.fixture(params=[False, True], ids=["individual", "team"])
def demo(request, monkeypatch, config_files, teams_file):
    return configure_demo(monkeypatch, config_files, teams_file, request.param)


@pytest.fixture
def team_demo(monkeypatch, config_files, teams_file):
    return configure_demo(monkeypatch, config_files, teams_file, True, SHORT_GAME)


def raw_files(run_dir):
    return {path.relative_to(run_dir).as_posix(): path.read_bytes()
            for path in sorted(run_dir.rglob("*")) if path.is_file()}


def test_run_killed_every_five_tasks_resumes_to_the_uninterrupted_result(demo, tmp_path, monkeypatch):
    """5 和每轮人数互质，轮前和轮中的各种位置都会被中断到，续跑过的场次也会再被中断。"""
    histories = Histories()
    baseline = tmp_path / "baseline"
    start_demo(monkeypatch, baseline, histories=histories, **demo)
    histories.verifying = True
    run_dir = tmp_path / "run"
    with pytest.raises(Killed):
        start_demo(monkeypatch, run_dir, KillSwitch(5), histories, **demo)

    # 续跑的参数、阵容和模型都取自场次快照：当前的比赛配置读不了也不影响。
    def unreadable(*args, **kwargs):
        raise AssertionError("续跑不读当前的比赛配置")
    monkeypatch.setattr(runners, "load_configuration", unreadable)
    monkeypatch.setattr(runners, "load_game_settings", unreadable)

    kills = 0
    while True:
        try:
            status = resume_demo(run_dir, KillSwitch(5), histories)
            break
        except Killed:
            kills += 1
    assert status == 0 and kills > 10
    assert_same_run(run_dir, baseline)


@pytest.mark.parametrize("function,fatal_call", [("publish_speeches", 2), ("publish_match", 2), ("record_letters", 4)])
def test_kill_between_commit_and_public_files_is_repaired(function, fatal_call, team_demo, tmp_path, monkeypatch):
    """一轮的任务记录都写好了，公开文件还没写就被关掉：续跑按任务日志把缺的补上。"""
    baseline = tmp_path / "baseline"
    generate_demo(baseline, **team_demo)
    run_dir = tmp_path / "run"
    real, calls = getattr(director_module, function), count(1)

    def dying(*args, **kwargs):
        if next(calls) == fatal_call:
            raise Killed
        return real(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(director_module, function, dying)
        with pytest.raises(Killed):
            generate_demo(run_dir, **team_demo)
    assert resume_demo(run_dir) == 0
    assert_same_run(run_dir, baseline)


def test_half_written_record_and_missing_markdown_section_are_repaired(team_demo, tmp_path, monkeypatch):
    baseline = tmp_path / "baseline"
    generate_demo(baseline, **team_demo)
    run_dir = tmp_path / "run"
    with pytest.raises(Killed):
        start_demo(monkeypatch, run_dir, KillSwitch(20), **team_demo)
    paths = RunPaths(run_dir)
    # 强关停在一行写到一半：半行不是完整的 JSON。
    with paths.player(1).open("ab") as file:
        file.write(b'{"task_id": "task-0005", "title": "\xe7\xac\xac 1 \xe5\xb1\x80')
    # 强关停在 JSONL 已写、Markdown 没写之间：可读日志少最后一节。
    readable = paths.player(2, readable=True)
    text = readable.read_bytes()
    readable.write_bytes(text[:text.rindex(b"## ")])

    assert resume_demo(run_dir) == 0
    assert_same_run(run_dir, baseline)


@pytest.mark.parametrize("damage", ["input", "order"])
def test_log_that_differs_from_the_replay_stops_before_any_request_and_changes_nothing(
        damage, team_demo, tmp_path, monkeypatch, capsys):
    """两次运行之间改过提示词（输入不同）或流程（轮次顺序不同），这场就续不上，原有文件不动。"""
    run_dir = tmp_path / "run"
    with pytest.raises(Killed):
        start_demo(monkeypatch, run_dir, KillSwitch(20), **team_demo)
    path = RunPaths(run_dir).player(2)
    records = complete_records(path)
    if damage == "input":
        prompt = next(part for message in records[2]["messages"] for part in message["parts"]
                      if part["part_kind"] == "user-prompt")
        prompt["content"] += "\n旧版提示里的一句话"
    else:
        del records[2]
    path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
    before = raw_files(run_dir)
    capsys.readouterr()

    assert resume_demo(run_dir, KillSwitch(0)) == 1
    assert "[无法续跑] 乙" in capsys.readouterr().out
    assert raw_files(run_dir) == before


def test_final_round_recorded_but_unpublished_is_finished_without_requests(team_demo, tmp_path, capsys):
    """暂停后最后一轮全员成功、还没确认就关掉：任务日志是全的，观众总结和最终结果还没出。"""
    run_dir = tmp_path / "run"
    generate_demo(run_dir, **team_demo)
    baseline = shutil.copytree(run_dir, tmp_path / "baseline")
    paths = RunPaths(run_dir)
    assert is_finished(paths)
    speeches = paths.speeches.read_bytes()
    paths.speeches.write_bytes(speeches[:speeches.index(f"===== {SUMMARY_TITLE}".encode())])
    assert not is_finished(paths)
    capsys.readouterr()

    assert resume_demo(run_dir, KillSwitch(0)) == 0
    output = capsys.readouterr().out
    assert "===== 最终排名 =====" in output and "获胜阵营" in output
    assert "已恢复到" not in output and SUMMARY_TITLE not in output
    assert is_finished(paths)
    assert_same_run(run_dir, baseline)


def test_resume_reports_position_and_stays_quiet_about_replayed_rounds(team_demo, tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    with pytest.raises(Killed):
        start_demo(monkeypatch, run_dir, KillSwitch(20), **team_demo)
    capsys.readouterr()
    with pytest.raises(Killed):
        resume_demo(run_dir, KillSwitch(0))
    output = capsys.readouterr().out
    assert "已恢复到：第 1 局开局讨论第 3 轮（6 人中 2 人已完成）" in output
    assert "===== 开局发言 =====" not in output
