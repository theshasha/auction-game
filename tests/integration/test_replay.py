"""对局回放：拿假模型赛的请求原文核对页面里每人每轮收到的信、当前金币和累计得分。"""

import json
import re
import shutil
import sys

import pytest

from auction_game.agent.log import read_records
from auction_game.cli import replay as replay_command
from auction_game.cli.offline_demo import generate_demo
from auction_game.reporting.replay import build_replay, render_replay, round_records
from auction_game.reporting.run_paths import RunPaths
from tests.support.offline import configure_offline_demo


def request_text(entry):
    return [part["content"] for part in entry["messages"][0]["parts"] if part["part_kind"] == "user-prompt"][-1]


def check_against_requests(run_dir, data):
    """页面数据和每条请求原文一致；返回页面里收到的信的总数。"""
    paths = RunPaths(run_dir)
    players = paths.read_config()["players"]
    names = [pc["name"] for pc in players]
    last = len(data["matches"])
    requests = [{where: request_text(entry) for where, entry in round_records(paths, pc["number"], last).items()}
                for pc in players]
    letters = 0
    for index, match in enumerate(data["matches"], 1):
        for key in match["cols"]:
            for i, entry in enumerate(match["rounds"][key]):
                prompt = requests[i][(index, key)]
                received = entry.get("in", [])
                letters += len(received)
                for sender, text in received:
                    assert f"【{names[sender]}】{text.strip()}" in prompt, (names[i], index, key)
                assert sorted(names[sender] for sender, _ in received) == letter_senders(prompt), (names[i], index, key)
        for number, day in enumerate(match["days"], 1):
            if number == 1:
                continue
            prompt = requests[0][(index, f"d{number}")]
            table = dict(re.findall(r"^\| (\S+) \|(?:.*\|)? (\d+) \|$", prompt, re.M))
            assert [int(table[name]) for name in names] == day["coins"], (index, number)
            line = re.search(r"^(?:全员)?累计总分：.*$", prompt, re.M)[0]
            assert [int(re.search(re.escape(name) + r" (\d+) 分", line)[1]) for name in names] == day["score"]
    return letters


def letter_senders(prompt):
    """请求原文里收到的信的写信人，排好序；信紧跟在标题后，每封一段，以【名字】开头。假模型的信只有一段。"""
    found = re.search(r"(?:写的信|写给你的私信)：\n\n(.*)", prompt, re.S)
    senders = []
    for paragraph in found[1].split("\n\n") if found else ():
        head = re.match(r"【(\S+?)】", paragraph)
        if not head:
            break
        senders.append(head[1])
    return sorted(senders)


def hide_markers(run_dir):
    """往快照写一个独特的渠道名、往一条成功记录塞一段 thinking，返回这两段文字。"""
    paths = RunPaths(run_dir)
    config = paths.read_config()
    for pc in config["players"]:
        pc["execution_model"]["provider"] = "渠道名不该出现"
    paths.config.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    log = paths.player(1)
    records, _ = read_records(log)
    records[1]["messages"][1]["parts"].insert(0, {"part_kind": "thinking", "content": "thinking 不该出现"})
    log.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
    return ["渠道名不该出现", "thinking 不该出现"]


def assert_private_text_hidden(run_dir, page, markers):
    system_prompt = json.loads(RunPaths(run_dir).player(1).read_text(encoding="utf-8").splitlines()[0])["content"]
    assert "【你的身份】" in system_prompt
    for text in [*markers, "【你的身份】"]:
        assert text not in page


def test_personal_replay(tmp_path, monkeypatch, config_files, teams_file):
    configure_offline_demo(monkeypatch, config_files, teams_file)
    run_dir = tmp_path / "20261016_120000_ai"
    generate_demo(run_dir, test_model=True)
    markers = hide_markers(run_dir)
    data = build_replay(run_dir)

    assert check_against_requests(run_dir, data) > 0
    matches = data["matches"]
    assert [match["playoff"] for match in matches] == [False, False, False, True]
    assert matches[0]["cols"] == ["open", "d1", "d2", "d3", "sp", "mem"]
    assert all(match["cols"] == ["d1", "d2", "d3", "sp", "mem"] for match in matches[1:])
    assert matches[3]["note"].startswith("加赛：甲、乙并列第一，各 ")
    assert data["teams"] is None and [player["n"] for player in data["players"]] == ["甲", "乙", "丙", "丁"]
    # 最后一轮是观众总结，只有总结正文。
    assert all(set(entry) == {"sum"} for entry in matches[-1]["rounds"]["mem"])
    assert_private_text_hidden(run_dir, render_replay(data), markers)

    # 命令不传目录时选最新的打完了的真实场次：更新的没打完的、离线赛都跳过。
    unfinished = shutil.copytree(run_dir, tmp_path / "20261017_120000_ai")
    paths = RunPaths(unfinished)
    speeches = paths.speeches.read_text(encoding="utf-8")
    paths.speeches.write_text(speeches[:speeches.index("===== 观众总结")], encoding="utf-8")
    shutil.copytree(run_dir, tmp_path / "20261018_120000_offline")
    monkeypatch.setattr("auction_game.reporting.run_paths.LOG_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["replay"])
    replay_command.main()
    assert (run_dir / "replay.html").exists()
    assert not (unfinished / "replay.html").exists()
    with pytest.raises(ValueError, match="没打完"):
        build_replay(unfinished)


def test_team_replay(tmp_path, monkeypatch, config_files, teams_file):
    configure_offline_demo(monkeypatch, config_files, teams_file, team=True)
    run_dir = tmp_path / "20261016_120000_team-offline"
    generate_demo(run_dir, test_model=True, team=True)
    markers = hide_markers(run_dir)
    data = build_replay(run_dir)

    assert check_against_requests(run_dir, data) > 0
    matches = data["matches"]
    assert data["teams"] == ["红队", "蓝队"] and [player["t"] for player in data["players"]] == [0, 0, 0, 1, 1, 1]
    assert matches[0]["cols"] == ["open", "t1", "t2", "t3", "d1", "d2", "d3", "sp", "mem"]
    assert all(match["cols"] == ["t1", "t2", "t3", "d1", "d2", "d3", "sp", "mem"] for match in matches[1:])
    assert matches[-1]["playoff"] and re.fullmatch(r"加赛：两队各 \d+ 分", matches[-1]["note"])
    assert_private_text_hidden(run_dir, render_replay(data), markers)


def test_replay_refuses_non_run_directory(tmp_path):
    with pytest.raises(ValueError, match="不是场次目录"):
        build_replay(tmp_path)
