"""team_report 命令的输出；合成对局见 tests/support/analyze_runs.py，计算口径见 tests/reporting/test_team_metrics.py。"""

import pytest

from auction_game.cli.team_report import main, report
from tests.support.analyze_runs import make_matches, make_team_report_match, make_team_void_match, write_matches
from tests.support.factories import TEAM_OF


def team_run(path, matches, team_of=TEAM_OF):
    write_matches(path, [{"match": match} for match in matches], run_type="team-offline", team_of=team_of)
    return path


def section(output, title):
    return output.split(f"\n{title}\n", 1)[1].split("\n\n", 1)[0].splitlines()


def test_single_run_prints_one_column(tmp_path, capsys):
    report([team_run(tmp_path / "run", [make_team_report_match()])])
    output = capsys.readouterr().out
    assert section(output, "概况") == [
        "| 指标 | run |",
        "| --- | --- |",
        "| 阵营 | 红队:蓝队 |",
        "| 已结束的局数 | 1（加赛 0） |",
        "| 天数 | 2 |",
    ]
    for line in ["| 总分 | 3:8 |",
                 "| 第 2 天得分 | 2:8 |",
                 "| 第 1 天平均正常出价 | 2.67:3.00 |",
                 "| 使用（人天） | 1/6（17%）:4/6（67%） |",
                 "| 使用率（两队合并） | 5/12（42%） |",
                 "| 拍卖分：只去掉前队的超级出价 | 1:2 |",
                 "| 超级出价把当天从对方换成本队（天） | 0:1 |",
                 "| 打余额价（占全部超级出价） | 0/1:4/4 |",
                 "| 事后最佳固定狙价（对方当天有人出的天数） | 2、4（2/2）:1、7（2/2） |",
                 "| 贴上限（人天） | 4/6（67%）:0/6（0%） |",
                 "| 第 2 天起的平均余额（出价前） | 5.67:7.33 |",
                 "|   明细 | 无:无 |",
                 "| 第 1 局第 1 天 | 0、1、7:2、3、4（重合 0） |",
                 "| 没有流局 | — |"]:
        assert line + "\n" in output


def test_runs_with_same_teams_add_up(tmp_path, capsys):
    runs = [team_run(tmp_path / "a", [make_team_report_match()]),
            team_run(tmp_path / "b", [make_team_report_match(), make_team_void_match() | {"match_index": 2}])]
    report(runs)
    output = capsys.readouterr().out
    assert section(output, "概况")[0] == "| 指标 | a | b | 合计 |"
    for line in ["| 已结束的局数 | 1（加赛 0） | 2（加赛 0） | 3（加赛 0） |",
                 "| 总分 | 3:8 | 5:10 | 8:18 |",
                 # 比率的合计是分子分母各自相加。
                 "| 贴上限（人天） | 4/6（67%）:0/6（0%） | 4/9（44%）:0/9（0%） | 8/15（53%）:0/15（0%） |",
                 "| 只留一人重算后能拿下的天数 | 0:0 | 1:1 | 1:1 |",
                 "|   明细 | 无:无 | 第 2 局第 1 天 1 分:第 2 局第 1 天 1 分 | — |",
                 "| 第 2 局第 1 天 | — | 1、1、5:0、0、3（重合 0） | — |"]:
        assert line + "\n" in output
    assert section(output, "流局日的正常出价")[2] == "| 第 2 局第 1 天 | — | 1、1、5:0、0、3 | — |"
    assert "总分差（前队减后队）和按天重抽样的 95% 区间" in output


def test_runs_with_different_teams_have_no_total(tmp_path, capsys):
    other_teams = {name: "甲队" if team == "红队" else "乙队" for name, team in TEAM_OF.items()}
    report([team_run(tmp_path / "a", [make_team_report_match()]),
            team_run(tmp_path / "b", [make_team_report_match()], other_teams)])
    output = capsys.readouterr().out
    assert "各场阵营名不同，不给合计\n" in output
    assert section(output, "概况")[:3] == ["| 指标 | a | b |", "| --- | --- | --- |", "| 阵营 | 红队:蓝队 | 甲队:乙队 |"]


def test_cli_rejects_individual_run(tmp_path, monkeypatch):
    write_matches(tmp_path / "solo", make_matches())
    monkeypatch.setattr("sys.argv", ["team_report", str(tmp_path / "solo")])
    with pytest.raises(SystemExit, match="不是团队赛"):
        main()


def test_cli_without_run_reports_latest_real_team_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("auction_game.reporting.analyze.LOG_DIR", tmp_path)
    matches = [{"match": make_team_report_match()}]
    write_matches(tmp_path / "20261016_110000_team", matches, run_type="team", team_of=TEAM_OF)
    write_matches(tmp_path / "20261016_120000_team-test", matches, run_type="team-test", team_of=TEAM_OF)
    write_matches(tmp_path / "20261016_130000_ai", make_matches(), run_type="ai")
    write_matches(tmp_path / "20261016_130000_team-offline", matches, run_type="team-offline", team_of=TEAM_OF)
    monkeypatch.setattr("sys.argv", ["team_report"])
    main()
    assert section(capsys.readouterr().out, "概况")[0] == "| 指标 | 20261016_120000_team-test |"


def test_cli_without_run_rejects_only_individual_runs(tmp_path, monkeypatch):
    monkeypatch.setattr("auction_game.reporting.analyze.LOG_DIR", tmp_path)
    write_matches(tmp_path / "20261016_130000_ai", make_matches(), run_type="ai")
    monkeypatch.setattr("sys.argv", ["team_report"])
    with pytest.raises(SystemExit, match="没有.*（team、team-test）"):
        main()
