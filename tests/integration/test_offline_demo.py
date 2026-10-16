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
from auction_game.reporting.replay import export_replay, protocol_tools
from auction_game.reporting.run_paths import RunPaths
from auction_game.reporting.request_records import load_requests


def test_complete_ai_tournament_and_selected_player_replay(tmp_path, monkeypatch, capsys, config_files):
    game = load_game_settings()
    def load(**kwargs):
        return load_configuration(*config_files, **kwargs)
    monkeypatch.setattr(offline_demo, "load_configuration", load)
    monkeypatch.setattr(runners, "load_configuration", load)
    _, configs = load(test_model=True)
    names = [pc["name"] for pc in configs]
    # 模拟比赛及离线 SDK 捕获都禁止读取真实密钥。
    monkeypatch.setattr("auction_game.ai.llm_client._require_env", lambda *args: (_ for _ in ()).throw(
        AssertionError("离线完整赛禁止读取密钥")))
    run_dir = tmp_path / "20261016_120000_ai"
    replay_path, factories = generate_demo(run_dir, test_model=True)
    paths = RunPaths(run_dir)
    assert len(factories) == len(names)
    expected_calls = 2 + game.regular_matches * (game.max_days - 1) + 2 * (game.regular_matches - 1) + 2
    assert all(f.opened == f.closed == f.calls == expected_calls for f in factories)
    snapshot = paths.read_config()
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
    assert all(f.settings["openai_reasoning_effort"] == "high" for f in factories)
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
    assert [entry["phase"] for entry in entries[:3]] == ["opening_speech", "opening_plan", "bid"]
    assert entries[2]["context"] == "第1局第2天"
    assert not any(entry["context"].endswith("第1天") for entry in entries if entry["task"] == "bid")
    requests = [entry["requests"][0] for entry in entries]
    assert len(requests) == expected_calls
    committed = []
    for entry, record in zip(entries, requests):
        assert entry["status"] == "success" and not entry["request_errors"]
        assert record == entry["requests"][0]
        assert record["request"] == 1
        assert record["settings"]["max_tokens"] == configs[0]["max_tokens"]
        assert record["settings"]["openai_reasoning_effort"] == "high"
        ModelMessagesTypeAdapter.validate_python(record["messages"])
        ModelMessagesTypeAdapter.validate_python([record["response"]])
        ModelMessagesTypeAdapter.validate_python(entry["messages"])
        # Agent 会在接收后补 run_id 等元数据，边界记录的输出正文保持一致。
        assert record["response"]["parts"] in [message["parts"] for message in entry["messages"]
                                               if message["kind"] == "response"]
        if entry["task"] != "summary":
            tool = record["parameters"]["output_tools"][0]
            assert tool["name"] == entry["task"]
            assert tool["parameters_json_schema"]["properties"]["strategy"]["description"].startswith("仅自己可见")
        if entry["task"] == "plan":
            match_index = 1 if entry["phase"] == "opening_plan" else int(entry["phase"].split("_")[1]) + 1
            actual = matches[match_index - 1]["match"]["days"][0]["bids"][0]
            assert (entry["result"]["bid"], entry["result"]["super_bid"]) == (actual["bid"], None)
        if entry["phase"] == "final_summary":
            # 总结看到此前所有任务，但请求与回复并不产生后续玩家状态。
            assert all(message in record["messages"] for message in committed if message["kind"] == "response")
        committed.extend(entry["messages"])
    # 非选中玩家正常日志不保存重复请求。
    for number, name in enumerate(names[1:], 2):
        assert all("requests" not in json.loads(line) for line in
                   paths.player(number).read_text(encoding="utf-8").splitlines())
        assert not paths.capture(number).exists()

    replay = replay_path.read_text(encoding="utf-8")
    titles = ["比赛开头 · 开局发言", "第一局规划 · 首日缓存", "第一局第二天 · 正常出价",
              "第一局结束 · 局末发言", "第二局规划 · 接收实际反馈", "实际比赛结尾 · 最终感言",
              "实际比赛结尾 · 观众总结"]
    positions = [replay.index("## " + title) for title in titles]
    assert positions == sorted(positions)
    assert replay.count("【你的身份】") == 1
    assert "不重复请求模型" in replay and "跳转：省略" in replay
    assert "## 第1局第3天" not in replay and "## 第2局第2天" not in replay
    assert "非 HTTP 报文" in replay and "模拟回复" in replay and "仅 speech 投递" in replay
    assert "SDK 在离线模拟传输中生成的请求定义，未发送到真实中转" in replay
    snapshots = json.loads(paths.definitions(1).read_text(encoding="utf-8"))
    assert len(snapshots) == 4  # 三种工具 + 文本；完整赛不逐请求重复捕获。
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
    for anchor in re.findall(r"\]\(debug_[^#)\n]*#([^)]*)\)", replay):
        assert f'id="{anchor}"' in debug
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
    assert export_replay(run_dir, selected, simulated=True).read_text(encoding="utf-8") == replay
    assert json.loads(protocol_path.read_text(encoding="utf-8")) == snapshots
    chosen = [entry for entry in entries if entry["phase"] in {
        "opening_speech", "opening_plan", "match_1_speech", "match_1_plan", "final_speech", "final_summary"
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
    # 跟随当前比赛配置核对完整赛输出，修改生产参数无需同步维护旧默认值。
    assert f"共进行 {game.regular_matches} 局（正赛 {game.regular_matches} 局 + 加赛 0 局）" in output
    assert f"总拍卖天数：{game.regular_matches * game.max_days}" in output
    assert f"{selected}：胜{game.regular_matches * expected_wins}" in output
    assert "测试运行结果（非身份模型成绩）" in output
