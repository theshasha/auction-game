"""私人阅读日志只做展示投影；原文、审计及增量写入独立验证。"""

from html import unescape
import json
from pathlib import Path
import re

import pytest

from auction_game.ai.agent_execution import CallFailedError, ExecutionResult
from auction_game.ai.agent_tasks import TaskRunner
from auction_game.reporting.agent_log import AgentLog, render_markdown_entry
from tests.support.factories import bid_reply, fake_task_runner, make_ai_obs, make_player
from tests.support.wire import Harness, reply


def test_inputs_are_collapsed_losslessly_and_final_fields_appear_once(tmp_path):
    strategy = "保持原文 **不调整**\n第二行策略"
    speech = "我的原话 <原样>！"
    prompt = '\n输入 <details> & </pre> "引号"\n```json\n{"x": 1}\n```\n' + "长行" * 500 + '\n最后一行\n'
    player = make_player(fake_task_runner([{"strategy": strategy, "speech": speech}]), tmp_path)
    assert player.give_speech(prompt, "opening_speech") == speech
    markdown = player.log_path.with_suffix(".md").read_text(encoding="utf-8")
    assert "## 开局发言" in markdown and "## opening_speech" not in markdown
    assert "<details>\n<summary>输入原文</summary>" in markdown
    assert [unescape(block) for block in re.findall(r"<pre[^>]*><code[^>]*>(.*?)</code></pre>", markdown, re.S)] == [prompt]
    assert 'white-space: pre-wrap; overflow-wrap: anywhere;' in markdown
    assert markdown.count(strategy) == markdown.count(speech) == 1
    assert "tool-return" not in markdown and "call-1" not in markdown
    assert "Final result processed." not in markdown and '"speech":' not in markdown
    entry = json.loads(player.log_path.read_text(encoding="utf-8"))
    assert entry["result"] == {"strategy": strategy, "speech": speech}
    assert any(part["part_kind"] == "tool-return" for msg in entry["messages"] for part in msg["parts"])
    assert f"[{entry['task_id']}]({player.log_path.name})" in markdown


@pytest.mark.parametrize("super_bid,label", [(None, "未使用"), (0, "0"), (3, "3")])
def test_plan_has_one_human_submission_and_cached_day_one_writes_nothing(tmp_path, super_bid, label):
    player = make_player(fake_task_runner([bid_reply(1, super_bid, strategy="策略原文")]), tmp_path)
    player.plan_strategy("规划输入", "opening_plan")
    before = player.log_path.with_suffix(".md").read_bytes(), player.log_path.read_bytes()
    assert player.decide_bid(make_ai_obs()) == (1, super_bid)
    assert (player.log_path.with_suffix(".md").read_bytes(), player.log_path.read_bytes()) == before
    markdown = before[0].decode()
    assert "## 第1局策略规划（含首日出价）" in markdown
    assert markdown.count("策略原文") == 1
    assert f"正常出价 1；超级出价 {label}" in markdown
    assert "提交出价" in markdown and "出价结果" not in markdown and '"bid":' not in markdown


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
    assert markdown.count("<summary>输入原文</summary>") == 1
    assert markdown.count("任务补跑 0 次") == markdown.count("任务补跑 1 次") == markdown.count("任务补跑 2 次") == 1
    assert markdown.count("鉴权失败（HTTP 401）") == 2
    assert markdown.count("网络尝试 1 次") == 3
    assert markdown.count("首次任务 task-0001](#task-0001)") == 2
    assert "上次记录 task-0002](#task-0002)" in markdown
    assert "状态：成功" not in prefixes[1]
    entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
    assert [entry["status"] for entry in entries] == ["failed", "failed", "success"]
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
    assert "输出纠错 1 次，最终通过校验" in markdown
    assert "原因：bid 必须是整数" in markdown
    assert "网络尝试 2 次（请求 1：1 次；请求 2：1 次）" in markdown
    audit = player.log_path.read_text(encoding="utf-8")
    assert "首次无效策略" in audit and "最终策略原文" in audit and "retry-prompt" in audit


@pytest.mark.parametrize("success", [True, False])
@pytest.mark.parametrize("content,reason", [
    ("Unknown tool name: 'plan'. Available tools: 'bid'", "误用工具 plan，提示改用 bid"),
    ("正常出价 8 超过当前余额 4", "正常出价 8 超过当前余额 4"),
    ([{"loc": ["bid"], "type": "int_type", "msg": "Input should be a valid integer",
       "input": "无效私有策略", "ctx": {"data": "无效私有策略"}}], "bid 必须是整数"),
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
    assert f"原因：{reason}" in markdown
    assert f"输出纠错 1 次，{'最终通过校验' if success else '最终失败'}" in markdown
    assert "无效私有策略" not in markdown and "retry-prompt" not in markdown
    assert entry == before


def test_wrong_tool_correction_uses_actual_sdk_retry_prompt(tmp_path):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    names = iter(["plan", "bid"])

    async def stream(messages, info):
        yield {0: DeltaToolCall(name=next(names), json_args=json.dumps(bid_reply(2)), tool_call_id="test-call")}

    @asynccontextmanager
    async def open_model():
        model = FunctionModel(stream_function=stream)
        model.request_errors = []
        yield model

    player = make_player(TaskRunner(SimpleNamespace(open=open_model, settings={})), tmp_path)
    assert player.decide_bid(make_ai_obs(day=2)) == (2, None)
    markdown = player.log_path.with_suffix(".md").read_text(encoding="utf-8")
    assert "误用工具 plan，提示改用 bid" in markdown
    assert "输出纠错 1 次，最终通过校验" in markdown
    entry = json.loads(player.log_path.read_text(encoding="utf-8"))
    retry = next(part for message in entry["messages"] for part in message["parts"]
                 if part["part_kind"] == "retry-prompt")
    assert "Unknown tool name: 'plan'" in retry["content"]


@pytest.mark.parametrize("phase,title", [
    ("match_2_speech", "第2局局末发言"),
    ("match_2_plan", "第3局策略规划（含首日出价）"),
    ("final_speech", "最终感言"), ("final_summary", "观众总结（不回喂）"),
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
    assert "网络尝试次数未记录" in markdown
    assert "[task-0001](../data/player.jsonl)" in markdown
    assert json.loads(log.path.read_text(encoding="utf-8"))["error"]["message"] == message


def test_changed_input_is_not_hidden_by_failed_task_link(tmp_path):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    log = AgentLog("Alpha", tmp_path / "player.jsonl", readable_path=tmp_path / "player.md")
    entry = {"phase": "opening_speech", "task": "speech", "context": ""}
    log.failure(entry, CallFailedError("failure", messages=[ModelRequest(parts=[UserPromptPart("原输入")])]))
    log.success(entry, ExecutionResult("原输出", [ModelRequest(parts=[UserPromptPart("新输入")])], []))
    markdown = log.readable_path.read_text(encoding="utf-8")
    assert markdown.count("<summary>输入原文</summary>") == 2
    assert "首次任务" not in markdown
