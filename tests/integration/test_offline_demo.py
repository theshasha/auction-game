"""一条生产 AI 完整赛：Agent、控制器、导演、结算和日志均真实执行。"""

import json
from copy import deepcopy
from dataclasses import replace
import re
import pytest

from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessagesTypeAdapter

from auction_game.reporting import analyze
from auction_game.cli import analyze as analyze_command, offline_demo
from auction_game.configuration import load_configuration, load_game_settings
from auction_game.cli.offline_demo import generate_demo
from auction_game.agent import prompts
from auction_game.game.lineup import Lineup
from auction_game.game.settings import GameSettings
from auction_game.reporting.run_paths import RunPaths
from tests.support.factories import TEAM_OF, task_entries, text_reply


def super_uses(day):
    """局日志里一天的超级出价：(玩家, 超级出价, 是否命中)。"""
    return [(result["player"], result["super_bid"], result["super_hit"])
            for result in day["players"] if result["super_bid"] is not None]


@pytest.fixture
def demo_configuration(monkeypatch, config_files):
    """完整阶段使用合成参数；生产 game.yaml 仅由独立烟测读取。"""
    def load(**kwargs):
        return load_configuration(*config_files, **kwargs)
    monkeypatch.setattr(offline_demo, "load_configuration", load)
    return load(test_model=True)


def test_complete_ai_tournament(tmp_path, monkeypatch, capsys, demo_configuration):
    game = GameSettings(
        initial_coins=12, daily_salary=6, daily_pool=(3, 3, 3), max_bid=10,
        super_cost=3, super_bounty=5, super_hit_score=1,
        regular_matches=3, max_playoffs=5,
    )
    config, templates = demo_configuration
    config["prompts"] = {"enable_predictions": True, "enable_personas": False, "enable_writing_style": True}
    names = ["甲", "乙", "丙", "丁"]
    configs = []
    for index, name in enumerate(names):
        pc = templates[index % len(templates)]
        configs.append(replace(pc, profile=replace(pc.profile, name=name), identity=replace(pc.identity, name=name)))
    monkeypatch.setattr(offline_demo, "load_configuration", lambda **_: deepcopy((config, configs)))
    monkeypatch.setattr(offline_demo, "load_game_settings", lambda **_: game)
    run_dir = tmp_path / "20261016_120000_offline"
    output_dir, factories = generate_demo(run_dir, test_model=True)
    assert output_dir == run_dir
    paths = RunPaths(run_dir)
    report = paths.matches_report.read_text(encoding="utf-8")

    # 阶段：每局首日流局且超级出价落空，次日两笔超级出价命中；正赛打平后加赛一局。
    matches = analyze.load_match_logs(run_dir)
    assert [payload["match"]["is_playoff"] for payload in matches] == [False] * game.regular_matches + [True]
    totals = dict.fromkeys(names, 0)
    for number, payload in enumerate(matches, 1):
        match = payload["match"]
        assert match["game"] == game.to_dict()
        first, second, *later = match["days"]
        winner = names[{1: 0, 2: 1, 3: 2}.get(number, 0)]
        assert first["winner"] is None and super_uses(first) == [(names[0], 1, False)]
        assert super_uses(second) == [(name, 2, True) for name in names[:2]]
        assert all(day["winner"] == winner for day in [second, *later])
        for name in names:
            totals[name] += match["scores"][name]
        if number == game.regular_matches:
            assert totals[names[0]] == totals[names[1]] == max(totals.values())
        assert f"## 第 {number} 局（{'加赛' if match['is_playoff'] else '正赛'}）" in report
    assert totals[names[0]] > max(totals[name] for name in names[1:])
    expected_calls = 2 + len(matches) * (game.max_days - 1) + 3 * (len(matches) - 1) + 2
    assert all(f.opened == f.closed == f.calls == expected_calls for f in factories)

    snapshot = paths.read_config()
    assert snapshot["run_type"] == "offline" and snapshot["mode"] == "test_model"
    assert snapshot["prompts"] == config["prompts"]
    capability_fields = ("max_tokens", "model_settings", "capabilities")
    for pc, saved in zip(configs, snapshot["players"]):
        identity, execution = pc.identity, pc.execution
        # 合成目录没写 reasoning_effort，快照里也没有这一项。
        assert saved["identity_model"] == {
            "key": identity.key, "name": identity.name, "model": identity.model, "provider": identity.provider,
            "protocol": identity.protocol, "models_dev": identity.models_dev,
            **{field: getattr(identity, field) for field in capability_fields}}
        assert saved["execution_model"] == {
            "key": execution.key, "model": execution.model, "provider": execution.provider,
            "protocol": execution.protocol, **{field: getattr(execution, field) for field in capability_fields}}
        assert identity.model != execution.model
    assert all(f.settings["max_tokens"] == configs[0].execution.max_tokens for f in factories)
    assert all(f.settings["openai_reasoning_effort"] == "medium" for f in factories)
    assert (run_dir / "RULES.md").read_text(encoding="utf-8") == prompts.render_rules(
        game, Lineup(tuple(pc.profile for pc in configs)))

    system = json.loads(paths.player(1).read_text(encoding="utf-8").splitlines()[0])
    assert system["task"] == "system"
    assert "【赛前预测】" in system["content"]
    assert "【你的人设】" not in system["content"] and "不要被人设" not in system["content"]
    entries = task_entries(paths.player(1))
    assert len(entries) == expected_calls
    assert [entry["title"] for entry in entries[:3]] == ["开局发言", "第 1 局策略规划（含首日出价）", "第 1 局第 2 天出价"]
    assert [entry["context"] for entry in entries[:3]] == ["开局发言", "第 1 局策略规划（含首日出价）", "第 1 局第 2 天"]
    assert not any(entry["context"].endswith("第 1 天") for entry in entries if entry["task"] == "bid")
    assert [entry["title"] for entry in entries if entry["task"] == "plan"] == [
        f"第 {index} 局策略规划（含首日出价）" for index in range(1, len(matches) + 1)]
    assert [entry["title"] for entry in entries if entry["task"] == "memory"] == [
        f"第 {index} 局累计私有记忆" for index in range(1, len(matches))]
    assert entries[-2]["title"] == "最终感言" and entries[-1]["title"] == "观众总结（仅观众可见）"
    for entry in entries:
        assert entry["status"] == "success" and not entry["request_errors"]
        audit = ModelMessagesTypeAdapter.validate_python(entry["messages"])
        text = "".join(part.content for message in audit if message.kind == "response" for part in message.parts)
        result = entry["result"]
        assert text == text_reply(result)
        if entry["task"] == "memory":
            assert set(result) == {"memory"}
            assert entry["history_reset"] == f"【累计私有记忆，仅自己可见】\n{result['memory']}"
        if entry["task"] == "plan":
            match_index = int(re.match(r"第 (\d+) 局", entry["title"])[1])
            actual = matches[match_index - 1]["match"]["days"][0]["players"][0]
            assert actual["player"] == names[0] and result["bid"] == actual["bid"]
    markdown = paths.player(1, readable=True).read_text(encoding="utf-8")
    assert markdown.count("## 系统提示") == 1 and markdown.count("【你的身份】") == 1

    # 假模型每次能写信都写给下一位；没人写信的轮次不进 letters.md。
    letter_titles = ["开局发言"]
    for number in range(1, len(matches) + 1):
        letter_titles.append(f"第 {number} 局策略规划（含首日出价）")
        letter_titles += [f"第 {number} 局第 {day} 天出价" for day in range(2, game.max_days + 1)]
        if number < len(matches):
            letter_titles.append(f"第 {number} 局局末发言")
    sections = paths.letters.read_text(encoding="utf-8").split("\n## ")[1:]
    assert [section.split("\n", 1)[0] for section in sections] == letter_titles
    ring = [(sender, names[(number + 1) % len(names)]) for number, sender in enumerate(names)]
    assert all(re.findall(r"【(\S+) → (\S+)】", section) == ring for section in sections)
    readme = (run_dir / "README.md").read_text(encoding="utf-8")
    assert "[私信](letters.md)" in readme
    # README 的链接都指向场次里实际写出的文件。
    assert all((run_dir / link).exists() for link in re.findall(r"\]\(([^)]+)\)", readme))
    # 甲写给乙：开局发言的信在乙规划时收到，最后一天出价的信在乙发表最终感言时收到。
    first_letter = entries[0]["result"]["letters"][0]["text"]
    last_bid = [entry for entry in entries if entry["task"] == "bid"][-1]
    assert last_bid["context"] == f"第 {len(matches)} 局第 {game.max_days} 天"
    last_letter = last_bid["result"]["letters"][0]["text"]
    receiver = {entry["title"]: entry for entry in task_entries(paths.player(2))}
    for title, written, letter in (("第 1 局策略规划（含首日出价）", "在开局发言时", first_letter),
                                   ("最终感言", "在本局最后一天出价时", last_letter)):
        prompt = [part["content"] for message in receiver[title]["messages"]
                  for part in message["parts"] if part["part_kind"] == "user-prompt"][-1]
        assert f"以下是其他玩家{written}写给你的私信：\n\n【甲】{letter}\n\n" in prompt

    stats = analyze.collect_player_stats(matches)
    assert stats["auction_score_matrix"][names[0]] == [payload["match"]["auction_scores"][names[0]]
                                                     for payload in matches]
    monkeypatch.setattr(analyze, "LOG_DIR", tmp_path)
    with pytest.raises(SystemExit, match="没有"):
        analyze.find_latest_ai_run()
    # 统计只读场次快照，当前配置入口损坏也能完整统计。
    monkeypatch.setattr("auction_game.configuration.load_game_settings", lambda: (_ for _ in ()).throw(AssertionError("应读快照")))
    analyze_command.analyze(run_dir)
    output = capsys.readouterr().out
    assert "离线演示：全员使用假模型，不调用真实服务，不收费。" in output
    assert "提示设置：赛前预测开启，人设关闭，写作要求开启" in output
    assert f"本场日志目录：{run_dir}" in output
    assert "操作：" not in output and "切换到连续模式" not in output
    assert "测试模型：" not in output and "[预算提醒]" not in output
    assert f"共进行 {len(matches)} 局（正赛 {game.regular_matches} 局 + 加赛 1 局）" in output
    assert f"总拍卖天数：{len(matches) * game.max_days}" in output
    assert "离线假模型结果：实际执行为假模型，无真实模型调用" in output


@pytest.mark.parametrize("game", [
    GameSettings(
        initial_coins=1, daily_salary=0, daily_pool=(1, 1), max_bid=7,
        super_cost=2, super_bounty=3, super_hit_score=2,
        regular_matches=1, max_playoffs=2,
    ),
], ids=["poor"])
def test_offline_economic_edges_finish(game, tmp_path, monkeypatch, demo_configuration):
    monkeypatch.setattr(offline_demo, "load_game_settings", lambda **_: game)
    run_dir = tmp_path / "edge"
    _, factories = generate_demo(run_dir, test_model=True)
    matches = analyze.load_match_logs(run_dir)
    assert game.regular_matches <= len(matches) <= game.regular_matches + game.max_playoffs
    assert all(payload["match"]["game"] == game.to_dict() for payload in matches)
    assert all(factory.opened == factory.closed == factory.calls for factory in factories)


def test_current_game_configuration_smoke(tmp_path, demo_configuration):
    """当前测试模式参数仅校验启动、快照与产物，不要求任何特定阶段。"""
    game = load_game_settings(test_model=True)
    run_dir = tmp_path / "current"
    _, factories = generate_demo(run_dir, test_model=True)
    paths = RunPaths(run_dir)
    matches = analyze.load_match_logs(run_dir)
    assert paths.read_config()["game"] == game.to_dict()
    assert game.regular_matches <= len(matches) <= game.regular_matches + game.max_playoffs
    assert all(payload["match"]["game"] == game.to_dict() for payload in matches)
    assert all(factory.opened == factory.closed for factory in factories)


@pytest.fixture
def team_demo_configuration(monkeypatch, config_files, teams_file):
    """团队假模型赛读合成的 teams.yaml，阵营和 TEAM_OF 相同：红队甲乙丙，蓝队丁戊己。"""
    monkeypatch.setattr(offline_demo, "load_configuration",
                        lambda **kwargs: load_configuration(*config_files, teams_path=teams_file, **kwargs))


def assert_team_phases(matches, regular_matches):
    """每局第 2 天都有只狙到队友价位的落空和跨阵营命中；正赛打完两队同分，加赛分出胜负。"""
    totals = dict.fromkeys(TEAM_OF.values(), 0)
    for number, payload in enumerate(matches, 1):
        match = payload["match"]
        bidders = {}
        for result in match["days"][1]["players"]:
            bidders.setdefault(result["bid"], set()).add(TEAM_OF[result["player"]])
        supers = [(TEAM_OF[player], bidders.get(value, set()), hit) for player, value, hit in super_uses(match["days"][1])]
        assert any(not hit and teams == {team} for team, teams, hit in supers)
        assert any(hit and teams - {team} for team, teams, hit in supers)
        for name, score in match["scores"].items():
            totals[TEAM_OF[name]] += score
        if number == regular_matches:
            assert len(set(totals.values())) == 1
    assert matches[-1]["match"]["is_playoff"] and len(set(totals.values())) == 2


def test_complete_team_tournament(tmp_path, monkeypatch, capsys, team_demo_configuration):
    game = GameSettings(
        initial_coins=12, daily_salary=6, daily_pool=(3, 3, 3), max_bid=10,
        super_cost=3, super_bounty=5, super_hit_score=1,
        regular_matches=3, max_playoffs=5,
    )
    monkeypatch.setattr(offline_demo, "load_game_settings", lambda **_: game)
    run_dir = tmp_path / "20261016_120000_team-offline"
    _, factories = generate_demo(run_dir, test_model=True, team=True)
    matches = analyze.load_match_logs(run_dir)
    assert [payload["match"]["is_playoff"] for payload in matches] == [False] * game.regular_matches + [True]
    assert_team_phases(matches, game.regular_matches)
    # 第 2 天起由本局赢的一队第 2 人独得：正赛红队乙、蓝队戊轮流，第 3 局是奇数局数的最后一局正赛，谁都不赢。
    assert [[day["winner"] for day in payload["match"]["days"]] for payload in matches] == [
        [None, "乙", "乙"], [None, "戊", "戊"], [None, None, None], [None, "乙", "乙"]]
    # 开局发言、3 轮开局讨论和第 1 局规划，之后每局第 2 天起的出价，局末发言、整理记忆、3 轮开局讨论和规划共 6 轮，
    # 最后是感言和总结。
    expected_calls = 5 + len(matches) * (game.max_days - 1) + 6 * (len(matches) - 1) + 2
    assert all(f.opened == f.closed == f.calls == expected_calls for f in factories)

    paths = RunPaths(run_dir)
    snapshot = paths.read_config()
    assert snapshot["run_type"] == "team-offline"
    sections = paths.letters.read_text(encoding="utf-8").split("\n## ")[1:]
    assert [section.split("\n", 1)[0] for section in sections[:5]] == [
        "开局发言", "第 1 局开局讨论第 1 轮", "第 1 局开局讨论第 2 轮", "第 1 局开局讨论第 3 轮",
        "第 1 局策略规划（含首日出价）"]
    assert all(re.findall(r"【(\S+) → (\S+)】", section) == list(TEAM_OF.items()) for section in sections)
    # 甲写到信件区的信，队友下一次行动时看到，对方看不到。
    letter = task_entries(paths.player(1))[0]["result"]["letters"][0]["text"]
    discussion = {number: next(entry for entry in task_entries(paths.player(number)) if entry["task"] == "discussion")
                  for number in (2, 3, 4)}
    prompts = {number: [part["content"] for message in entry["messages"] for part in message["parts"]
                        if part["part_kind"] == "user-prompt"][-1] for number, entry in discussion.items()}
    assert f"以下是队友在开局发言时写的信：\n\n【甲】{letter}" in prompts[2] and f"【甲】{letter}" in prompts[3]
    assert letter not in prompts[4]
    assert {pc["name"]: pc["team"] for pc in snapshot["players"]} == TEAM_OF
    assert "红队有 甲、乙、丙，蓝队有 丁、戊、己" in (run_dir / "RULES.md").read_text(encoding="utf-8")
    readme = (run_dir / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# 拍卖团队赛") and "### 红队" in readme and "### 蓝队" in readme
    assert "[队内信件区](letters.md)" in readme
    assert all((run_dir / link).exists() for link in re.findall(r"\]\(([^)]+)\)", readme))
    analyze_command.analyze(run_dir)
    output = capsys.readouterr().out
    assert "获胜阵营: 红队" in output
    assert "运行类型：团队赛离线模拟" in output and "离线假模型结果：实际执行为假模型，无真实模型调用" in output


@pytest.mark.parametrize("test_model", [False, True], ids=["production", "test-model-overrides"])
def test_current_game_configuration_shows_team_phases(tmp_path, team_demo_configuration, test_model):
    """当前 game.yaml 的生产参数和测试覆盖参数下，团队假模型赛都出现脚本设计的各个阶段。"""
    game = load_game_settings(test_model=test_model)
    run_dir = tmp_path / "current"
    generate_demo(run_dir, test_model=test_model, team=True)
    matches = analyze.load_match_logs(run_dir)
    assert all(payload["match"]["game"] == game.to_dict() for payload in matches)
    assert_team_phases(matches, game.regular_matches)


def test_offline_refuses_non_empty_output(tmp_path, demo_configuration):
    output = tmp_path / "existing"
    output.mkdir()
    (output / "README.md").write_text("旧场次", encoding="utf-8")
    with pytest.raises(FileExistsError, match="拒绝覆盖"):
        generate_demo(output, test_model=True)
    assert (output / "README.md").read_text(encoding="utf-8") == "旧场次"


INVALID_SPEECH = "<strategy>\n故意缺少发言\n</strategy>"


@pytest.mark.parametrize("fault", ["output", "request"])
def test_offline_failure_is_saved_and_exits_without_confirmation(fault, tmp_path, monkeypatch, demo_configuration):
    real_stream = offline_demo.DemoFactory.stream

    async def faulty(self, messages, info):
        if self.index != 0:
            async for part in real_stream(self, messages, info):
                yield part
        elif fault == "request":
            raise ModelAPIError("offline", "离线故障测试")
        else:
            yield INVALID_SPEECH

    monkeypatch.setattr(offline_demo.DemoFactory, "stream", faulty)
    monkeypatch.setattr(offline_demo.Controller, "start_stdin_reader", lambda *_: pytest.fail("启动键盘读取"))
    run_dir = tmp_path / "failure"
    with pytest.raises(RuntimeError, match="离线任务失败：开局发言"):
        generate_demo(run_dir, test_model=True)

    entries = task_entries(RunPaths(run_dir).player(1))
    assert len(entries) == 1 and entries[0]["status"] == "failed"
    if fault == "request":
        assert "离线故障测试" in entries[0]["error"]["message"]
    else:
        replies = [part["content"] for message in entries[0]["messages"] if message["kind"] == "response"
                   for part in message["parts"]]
        assert replies == [INVALID_SPEECH] * 2
