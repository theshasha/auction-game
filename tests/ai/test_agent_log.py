"""私人阅读日志只做展示投影；原文、审计及增量写入独立验证。"""

import json
from pathlib import Path

import pytest

from auction_game.ai.agent_execution import CallFailedError, ExecutionResult
from auction_game.ai.agent_tasks import TaskRunner
from auction_game.ai.text_output import output_requirement
from auction_game.reporting.agent_log import AgentLog, render_markdown_entry
from tests.support.factories import bid_reply, fake_task_runner, make_ai_obs, make_player, task_entries
from tests.support.wire import Harness, reply


def test_inputs_are_expanded_losslessly_and_final_fields_appear_once(tmp_path):
    strategy = "保持原文 **不调整**\n第二行策略"
    speech = "我的原话 <原样>！"
    prompt = '\n输入 <details> & </pre> "引号"\n```json\n{"x": 1}\n```\n' + "长行" * 500 + '\n最后一行\n'
    player = make_player(fake_task_runner([{"strategy": strategy, "speech": speech}]), tmp_path)
    assert player.give_speech(prompt, "opening_speech") == speech
    markdown = player.log_path.with_suffix(".md").read_text(encoding="utf-8")
    assert "## 开局发言" in markdown and "## opening_speech" not in markdown
    assert "### 输入原文\n\n" + prompt + "\n\n" + output_requirement("speech") in markdown
    assert "<summary>" not in markdown and "<br>" not in markdown
    assert markdown.count(strategy) == markdown.count(speech) == 1
    assert "tool-return" not in markdown and "call-1" not in markdown
    assert "Final result processed." not in markdown and '"speech":' not in markdown
    entry = task_entries(player.log_path)[-1]
    assert entry["result"] == {"strategy": strategy, "speech": speech}
    response = [part for msg in entry["messages"] if msg["kind"] == "response" for part in msg["parts"]]
    assert len(response) == 1 and response[0]["part_kind"] == "text"
    assert response[0]["content"] == f"<strategy>\n{strategy}\n</strategy>\n<speech>\n{speech}\n</speech>"
    assert not any(part["part_kind"] in ("tool-call", "tool-return")
                   for msg in entry["messages"] for part in msg["parts"])
    assert '<a id=' not in markdown and entry["task_id"] not in markdown
    assert all(text not in markdown for text in ("任务：", "状态：", "任务补跑", "网络尝试", "完整审计"))
    assert entry["reruns"] == 0 and entry["first_task"] is entry["previous_task"] is None


def test_system_prompt_is_logged_once_before_tasks(tmp_path):
    player = make_player(fake_task_runner([bid_reply(1)]), tmp_path)
    player.decide_bid(make_ai_obs())
    system = player.messages[0].parts[0].content
    first = json.loads(player.log_path.read_text(encoding="utf-8").splitlines()[0])
    assert first == {"task": "system", "content": system}
    markdown = player.log_path.with_suffix(".md").read_text(encoding="utf-8")
    assert markdown.startswith(f"# Alpha · 私人对话（含私有策略）\n\n## 系统提示\n\n{system}\n\n---")
    assert markdown.count("【你的身份】") == 1


@pytest.mark.parametrize("super_bid,label", [(None, "未使用"), (0, "0"), (3, "3")])
def test_plan_has_one_human_submission_and_cached_day_one_writes_nothing(tmp_path, super_bid, label):
    player = make_player(fake_task_runner([bid_reply(1, super_bid, strategy="策略原文")]), tmp_path)
    player.plan_strategy("规划输入", "opening_plan")
    before = player.log_path.with_suffix(".md").read_bytes(), player.log_path.read_bytes()
    assert player.decide_bid(make_ai_obs()) == (1, super_bid)
    assert (player.log_path.with_suffix(".md").read_bytes(), player.log_path.read_bytes()) == before
    markdown = before[0].decode()
    assert "## 第1局策略规划含首日出价" in markdown
    assert markdown.count("策略原文") == 1
    assert f"正常出价 1；超级出价 {label}" in markdown
    assert "提交出价" in markdown and "出价结果" not in markdown
    assert '"bid":' not in markdown.split("### 私有策略", 1)[1]


def test_failures_and_recovery_append_links_to_first_input(tmp_path, monkeypatch):
    h = Harness("chat", [401, 401, reply()])
    player = make_player(TaskRunner(h.factory), tmp_path)
    md_path = player.log_path.with_suffix(".md")
    original_open = Path.open
    writes = []

    def open_file(path, mode="r", *args, **kwargs):
        if path == md_path:
            writes.append(mode)
            assert mode == "a", "补跑不得读回或整文件重写私人日志"
        return original_open(path, mode, *args, **kwargs)

    prefixes = []
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", open_file)
        for _ in range(2):
            with pytest.raises(CallFailedError):
                player.decide_bid(make_ai_obs(day=2))
            with original_open(md_path, encoding="utf-8") as file:
                prefixes.append(file.read())
        assert player.decide_bid(make_ai_obs(day=2)) == (2, None)
    markdown = md_path.read_text(encoding="utf-8")
    assert writes == ["a", "a", "a"]
    assert prefixes[1].startswith(prefixes[0]) and markdown.startswith(prefixes[1])
    assert markdown.count("### 输入原文") == 1
    assert markdown.count("补跑仍失败。") == markdown.count("补跑后成功。") == 1
    assert markdown.count("鉴权失败（HTTP 401）") == 2
    assert "网络尝试" not in markdown and "完整审计" not in markdown
    assert markdown.count("首次输入](#第1局第2天每日出价)") == 2
    assert "上次记录](#第1局第2天每日出价补跑1)" in markdown
    assert "状态：成功" not in prefixes[1]
    entries = task_entries(player.log_path)
    assert [entry["status"] for entry in entries] == ["failed", "failed", "success"]
    assert [entry["reruns"] for entry in entries] == [0, 1, 2]
    assert [entry["first_task"] for entry in entries] == [None, "task-0001", "task-0001"]
    assert [entry["previous_task"] for entry in entries] == [None, "task-0001", "task-0002"]
    assert all(any(part["part_kind"] == "user-prompt" for msg in entry["messages"]
                   for part in msg["parts"]) for entry in entries)


def test_output_correction_shows_only_final_strategy_and_keeps_both_audited(tmp_path):
    runner = fake_task_runner([bid_reply("bad", strategy="首次无效策略"),
                               bid_reply(2, strategy="最终策略原文")])
    player = make_player(runner, tmp_path)
    player.decide_bid(make_ai_obs(day=2))
    markdown = player.log_path.with_suffix(".md").read_text(encoding="utf-8")
    assert "首次无效策略" not in markdown
    assert markdown.count("最终策略原文") == 1
    assert "纠错 1 次，已通过（bid 必须是不加引号的整数" in markdown
    assert "网络尝试" not in markdown
    audit = player.log_path.read_text(encoding="utf-8")
    assert "首次无效策略" in audit and "最终策略原文" in audit and "retry-prompt" in audit


@pytest.mark.parametrize("success", [True, False])
@pytest.mark.parametrize("content,reason", [
    ("正常出价 8 超过当前余额 4", "正常出价 8 超过当前余额 4"),
    ("decision 必须是 JSON 对象\n请重新提交完整结果。\n<strategy>\n策略正文\n</strategy>",
     "decision 必须是 JSON 对象"),
    ("长错误" * 100, "长错误" * 33 + "长…"),
])
def test_correction_reason_is_short_and_does_not_include_invalid_input(content, reason, success):
    from copy import deepcopy

    entry = {"task_id": "task-0001", "phase": "bid", "context": "第1局第2天", "task": "bid",
             "status": "success" if success else "failed", "request_errors": [],
             "messages": [{"kind": "response", "parts": []},
                          {"kind": "request", "parts": [{"part_kind": "retry-prompt", "content": content}]},
                          {"kind": "response", "parts": []}]}
    if not success:
        entry["error"] = {"type": "OutputFailedError", "message": "budget exhausted"}
    before = deepcopy(entry)
    markdown = render_markdown_entry(entry)
    assert f"纠错 1 次，{'已通过' if success else '仍失败'}（{reason}）。" in markdown
    assert "无效私有策略" not in markdown and "retry-prompt" not in markdown
    assert "请重新提交完整结果" not in markdown and "策略正文" not in markdown
    assert entry == before


def test_text_format_correction_uses_actual_sdk_retry_prompt(tmp_path):
    invalid = "<strategy>\n首次私有策略\n</strategy>"
    player = make_player(fake_task_runner([invalid, bid_reply(2, strategy="最终私有策略")]), tmp_path)
    assert player.decide_bid(make_ai_obs(day=2)) == (2, None)
    markdown = player.log_path.with_suffix(".md").read_text(encoding="utf-8")
    assert "纠错 1 次，已通过" in markdown
    assert "首次私有策略" not in markdown and "最终私有策略" in markdown
    entry = task_entries(player.log_path)[-1]
    retry = next(part for message in entry["messages"] for part in message["parts"]
                 if part["part_kind"] == "retry-prompt")
    assert "<decision>" in retry["content"]
    assert any(part.get("content") == invalid for message in entry["messages"] for part in message["parts"])


@pytest.mark.parametrize("phase,title", [
    ("match_2_speech", "第2局局末发言"),
    ("match_2_plan", "第3局策略规划（含首日出价）"),
    ("final_speech", "最终感言"), ("final_summary", "观众总结（仅供观众）"),
    ("bid", "第3局第2天 · 每日出价"),
])
def test_phase_titles(phase, title):
    entry = {"task_id": "task-0001", "phase": phase, "context": "第3局第2天", "task": "bid",
             "status": "success", "messages": [], "request_errors": [], "result": "原文"}
    assert f"## {title}" in render_markdown_entry(entry)


def test_unknown_failure_is_short_and_audit_stays_complete(tmp_path):
    log = AgentLog("Alpha", tmp_path / "data" / "player.jsonl", readable_path=tmp_path / "players" / "player.md")
    message = "未识别故障\n" + "长错误" * 200
    log.failure({"phase": "bid", "task": "bid", "context": "第1局第2天"}, CallFailedError(message))
    markdown = log.readable_path.read_text(encoding="utf-8")
    assert "未识别故障" in markdown and message not in markdown
    assert "网络尝试" not in markdown
    assert "完整审计" not in markdown and "../data/player.jsonl" not in markdown
    assert json.loads(log.path.read_text(encoding="utf-8"))["error"]["message"] == message


def test_changed_input_is_not_hidden_by_failed_task_link(tmp_path):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    log = AgentLog("Alpha", tmp_path / "player.jsonl", readable_path=tmp_path / "player.md")
    entry = {"phase": "opening_speech", "task": "speech", "context": ""}
    log.failure(entry, CallFailedError("failure", messages=[ModelRequest(parts=[UserPromptPart("原输入")])]))
    log.success(entry, ExecutionResult("原输出", [ModelRequest(parts=[UserPromptPart("新输入")])], []))
    markdown = log.readable_path.read_text(encoding="utf-8")
    assert markdown.count("### 输入原文") == 2
    assert "首次输入" not in markdown
    assert markdown.count("## 开局发言\n") == markdown.count("## 开局发言记录2\n") == 1


def test_input_markdown_preserves_indentation():
    content = '  原文\n\n\t& "x"'
    entry = {"phase": "bid", "status": "success", "messages": [
        {"parts": [{"part_kind": "user-prompt", "content": content}]}]}
    assert "### 输入原文\n\n" + content in render_markdown_entry(entry)
