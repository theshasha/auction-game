"""一条生产 AI 完整赛：Agent、控制器、导演、结算、审计与回放均真实执行。"""

import json
import re

from pydantic_ai.messages import ModelMessagesTypeAdapter

import analyze
from configuration import load_configuration, load_game_settings
from offline_demo import generate_demo
import prompts
from replay import export_replay, protocol_tools


def test_complete_ai_tournament_and_selected_player_replay(tmp_path, monkeypatch, capsys):
    game = load_game_settings()
    _, configs = load_configuration()
    names = [pc["name"] for pc in configs]
    # 模拟比赛及离线 SDK 捕获都禁止读取真实密钥。
    monkeypatch.setattr("llm_client._require_env", lambda *args: (_ for _ in ()).throw(
        AssertionError("离线完整赛禁止读取密钥")))
    run_dir = tmp_path / "20261016_120000_ai"
    replay_path, factories = generate_demo(run_dir)
    assert len(factories) == len(names)
    expected_calls = 2 + game.regular_matches * (game.max_days - 1) + 2 * (game.regular_matches - 1) + 2
    assert all(f.opened == f.closed == f.calls == expected_calls for f in factories)

    matches = analyze.load_match_logs(run_dir)
    monkeypatch.setattr(analyze, "LOG_DIR", tmp_path)
    assert analyze.find_latest_ai_run() == run_dir
    assert len(matches) == game.regular_matches == 5
    expected_wins = sum(game.daily_pool)
    for index, payload in enumerate(matches, 1):
        match = payload["match"]
        assert match["match_index"] == index and not match["is_playoff"]
        assert match["game"] == game.to_dict()
        assert match["wins"] == {name: expected_wins if i == 0 else 0 for i, name in enumerate(names)}
        assert len(match["days"]) == game.max_days
        assert all(bid["status"] != "illegal" for day in match["days"] for bid in day["bids"])
        assert (run_dir / f"match_{index}.md").exists()
    assert (run_dir / "RULES.md").read_text(encoding="utf-8") == prompts.render_rules(game, names)

    selected = names[0]
    entries = [json.loads(line) for line in (run_dir / f"ai_{selected}.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(entries) == expected_calls
    assert [entry["phase"] for entry in entries[:3]] == ["opening_speech", "opening_plan", "bid"]
    assert entries[2]["context"] == "第1局第2天"
    assert not any(entry["context"].endswith("第1天") for entry in entries if entry["task"] == "bid")
    requests = [json.loads(line) for line in (run_dir / f"requests_{selected}.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(requests) == expected_calls
    committed = []
    for entry, record in zip(entries, requests):
        assert entry["status"] == "success" and not entry["request_errors"]
        assert record == {"task_id": entry["task_id"], "task": entry["task"], "phase": entry["phase"],
                          "context": entry["context"], **entry["requests"][0]}
        assert record["request"] == 1
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
    for name in names[1:]:
        assert all("requests" not in json.loads(line) for line in
                   (run_dir / f"ai_{name}.jsonl").read_text(encoding="utf-8").splitlines())

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
    snapshots = json.loads((run_dir / f"protocol_{selected}.json").read_text(encoding="utf-8"))
    assert len(snapshots) == 4  # 三种工具 + 文本；完整赛不逐请求重复捕获。
    definitions = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", replay, re.S)
                   if '"tools"' in block]
    assert definitions == [snapshot["definition"] for snapshot in snapshots.values() if snapshot["definition"].get("tools")]
    for snapshot in snapshots.values():
        assert snapshot["definition"] == protocol_tools(snapshot["body"])
    for forbidden in ("task-0001", "opening_speech", "request-1", "schema-1", "outer_typed_dict_key",
                      '"output_tools"', '"metadata"', '"sequential"', '"timeout"', '"part_kind"'):
        assert forbidden not in replay
    for anchor in re.findall(r"\]\(#([^)]*)\)", replay):
        assert f'id="{anchor}"' in replay
    debug = (run_dir / f"debug_{selected}.md").read_text(encoding="utf-8")
    for anchor in re.findall(r"\]\(debug_[^#)\n]*#([^)]*)\)", replay):
        assert f'id="{anchor}"' in debug
    assert "task-0001" in debug and '"settings"' in debug and '"messages"' in debug
    assert export_replay(run_dir, selected, simulated=True).read_text(encoding="utf-8") == replay
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
    for file in (run_dir / f"requests_{selected}.jsonl", run_dir / f"protocol_{selected}.json", run_dir / f"debug_{selected}.md"):
        content = file.read_text(encoding="utf-8")
        assert "offline-demo-not-a-secret" not in content and "offline-capture-not-a-secret" not in content
    stats = analyze.collect_player_stats(matches)
    assert stats["win_matrix"][selected] == [expected_wins] * game.regular_matches
    assert all(row["illegal"] == 0 for row in stats["rows"])
    # 实际统计入口依赖场次快照：即使当前配置入口损坏仍能完整统计。
    monkeypatch.setattr("configuration.load_game_settings", lambda: (_ for _ in ()).throw(AssertionError("应读快照")))
    analyze.analyze(run_dir)
    output = capsys.readouterr().out
    assert "共进行 5 局（正赛 5 局 + 加赛 0 局）" in output
    assert "总拍卖天数：30" in output
    assert f"{selected}：胜30" in output
