"""一条生产 AI 完整赛：Agent、控制器、导演、结算和日志均真实执行。"""

import json
from copy import deepcopy
import pytest

from pydantic_ai.messages import ModelMessagesTypeAdapter

from auction_game.reporting import analyze
from auction_game.cli import offline_demo
from auction_game import runners
from auction_game.configuration import load_configuration, load_game_settings
from auction_game.cli.offline_demo import generate_demo
from auction_game.ai import prompts
from auction_game.game.game_rules import GameSettings
from auction_game.reporting.run_paths import RunPaths
from tests.support.factories import task_entries


@pytest.fixture
def demo_configuration(monkeypatch, config_files):
    """完整阶段使用合成参数；生产 game.yaml 仅由独立烟测读取。"""
    def load(**kwargs):
        return load_configuration(*config_files, **kwargs)
    monkeypatch.setattr(offline_demo, "load_configuration", load)
    # 离线比赛禁止读取真实密钥。
    monkeypatch.setattr("auction_game.ai.llm_client._require_env", lambda *args: (_ for _ in ()).throw(
        AssertionError("离线完整赛禁止读取密钥")))
    return load(test_model=True)


def test_complete_ai_tournament(tmp_path, monkeypatch, capsys, demo_configuration):
    game = GameSettings(
        initial_coins=12, daily_salary=6, daily_pool=(3, 3, 3), max_bid=10,
        super_cost=3, super_bounty=5, super_hit_score=1,
        regular_matches=3, max_playoffs=5,
    )
    config, templates = demo_configuration
    config["prompts"] = {"enable_predictions": True, "enable_personas": False}
    names = ["甲", "乙", "丙", "丁"]
    configs = [deepcopy(templates[index % len(templates)]) for index in range(len(names))]
    for name, pc in zip(names, configs):
        pc["name"] = pc["identity_model"]["name"] = name
    monkeypatch.setattr(offline_demo, "load_configuration", lambda **_: deepcopy((config, configs)))
    monkeypatch.setattr(offline_demo, "load_game_settings", lambda: game)
    run_dir = tmp_path / "20261016_120000_offline"
    output_dir, factories = generate_demo(run_dir, test_model=True)
    assert output_dir == run_dir
    paths = RunPaths(run_dir)

    # 阶段：每局首日流局且超级出价落空，次日两笔超级命中；正赛打平后加赛一局。
    matches = analyze.load_match_logs(run_dir)
    assert [payload["match"]["is_playoff"] for payload in matches] == [False] * game.regular_matches + [True]
    totals = dict.fromkeys(names, 0)
    for number, payload in enumerate(matches, 1):
        match = payload["match"]
        assert match["game"] == game.to_dict()
        first, second, *later = match["days"]
        winner = names[{1: 0, 2: 1, 3: 2}.get(number, 0)]
        assert first["is_void"] and first["super_uses"] == [{"player": names[0], "value": 1, "hit": False}]
        assert second["super_uses"] == [{"player": name, "value": 2, "hit": True} for name in names[:2]]
        assert all(day["winner"] == winner for day in [second, *later])
        for name in names:
            totals[name] += match["scores"][name]
        if number == game.regular_matches:
            assert totals[names[0]] == totals[names[1]] == max(totals.values())
        assert paths.match(number, readable=True).exists()
    assert totals[names[0]] > max(totals[name] for name in names[1:])
    expected_calls = 2 + len(matches) * (game.max_days - 1) + 3 * (len(matches) - 1) + 2
    assert all(f.opened == f.closed == f.calls == expected_calls for f in factories)

    snapshot = paths.read_config()
    assert snapshot["run_type"] == "offline" and snapshot["mode"] == "test_model"
    assert snapshot["prompts"] == config["prompts"]
    for pc, saved in zip(configs, snapshot["players"]):
        assert saved["identity_model"] == pc["identity_model"]
        assert saved["execution_model"] == {"key": pc["model_key"],
                                            **{k: pc[k] for k in ("model", "provider", "protocol", "max_tokens",
                                                                  "model_settings", "capabilities")}}
        assert pc["identity_model"]["model"] != pc["model"]
    assert all(f.settings["max_tokens"] == configs[0]["max_tokens"] for f in factories)
    assert all(f.settings["openai_reasoning_effort"] == "medium" for f in factories)
    assert (run_dir / "RULES.md").read_text(encoding="utf-8") == prompts.render_rules(game, names)

    system = json.loads(paths.player(1).read_text(encoding="utf-8").splitlines()[0])
    assert system["task"] == "system"
    assert "【赛前预测】" in system["content"]
    assert "【你的人设】" not in system["content"] and "不要被人设" not in system["content"]
    entries = task_entries(paths.player(1))
    assert len(entries) == expected_calls
    assert [entry["phase"] for entry in entries[:3]] == ["opening_speech", "opening_plan", "bid"]
    assert entries[2]["context"] == "第1局第2天"
    assert not any(entry["context"].endswith("第1天") for entry in entries if entry["task"] == "bid")
    assert not any(entry["phase"] == f"match_{len(matches)}_memory" for entry in entries)
    assert entries[-2]["phase"] == "final_speech" and entries[-1]["phase"] == "final_summary"
    for entry in entries:
        assert entry["status"] == "success" and not entry["request_errors"]
        audit = ModelMessagesTypeAdapter.validate_python(entry["messages"])
        text = "".join(part.content for message in audit if message.kind == "response" for part in message.parts)
        result = entry["result"]
        if entry["task"] in ("bid", "plan"):
            decision = json.dumps({"bid": result["bid"], "super_bid": result["super_bid"]}, ensure_ascii=False)
            assert text == f"<strategy>\n{result['strategy']}\n</strategy>\n<decision>\n{decision}\n</decision>"
        elif entry["task"] == "speech":
            assert text == f"<strategy>\n{result['strategy']}\n</strategy>\n<speech>\n{result['speech']}\n</speech>"
        elif entry["task"] == "memory":
            assert set(result) == {"memory"} and text == result["memory"]
            assert entry["history_reset"] == f"【累计私有记忆，仅自己可见】\n{result['memory']}"
        else:
            assert text == result
        if entry["task"] == "plan":
            match_index = 1 if entry["phase"] == "opening_plan" else int(entry["phase"].split("_")[1]) + 1
            actual = matches[match_index - 1]["match"]["days"][0]["bids"][0]
            assert actual["player"] == names[0] and result["bid"] == actual["bid"]
    markdown = paths.player(1, readable=True).read_text(encoding="utf-8")
    assert markdown.count("## 系统提示") == 1 and markdown.count("【你的身份】") == 1

    stats = analyze.collect_player_stats(matches)
    assert stats["auction_score_matrix"][names[0]] == [payload["match"]["auction_scores"][names[0]]
                                                     for payload in matches]
    monkeypatch.setattr(analyze, "LOG_DIR", tmp_path)
    with pytest.raises(SystemExit, match="没有"):
        analyze.find_latest_ai_run()
    # 统计只读场次快照，当前配置入口损坏也能完整统计。
    monkeypatch.setattr("auction_game.configuration.load_game_settings", lambda: (_ for _ in ()).throw(AssertionError("应读快照")))
    analyze.analyze(run_dir)
    output = capsys.readouterr().out
    assert "离线演示：全员使用假模型，不调用真实服务，不收费。" in output
    assert "提示设置：赛前预测开启，人设关闭" in output
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
    GameSettings(
        initial_coins=7, daily_salary=0, daily_pool=(1,), max_bid=0,
        super_cost=2, super_bounty=3, super_hit_score=2,
        regular_matches=1, max_playoffs=2,
    ),
], ids=["poor", "zero-max-bid"])
def test_offline_economic_edges_finish(game, tmp_path, monkeypatch, demo_configuration):
    monkeypatch.setattr(offline_demo, "load_game_settings", lambda: game)
    run_dir = tmp_path / "edge"
    _, factories = generate_demo(run_dir, test_model=True)
    matches = analyze.load_match_logs(run_dir)
    assert game.regular_matches <= len(matches) <= game.regular_matches + game.max_playoffs
    assert all(payload["match"]["game"] == game.to_dict() for payload in matches)
    assert all(factory.opened == factory.closed == factory.calls for factory in factories)


def test_current_game_configuration_smoke(tmp_path, demo_configuration):
    """当前生产参数仅校验启动、快照与产物，不要求任何特定阶段。"""
    game = load_game_settings()
    run_dir = tmp_path / "current"
    _, factories = generate_demo(run_dir, test_model=True)
    paths = RunPaths(run_dir)
    matches = analyze.load_match_logs(run_dir)
    assert paths.read_config()["game"] == game.to_dict()
    assert game.regular_matches <= len(matches) <= game.regular_matches + game.max_playoffs
    assert all(payload["match"]["game"] == game.to_dict() for payload in matches)
    assert all(factory.opened == factory.closed for factory in factories)


def test_offline_reuses_one_game_and_syncs_once_before_directory(tmp_path, monkeypatch, demo_configuration):
    game = GameSettings(
        initial_coins=7, daily_salary=3, daily_pool=(1,), max_bid=7,
        super_cost=2, super_bounty=3, super_hit_score=2,
        regular_matches=1, max_playoffs=0,
    )
    run_dir = tmp_path / "single-load"
    loads, syncs = [], []
    def load_game():
        loads.append(game)
        assert len(loads) == 1
        return game
    real_sync = offline_demo.sync_root_rules
    def sync(prepared):
        assert not run_dir.exists()
        syncs.append(prepared)
        return real_sync(prepared)
    monkeypatch.setattr(offline_demo, "load_game_settings", load_game)
    monkeypatch.setattr(offline_demo, "sync_root_rules", sync)
    monkeypatch.setattr(runners, "load_game_settings", lambda: pytest.fail("重复读取 game.yaml"))
    monkeypatch.setattr(runners, "sync_root_rules", lambda *_: pytest.fail("重复同步规则"))
    real_tournament = runners.run_tournament
    def tournament(players, actual_game, **kwargs):
        assert actual_game is game
        assert all(player.game is game for player in players)
        return real_tournament(players, actual_game, **kwargs)
    monkeypatch.setattr(runners, "run_tournament", tournament)
    generate_demo(run_dir, test_model=True)
    assert loads == syncs == [game]


@pytest.mark.parametrize("explicit_output", [True, False])
def test_offline_sync_failure_creates_no_run(tmp_path, monkeypatch, demo_configuration, explicit_output):
    from auction_game.configuration import ConfigurationError
    monkeypatch.setattr(offline_demo, "sync_root_rules", lambda *_: (_ for _ in ()).throw(
        ConfigurationError("同步失败")))
    monkeypatch.setattr(offline_demo, "make_run_dir", lambda *_: pytest.fail("同步失败后创建目录"))
    monkeypatch.setattr(offline_demo.DemoFactory, "open", lambda *_: pytest.fail("同步失败后创建模型"))
    output = tmp_path / "failed"
    with pytest.raises(ConfigurationError, match="同步失败"):
        generate_demo(output if explicit_output else None, test_model=True)
    assert not output.exists()


def test_offline_refuses_non_empty_output(tmp_path, demo_configuration):
    output = tmp_path / "existing"
    output.mkdir()
    (output / "README.md").write_text("旧场次", encoding="utf-8")
    with pytest.raises(FileExistsError, match="拒绝覆盖"):
        generate_demo(output, test_model=True)
    assert (output / "README.md").read_text(encoding="utf-8") == "旧场次"


def test_offline_invalid_output_exits_without_retry_gate(tmp_path, monkeypatch, demo_configuration):
    game = GameSettings(
        initial_coins=7, daily_salary=3, daily_pool=(1, 1), max_bid=7,
        super_cost=2, super_bounty=3, super_hit_score=2,
        regular_matches=1, max_playoffs=0,
    )
    monkeypatch.setattr(offline_demo, "load_game_settings", lambda: game)
    real_stream = offline_demo.DemoFactory.stream
    attempts = []
    async def invalid_bid(self, messages, info):
        task = offline_demo._current_task.get().task
        if self.index == 0 and task in ("plan", "bid"):
            attempts.append(task)
            yield '<strategy>\n故意非法出价\n</strategy>\n<decision>\n{"bid": -1, "super_bid": null}\n</decision>'
        else:
            async for part in real_stream(self, messages, info):
                yield part
    monkeypatch.setattr(offline_demo.DemoFactory, "stream", invalid_bid)
    wait = offline_demo.Controller.wait_for_turn
    def continuous_only(self, label, **kwargs):
        assert self._mode == "continuous", "离线失败进入了无人交互闸门"
        assert not kwargs.get("require_confirmation"), "离线失败进入了交互重试"
        return wait(self, label, **kwargs)
    monkeypatch.setattr(offline_demo.Controller, "wait_for_turn", continuous_only)
    run_dir = tmp_path / "invalid"
    with pytest.raises(RuntimeError, match="离线任务失败：第一局策略规划"):
        generate_demo(run_dir, test_model=True)
    assert attempts == ["plan", "plan"]  # 共享输出纠错保留，整轮不自动补跑。
    paths = RunPaths(run_dir)
    entries = task_entries(paths.player(1))
    failed = entries[-1]
    assert failed["status"] == "failed" and failed["error"]["type"] == "OutputFailedError"
    assert failed["phase"] == "opening_plan" and failed["reruns"] == 0
    replies = [part["content"] for message in failed["messages"] if message["kind"] == "response"
               for part in message["parts"]]
    assert replies == ['<strategy>\n故意非法出价\n</strategy>\n<decision>\n{"bid": -1, "super_bid": null}\n</decision>'] * 2


def test_offline_request_failure_is_saved_before_exit(tmp_path, monkeypatch, demo_configuration):
    from pydantic_ai.exceptions import ModelAPIError

    attempts = []
    async def unavailable(self, messages, info):
        attempts.append(self.name)
        raise ModelAPIError("offline", "离线故障测试")
        yield  # FunctionModel 的流式接口。

    monkeypatch.setattr(offline_demo.DemoFactory, "stream", unavailable)
    monkeypatch.setattr(offline_demo.Controller, "start_stdin_reader", lambda *_: pytest.fail("启动键盘读取"))
    real_wait = offline_demo.Controller.wait_for_turn
    def no_retry(self, label, **kwargs):
        assert not kwargs.get("require_confirmation"), "进入交互重试闸门"
        return real_wait(self, label, **kwargs)
    monkeypatch.setattr(offline_demo.Controller, "wait_for_turn", no_retry)
    run_dir = tmp_path / "request-failure"
    with pytest.raises(RuntimeError, match="离线任务失败：开局发言"):
        generate_demo(run_dir, test_model=True)

    _, configs = demo_configuration
    assert sorted(attempts) == sorted(pc["name"] for pc in configs)
    paths = RunPaths(run_dir)
    for number in range(1, len(configs) + 1):
        entries = task_entries(paths.player(number))
        assert len(entries) == 1
        assert entries[0]["status"] == "failed"
        assert "离线故障测试" in entries[0]["error"]["message"]
