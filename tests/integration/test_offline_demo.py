"""一条生产 AI 完整赛：Agent、控制器、导演、结算、审计与回放均真实执行。"""

import json
import re
import pytest

from pydantic_ai.messages import ModelMessagesTypeAdapter

from auction_game.reporting import analyze
from auction_game.cli import offline_demo
from auction_game import runners
from auction_game.configuration import load_configuration, load_game_settings
from auction_game.cli.offline_demo import generate_demo
from auction_game.ai import prompts
from auction_game.game.game_rules import GameSettings
from auction_game.reporting.replay import export_replay, protocol_tools
from auction_game.reporting.run_paths import RunPaths
from auction_game.reporting.request_records import load_requests


@pytest.fixture
def demo_configuration(monkeypatch, config_files):
    """完整阶段使用合成参数；生产 game.yaml 仅由独立烟测读取。"""
    def load(**kwargs):
        return load_configuration(*config_files, **kwargs)
    monkeypatch.setattr(offline_demo, "load_configuration", load)
    # 模拟比赛及离线 SDK 捕获都禁止读取真实密钥。
    monkeypatch.setattr("auction_game.ai.llm_client._require_env", lambda *args: (_ for _ in ()).throw(
        AssertionError("离线完整赛禁止读取密钥")))
    return load(test_model=True)


def test_complete_ai_tournament_and_selected_player_replay(tmp_path, monkeypatch, capsys, demo_configuration):
    game = GameSettings(7, 3, (1, 1), 7, 2, 3, 2, 2)
    monkeypatch.setattr(offline_demo, "load_game_settings", lambda: game)
    _, configs = demo_configuration
    names = [pc["name"] for pc in configs]
    run_dir = tmp_path / "20261016_120000_ai"
    replay_path, factories = generate_demo(run_dir, test_model=True)
    paths = RunPaths(run_dir)
    assert len(factories) == len(names)
    expected_calls = 2 + game.regular_matches * (game.max_days - 1) + 3 * (game.regular_matches - 1) + 2
    assert all(f.opened == f.closed == f.calls == expected_calls for f in factories)
    snapshot = paths.read_config()
    tool_definitions = json.loads(paths.tool_definitions.read_text(encoding="utf-8"))
    tool_refs = [json.loads(line) for line in paths.tool_requests.read_text(encoding="utf-8").splitlines()]
    assert len(tool_definitions) == 4 and len(tool_refs) == expected_calls * len(names)
    assert all(ref["source"] == "offline_sdk_generated" and ref["definition_id"] in tool_definitions for ref in tool_refs)
    assert all(ref["versions"]["pydantic-ai-slim"] for ref in tool_refs)
    assert "离线 SDK 生成" in paths.tools.read_text(encoding="utf-8")
    assert "[本场工具定义](tools.md)" in (run_dir / "README.md").read_text(encoding="utf-8")
    assert snapshot["run_type"] == "offline"
    assert snapshot["mode"] == "test_model"
    for pc, saved in zip(configs, snapshot["players"]):
        assert saved["identity_model"] == pc["identity_model"]
        assert saved["execution_model"] == {"key": pc["model_key"], "base_url": "https://offline.invalid/v1",
                                            "model_profile": {"supports_forced_tool_choice": False},
                                            **{k: pc[k] for k in ("model", "provider", "protocol", "max_tokens",
                                                                  "model_settings", "capabilities")}}
        assert pc["identity_model"]["model"] != pc["model"]
    assert all(f.settings["max_tokens"] == configs[0]["max_tokens"] for f in factories)
    assert all(f.settings["openai_reasoning_effort"] == "medium" for f in factories)
    assert all(f.model_profile == {"supports_forced_tool_choice": False} for f in factories)

    matches = analyze.load_match_logs(run_dir)
    monkeypatch.setattr(analyze, "LOG_DIR", tmp_path)
    with pytest.raises(SystemExit, match="没有有效"):
        analyze.find_latest_ai_run()
    assert len(matches) == game.regular_matches
    expected_wins = sum(game.daily_pool)
    for index, payload in enumerate(matches, 1):
        match = payload["match"]
        assert match["match_index"] == index and not match["is_playoff"]
        assert match["game"] == game.to_dict()
        assert match["wins"] == {name: expected_wins if i == 0 else 0 for i, name in enumerate(names)}
        assert len(match["days"]) == game.max_days
        assert all(bid["status"] != "illegal" for day in match["days"] for bid in day["bids"])
        assert paths.match(index, readable=True).exists()
    assert (run_dir / "RULES.md").read_text(encoding="utf-8") == prompts.render_rules(game, names)

    selected = names[0]
    entries = [json.loads(line) for line in paths.player(1).read_text(encoding="utf-8").splitlines()]
    assert all("requests" not in entry for entry in entries)
    load_requests(paths.capture(1), entries)
    assert len(entries) == expected_calls
    for entry in entries:
        refs = [ref for ref in tool_refs if ref["number"] == 1 and ref["task_id"] == entry["task_id"]]
        assert [ref["definition_id"] for ref in refs] == entry["tool_definitions"]
    assert [entry["phase"] for entry in entries[:3]] == ["opening_speech", "opening_plan", "bid"]
    assert entries[2]["context"] == "第1局第2天"
    assert not any(entry["context"].endswith("第1天") for entry in entries if entry["task"] == "bid")
    requests = [entry["requests"][0] for entry in entries]
    assert len(requests) == expected_calls
    for entry, record in zip(entries, requests):
        assert entry["status"] == "success" and not entry["request_errors"]
        assert record["request"] == 1
        assert record["settings"]["max_tokens"] == configs[0]["max_tokens"]
        assert record["settings"]["openai_reasoning_effort"] == "medium"
        ModelMessagesTypeAdapter.validate_python(record["messages"])
        ModelMessagesTypeAdapter.validate_python([record["response"]])
        ModelMessagesTypeAdapter.validate_python(entry["messages"])
        # Agent 会在接收后补 run_id 等元数据，边界记录的输出正文保持一致。
        assert record["response"]["parts"] in [message["parts"] for message in entry["messages"]
                                               if message["kind"] == "response"]
        if entry["task"] != "summary":
            tool = record["parameters"]["output_tools"][0]
            assert tool["name"] == {"plan": "bid", "memory": "save_memory"}.get(entry["task"], entry["task"])
            properties = tool["parameters_json_schema"]["properties"]
            field = "memory" if entry["task"] == "memory" else "strategy"
            assert properties[field]["description"].startswith("仅自己可见")
            if entry["task"] == "memory":
                assert set(properties) == {"memory"} and entry["memory_chars"] == len(entry["result"]["memory"])
        if entry["task"] == "plan":
            match_index = 1 if entry["phase"] == "opening_plan" else int(entry["phase"].split("_")[1]) + 1
            actual = matches[match_index - 1]["match"]["days"][0]["bids"][0]
            assert (entry["result"]["bid"], entry["result"]["super_bid"]) == (actual["bid"], None)
        if entry["phase"] == "final_summary":
            # 总结看到全场原生对话和工具回执，推理仅留原始审计。
            review = json.dumps(record["messages"], ensure_ascii=False)
            history = [p["content"] for m in record["messages"] for p in m["parts"]
                       if p["part_kind"] == "user-prompt"]
            for prior in entries[:-1]:
                for message in prior["messages"]:
                    for part in message["parts"]:
                        if part["part_kind"] == "user-prompt":
                            assert any(part["content"] in text for text in history)
            assert "程序提供的已完成任务资料" not in review
            assert any(m["kind"] == "response" for m in record["messages"])
            assert '"part_kind": "thinking"' not in review and '"part_kind": "tool-call"' in review
    # 非选中玩家正常日志不保存重复请求。
    for number, name in enumerate(names[1:], 2):
        assert all("requests" not in json.loads(line) for line in
                   paths.player(number).read_text(encoding="utf-8").splitlines())
        assert not paths.capture(number).exists()

    replay = replay_path.read_text(encoding="utf-8")
    assert "[本场默认工具记录](tools.md)" in replay
    assert "tools.md" not in paths.player(1, readable=True).read_text(encoding="utf-8")
    titles = ["比赛开头 · 开局发言", "第一局规划 · 首日缓存", "第一局第二天 · 正常出价",
              "第一局结束 · 局末发言", "第一局结束 · 累计私有记忆", "第二局规划 · 接收实际反馈", "实际比赛结尾 · 最终感言",
              "实际比赛结尾 · 观众总结"]
    positions = [replay.index("## " + title) for title in titles]
    assert positions == sorted(positions)
    assert replay.count("【你的身份】") == 1
    assert "不重复请求模型" in replay and "跳转：省略" in replay
    assert "## 第1局第3天" not in replay and "## 第2局第2天" not in replay
    assert "不含 HTTP 报文" in replay and "模拟回复" in replay and "仅 speech 投递" in replay
    assert "SDK 在离线模拟传输中生成的请求定义，未发送到真实中转" in replay
    snapshots = json.loads(paths.definitions(1).read_text(encoding="utf-8"))
    assert len(snapshots) == 4  # 共享出价工具、发言、记忆与文本；完整赛不逐请求重复捕获。
    definitions = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", replay, re.S)
                   if '"tools"' in block]
    assert definitions == [snapshot["definition"] for snapshot in snapshots.values() if snapshot["definition"].get("tools")]
    for snapshot in snapshots.values():
        assert "body" not in snapshot and snapshot["versions"]["openai"]
        assert snapshot["model"] == configs[0]["model"]
        assert snapshot["protocol"] == configs[0]["protocol"]
        assert snapshot["model_profile"] == {"supports_forced_tool_choice": False}
        if snapshot["definition"].get("tools"):
            assert snapshot["definition"]["tool_choice"] == "auto"
    for forbidden in ("task-0001", "opening_speech", "request-1", "schema-1", "outer_typed_dict_key",
                      '"output_tools"', '"metadata"', '"sequential"', '"timeout"', '"part_kind"'):
        assert forbidden not in replay
    for anchor in re.findall(r"\]\(#([^)]*)\)", replay):
        assert f'id="{anchor}"' in replay
    assert not (run_dir / "debug").exists()
    export_replay(run_dir, selected, debug=True)
    debug = (run_dir / "debug" / "001.md").read_text(encoding="utf-8")
    assert "task-0001" in debug and '"settings"' in debug and '"messages"' in debug
    # 切断今天的配置，并保留旧协议文件后重新捕获，验证唯一来源是当场执行快照。
    def unavailable(*args, **kwargs):
        raise AssertionError("回放/分析应读取场次快照")
    monkeypatch.setattr("auction_game.configuration.load_configuration", unavailable)
    protocol_path = paths.definitions(1)
    protocol_path.rename(run_dir / "saved_protocol.json")
    with pytest.raises(ValueError, match="缺少 SDK"):
        export_replay(run_dir, selected)
    (run_dir / "saved_protocol.json").rename(protocol_path)
    monkeypatch.setattr("auction_game.reporting.replay.capture_protocol_definition", unavailable)
    assert export_replay(run_dir, selected).read_text(encoding="utf-8") == replay
    assert json.loads(protocol_path.read_text(encoding="utf-8")) == snapshots
    chosen = [entry for entry in entries if entry["phase"] in {
        "opening_speech", "opening_plan", "match_1_speech", "match_1_memory", "match_1_plan", "final_speech", "final_summary"
    } or entry["context"] == "第1局第2天"]
    for entry in chosen:
        for message in entry["messages"]:
            for part in message["parts"]:
                if part["part_kind"] == "user-prompt":
                    assert part["content"] in replay
        assert entry["task_id"] in debug
        for record in entry["requests"]:
            for part in record["response"]["parts"]:
                if part["part_kind"] == "tool-call":
                    assert part["args"] in replay  # 不硬编码回复，不换成内部对象或最终结果。
                elif part["part_kind"] == "text":
                    assert part["content"] in replay
    for file in (paths.capture(1), paths.definitions(1), run_dir / "debug" / "001.md"):
        content = file.read_text(encoding="utf-8")
        assert "offline-demo-not-a-secret" not in content and "offline-capture-not-a-secret" not in content
    stats = analyze.collect_player_stats(matches)
    assert stats["win_matrix"][selected] == [expected_wins] * game.regular_matches
    assert all(row["illegal"] == 0 for row in stats["rows"])
    # 实际统计入口依赖场次快照：即使当前配置入口损坏仍能完整统计。
    monkeypatch.setattr("auction_game.configuration.load_game_settings", lambda: (_ for _ in ()).throw(AssertionError("应读快照")))
    analyze.analyze(run_dir)
    output = capsys.readouterr().out
    assert "离线演示：全员使用假模型，不调用真实服务，不收费。" in output
    assert f"本场日志目录：{run_dir}" in output
    assert "操作：" not in output and "切换到连续模式" not in output
    assert "测试模型：" not in output and "[预算提醒]" not in output
    # 明确的合成参数覆盖各阶段，不随生产配置缩短而丢失断言。
    assert f"共进行 {game.regular_matches} 局（正赛 {game.regular_matches} 局 + 加赛 0 局）" in output
    assert f"总拍卖天数：{game.regular_matches * game.max_days}" in output
    assert f"\n{selected}\n  总胜场：{game.regular_matches * expected_wins}\n" in output
    assert "测试运行结果（非身份模型成绩）" in output


@pytest.mark.parametrize("game,first_bids,tied", [
    (GameSettings(1, 0, (1, 1), 7, 2, 3, 1, 2), [1, 0], False),
    (GameSettings(4, 0, (1, 1, 1), 7, 2, 3, 1, 2), [3, 1, 0], False),
    (GameSettings(0, 0, (1,), 7, 2, 3, 1, 2), [0], True),
    (GameSettings(7, 0, (1,), 0, 2, 3, 1, 2), [0], True),
])
def test_offline_economic_edges_finish_and_export(game, first_bids, tied, tmp_path,
                                                 monkeypatch, demo_configuration):
    monkeypatch.setattr(offline_demo, "load_game_settings", lambda: game)
    run_dir = tmp_path / "edge"
    replay, factories = generate_demo(run_dir, test_model=True)
    paths = RunPaths(run_dir)
    matches = analyze.load_match_logs(paths.root)
    assert len(matches) == game.regular_matches + (game.max_playoffs if tied else 0)
    for payload in matches:
        match = payload["match"]
        assert match["game"] == game.to_dict()
        assert [day["bids"][0]["bid"] for day in match["days"]] == first_bids
        assert all(bid["status"] != "illegal" for day in match["days"] for bid in day["bids"])
    if tied:
        assert all(wins == 0 for payload in matches for wins in payload["match"]["wins"].values())
    assert replay.exists()
    assert all(factory.opened == factory.closed == factory.calls for factory in factories)


def test_current_game_configuration_smoke(tmp_path, demo_configuration):
    """当前生产参数仅校验启动、快照与产物，不要求任何特定阶段。"""
    game = load_game_settings()
    run_dir = tmp_path / "current"
    replay, factories = generate_demo(run_dir, test_model=True)
    paths = RunPaths(run_dir)
    matches = analyze.load_match_logs(run_dir)
    assert paths.read_config()["game"] == game.to_dict()
    assert game.regular_matches <= len(matches) <= game.regular_matches + game.max_playoffs
    assert all(payload["match"]["game"] == game.to_dict() for payload in matches)
    assert replay.exists()
    assert all(factory.opened == factory.closed for factory in factories)


@pytest.mark.parametrize("test_model", [False, True])
def test_offline_model_profile_follows_actual_execution(
        tmp_path, monkeypatch, config_files, demo_configuration, test_model):
    """身份带强制覆盖，替身关闭；快照与离线 SDK 定义必须使用同一执行配置。"""
    import yaml
    model_path = config_files[2]
    models = yaml.safe_load(model_path.read_text(encoding="utf-8"))
    models["models"]["first"]["model_profile"] = {"supports_forced_tool_choice": True}
    models["models"]["substitute"]["model_profile"] = {"supports_forced_tool_choice": False}
    model_path.write_text(yaml.safe_dump(models), encoding="utf-8")
    monkeypatch.setattr(offline_demo, "load_game_settings", lambda: GameSettings(7, 0, (1,), 7, 2, 3, 1, 0))
    run_dir = tmp_path / "profiles"
    _, factories = generate_demo(run_dir, test_model=test_model)
    paths = RunPaths(run_dir)
    snapshot = paths.read_config()
    assert snapshot["players"][0]["identity_model"]["model_profile"] == {"supports_forced_tool_choice": True}
    assert factories[0].model_profile == {"supports_forced_tool_choice": not test_model}
    definitions = json.loads(paths.tool_definitions.read_text(encoding="utf-8"))
    refs = [json.loads(line) for line in paths.tool_requests.read_text(encoding="utf-8").splitlines()]
    for number, (factory, saved) in enumerate(zip(factories, snapshot["players"]), 1):
        assert saved["execution_model"]["model_profile"] == factory.model_profile
        choices = [definitions[ref["definition_id"]]["definition"]["tool_choice"] for ref in refs
                   if ref["number"] == number and definitions[ref["definition_id"]]["definition"].get("tools")]
        expected = ("auto" if test_model else "required") if number == 1 else (
            "auto" if test_model else {"type": "any"})
        assert choices and all(choice == expected for choice in choices)


def test_offline_reuses_one_game_and_syncs_once_before_directory(tmp_path, monkeypatch, demo_configuration):
    game = GameSettings(7, 3, (1,), 7, 2, 3, 1, 0)
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
    _, factories = generate_demo(run_dir, test_model=True)
    assert loads == syncs == [game]
    assert all(factory.game is game for factory in factories)


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


def test_offline_invalid_output_exits_without_retry_gate(tmp_path, monkeypatch, demo_configuration):
    from pydantic_ai.models.function import DeltaToolCall
    game = GameSettings(7, 3, (1, 1), 7, 2, 3, 1, 0)
    monkeypatch.setattr(offline_demo, "load_game_settings", lambda: game)
    real_stream = offline_demo.DemoFactory.stream
    attempts = []
    async def invalid_bid(self, messages, info):
        tool = info.output_tools[0].name if info.output_tools else "summary"
        if self.index == 0 and tool == "bid":
            attempts.append(tool)
            yield {0: DeltaToolCall(name="bid", json_args=json.dumps(
                {"strategy": "故意非法出价", "bid": -1, "super_bid": None}), tool_call_id="bad-bid")}
        else:
            async for part in real_stream(self, messages, info):
                yield part
    monkeypatch.setattr(offline_demo.DemoFactory, "stream", invalid_bid)
    wait = offline_demo.Controller.wait_for_turn
    def continuous_only(self, label):
        assert self._mode == "continuous", "离线失败进入了无人交互闸门"
        return wait(self, label)
    monkeypatch.setattr(offline_demo.Controller, "wait_for_turn", continuous_only)
    with pytest.raises(RuntimeError, match="离线任务失败：甲 / plan"):
        generate_demo(tmp_path / "invalid", test_model=True)
    assert attempts == ["bid", "bid"]  # 共享输出纠错保留，整轮不自动补跑。
