"""私人日志的 Markdown 展示和 JSONL 审计记录。"""

import json

import pytest

from auction_game.llm.execution import CallFailedError, ExecutionResult, TaskCancelledError
from auction_game.agent.tasks import SpeechOutput, SummaryOutput, TaskRunner
from auction_game.agent.text_output import LetterNames, SPEECH, output_requirement
from auction_game.agent.log import AgentLog, ResumeError, render_markdown_entry
from tests.support.factories import (ALL_NAMES, bid_reply, fake_task_runner, make_ai_obs, make_player, reopen_player,
                                     task_entries)
from tests.support.wire import Harness, reply


def test_inputs_are_expanded_losslessly_and_final_fields_appear_once(tmp_path):
    strategy = "保持原文 **不调整**\n第二行策略"
    speech = "我的原话 <原样>！"
    prompt = '\n输入 <details> & </pre> "引号"\n```json\n{"x": 1}\n```\n' + "长行" * 500 + '\n最后一行\n'
    player = make_player(fake_task_runner([{"strategy": strategy, "speech": speech}]), tmp_path)
    output = player.give_speech(prompt, "开局发言")
    assert (output.speech, output.letters) == (speech, [])
    markdown = player.log.readable_path.read_text(encoding="utf-8")
    assert "## 开局发言" in markdown
    letters = LetterNames("Alpha", tuple(ALL_NAMES[1:]))
    assert "### 输入原文\n\n" + prompt + "\n\n" + output_requirement(SPEECH, letters) in markdown
    assert markdown.count(strategy) == markdown.count(speech) == 1
    entry = task_entries(player.log.path)[-1]
    assert entry["result"] == {"strategy": strategy, "speech": speech, "letters": []}
    response = [part for msg in entry["messages"] if msg["kind"] == "response" for part in msg["parts"]]
    assert len(response) == 1 and response[0]["part_kind"] == "text"
    assert response[0]["content"] == f"<strategy>\n{strategy}\n</strategy>\n<speech>\n{speech}\n</speech>"
    assert entry["reruns"] == 0 and entry["first_task"] is entry["previous_task"] is None


def test_system_prompt_is_logged_once_before_tasks(tmp_path):
    player = make_player(fake_task_runner([bid_reply(1)]), tmp_path)
    player.bid(make_ai_obs(), {}, "第 1 局第 2 天出价")
    system = player.messages[0].parts[0].content
    first = json.loads(player.log.path.read_text(encoding="utf-8").splitlines()[0])
    assert first == {"task": "system", "content": system}
    markdown = player.log.readable_path.read_text(encoding="utf-8")
    assert markdown.startswith(f"# Alpha · 私人对话（含私有策略）\n\n## 系统提示\n\n{system}\n\n---")
    assert markdown.count("【你的身份】") == 1


@pytest.mark.parametrize("super_bid,label", [(None, "未使用"), (0, "0"), (3, "3")])
def test_plan_log_has_one_human_submission(tmp_path, super_bid, label):
    player = make_player(fake_task_runner([bid_reply(1, super_bid, strategy="策略原文")]), tmp_path)
    assert player.plan_strategy("规划输入", "第 1 局策略规划（含首日出价）").submission == (1, super_bid)
    markdown = player.log.readable_path.read_text(encoding="utf-8")
    assert "## 第1局策略规划含首日出价" in markdown
    assert markdown.count("策略原文") == 1
    assert f"正常出价 1；超级出价 {label}" in markdown
    assert "提交出价" in markdown


def test_failures_and_recovery_append_links_to_first_input(tmp_path):
    h = Harness("chat", [401] * 8 + [reply()])
    player = make_player(TaskRunner(h.factory), tmp_path)
    md_path = player.log.readable_path
    prefixes = []
    for _ in range(2):
        with pytest.raises(CallFailedError):
            player.bid(make_ai_obs(day=2), {}, "第 1 局第 2 天出价")
        prefixes.append(md_path.read_text(encoding="utf-8"))
    assert player.bid(make_ai_obs(day=2), {}, "第 1 局第 2 天出价").submission == (2, None)
    markdown = md_path.read_text(encoding="utf-8")
    assert prefixes[1].startswith(prefixes[0]) and markdown.startswith(prefixes[1])
    assert markdown.count("### 输入原文") == 1
    assert markdown.count("补跑仍失败。") == markdown.count("补跑后成功。") == 1
    assert markdown.count("鉴权失败（HTTP 401）") == 2
    assert markdown.count("输入与[首次输入](#第1局第2天出价)相同") == 2
    assert "上次失败见[第1局第2天出价](#第1局第2天出价)。" in markdown
    assert "上次失败见[第1局第2天出价补跑1](#第1局第2天出价补跑1)。" in markdown
    entries = task_entries(player.log.path)
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
    player.bid(make_ai_obs(day=2), {}, "第 1 局第 2 天出价")
    markdown = player.log.readable_path.read_text(encoding="utf-8")
    assert "首次无效策略" not in markdown
    assert markdown.count("最终策略原文") == 1
    assert "纠错 1 次，已通过（bid 必须是不加引号的整数" in markdown
    audit = player.log.path.read_text(encoding="utf-8")
    assert "首次无效策略" in audit and "最终策略原文" in audit and "retry-prompt" in audit


@pytest.mark.parametrize("content,reason,status,outcome", [
    ("正常出价 8 超过当前余额 4", "正常出价 8 超过当前余额 4", "success", "已通过"),
    ("decision 必须是 JSON 对象\n请重新提交完整结果。\n<strategy>\n策略正文\n</strategy>",
     "decision 必须是 JSON 对象", "failed", "仍失败"),
    ("长错误" * 100, "长错误" * 33 + "长…", "success", "已通过"),
    ("正常出价 8 超过当前余额 4", "正常出价 8 超过当前余额 4", "cancelled", "未完成"),
])
def test_correction_reason_is_truncated_first_line(content, reason, status, outcome):
    from copy import deepcopy

    entry = {"task_id": "task-0001", "title": "第 1 局第 2 天出价", "context": "第 1 局第 2 天", "task": "bid",
             "status": status, "request_errors": [],
             "messages": [{"kind": "response", "parts": []},
                          {"kind": "request", "parts": [{"part_kind": "retry-prompt", "content": content}]},
                          {"kind": "response", "parts": []}]}
    if status == "failed":
        entry["error"] = {"type": "OutputFailedError", "message": "budget exhausted"}
    before = deepcopy(entry)
    markdown = render_markdown_entry(entry, "第1局第2天出价")
    assert f"纠错 1 次，{outcome}（{reason}）。" in markdown
    assert "策略正文" not in markdown
    assert entry == before


def test_text_format_correction_uses_actual_sdk_retry_prompt(tmp_path):
    invalid = "<strategy>\n首次私有策略\n</strategy>"
    player = make_player(fake_task_runner([invalid, bid_reply(2, strategy="最终私有策略")]), tmp_path)
    assert player.bid(make_ai_obs(day=2), {}, "第 1 局第 2 天出价").submission == (2, None)
    markdown = player.log.readable_path.read_text(encoding="utf-8")
    assert "纠错 1 次，已通过" in markdown
    assert "首次私有策略" not in markdown and "最终私有策略" in markdown
    entry = task_entries(player.log.path)[-1]
    retry = next(part for message in entry["messages"] for part in message["parts"]
                 if part["part_kind"] == "retry-prompt")
    assert "<decision>" in retry["content"]
    assert any(part.get("content") == invalid for message in entry["messages"] for part in message["parts"])


@pytest.mark.parametrize("title,heading", [
    ("第 2 局局末发言", "第2局局末发言"),
    ("第 3 局策略规划（含首日出价）", "第3局策略规划含首日出价"),
    ("观众总结（仅观众可见）", "观众总结仅观众可见"),
])
def test_heading_is_round_title_without_spaces_or_punctuation(title, heading, tmp_path):
    log = AgentLog("Alpha", tmp_path / "player.jsonl", readable_path=tmp_path / "player.md")
    log.success({"title": title, "task": "summary", "context": title}, ExecutionResult(SummaryOutput("原文"), [], []))
    assert f"\n## {heading}\n" in log.readable_path.read_text(encoding="utf-8")


def test_unknown_failure_is_short_and_audit_stays_complete(tmp_path):
    log = AgentLog("Alpha", tmp_path / "data" / "player.jsonl", readable_path=tmp_path / "players" / "player.md")
    message = "未识别故障\n" + "长错误" * 200
    log.failure({"title": "第 1 局第 2 天出价", "task": "bid", "context": "第 1 局第 2 天"}, CallFailedError(message))
    markdown = log.readable_path.read_text(encoding="utf-8")
    assert "未识别故障" in markdown and message not in markdown
    assert json.loads(log.path.read_text(encoding="utf-8"))["error"]["message"] == message


def test_changed_input_is_shown_again_on_rerun(tmp_path):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    log = AgentLog("Alpha", tmp_path / "player.jsonl", readable_path=tmp_path / "player.md")
    entry = {"title": "开局发言", "task": "speech", "context": "开局发言"}
    log.failure(entry, CallFailedError("failure", messages=[ModelRequest(parts=[UserPromptPart("原输入")])]))
    log.success(entry, ExecutionResult(SpeechOutput("策略", "原输出", []),
                                       [ModelRequest(parts=[UserPromptPart("新输入")])], []))
    markdown = log.readable_path.read_text(encoding="utf-8")
    assert markdown.count("### 输入原文") == 2
    assert "首次输入" not in markdown
    assert markdown.count("## 开局发言\n") == markdown.count("## 开局发言记录2\n") == 1


def test_input_markdown_preserves_indentation():
    content = '  原文\n\n\t& "x"'
    entry = {"status": "success", "messages": [{"parts": [{"part_kind": "user-prompt", "content": content}]}]}
    assert "### 输入原文\n\n" + content in render_markdown_entry(entry, "第1局第2天出价")


def test_private_log_lists_letters_after_result(tmp_path):
    letters = [{"to": "Bravo", "text": "第一行\n第二行"}, {"to": "Charlie", "text": "另一封"}]
    player = make_player(fake_task_runner([{**bid_reply(1), "letters": letters}, bid_reply(2)]), tmp_path)
    player.bid(make_ai_obs(day=2), {}, "第 1 局第 2 天出价")
    player.bid(make_ai_obs(day=3), {}, "第 1 局第 3 天出价")
    markdown = player.log.readable_path.read_text(encoding="utf-8")
    assert ("### 提交出价\n\n正常出价 1；超级出价 未使用\n\n"
            "### 私信\n\n写给 Bravo：第一行\n第二行\n\n写给 Charlie：另一封\n\n---") in markdown
    assert markdown.count("### 私信") == 1


def test_reopened_log_rebuilds_markdown_and_continues_numbering_and_rerun_links(tmp_path):
    """关掉前一个任务成功、一个失败；重新打开后可读日志原样重建，补跑接在原来的失败后面。"""
    h = Harness("chat", [reply(), 401, 401, 401, 401, reply()])
    player = make_player(TaskRunner(h.factory), tmp_path)
    player.plan_strategy("规划输入", "第 1 局策略规划（含首日出价）")
    with pytest.raises(CallFailedError):
        player.bid(make_ai_obs(day=2), {}, "第 1 局第 2 天出价")
    markdown = player.log.readable_path.read_bytes()
    records = player.log.path.read_bytes()
    player.log.readable_path.unlink()

    reopened = reopen_player(player, TaskRunner(h.factory))
    reopened.log.restore_files()
    assert player.log.readable_path.read_bytes() == markdown and player.log.path.read_bytes() == records
    reopened.plan_strategy("规划输入", "第 1 局策略规划（含首日出价）")
    assert reopened.bid(make_ai_obs(day=2), {}, "第 1 局第 2 天出价").submission == (2, None)
    assert len(h.requests) == 6

    entries = task_entries(player.log.path)
    assert [(entry["task_id"], entry["status"], entry["reruns"]) for entry in entries] == [
        ("task-0001", "success", 0), ("task-0002", "failed", 0), ("task-0003", "success", 1)]
    assert entries[2]["first_task"] == entries[2]["previous_task"] == "task-0002"
    text = player.log.readable_path.read_text(encoding="utf-8")
    assert text.count("## 系统提示") == 1 and text.count("### 输入原文") == 2
    assert "## 第1局第2天出价补跑1\n\n补跑后成功。\n\n输入与[首次输入](#第1局第2天出价)相同" in text
    assert player.log.path.read_text(encoding="utf-8").count('"task": "system"') == 1


def test_reopened_log_skips_half_written_last_line_and_truncates_it_on_restore(tmp_path):
    player = make_player(fake_task_runner([bid_reply(1)]), tmp_path)
    player.bid(make_ai_obs(), {}, "第 1 局第 2 天出价")
    complete = player.log.path.read_bytes()
    damaged = complete + '{"task_id": "task-0002", "title": "第 1 局第 3 天'.encode()
    player.log.path.write_bytes(damaged)

    log = AgentLog.open("Alpha", player.log.path, readable_path=player.log.readable_path)
    assert log.next_recorded()["title"] == "第 1 局第 2 天出价"
    assert player.log.path.read_bytes() == damaged  # 核对通过之前不动文件
    log.restore_files()
    assert player.log.path.read_bytes() == complete


@pytest.mark.parametrize("content", [None, b"", b'{"task": "system", "content": "half'])
def test_log_without_complete_system_prompt_cannot_be_reopened(content, tmp_path):
    path = tmp_path / "player.jsonl"
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(ResumeError, match="这场还没开始"):
        AgentLog.open("Alpha", path, readable_path=tmp_path / "player.md")


def test_reopened_log_rebuilds_failed_and_cancelled_records_as_written(tmp_path):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    log = AgentLog("Alpha", tmp_path / "player.jsonl", readable_path=tmp_path / "player.md")
    log.system_prompt("系统提示原文")
    entry = {"title": "开局发言", "task": "speech", "context": "开局发言"}
    request = [ModelRequest(parts=[UserPromptPart("发言输入")])]
    log.failure(entry, CallFailedError("status_code: 429", messages=request))
    log.cancelled(entry, TaskCancelledError(messages=request))
    log.success(entry, ExecutionResult(SpeechOutput("策略", "发言", [{"to": "Bravo", "text": "信"}]), request, []))
    markdown = log.readable_path.read_bytes()
    assert "补跑时被取消。".encode() in markdown and "### 私信".encode() in markdown
    log.readable_path.unlink()

    reopened = AgentLog.open("Alpha", log.path, readable_path=log.readable_path)
    reopened.restore_files()
    assert log.readable_path.read_bytes() == markdown
    assert reopened.next_recorded()["task_id"] == "task-0003"
