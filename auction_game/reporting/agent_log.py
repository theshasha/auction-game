"""完整任务审计及精简私人阅读投影；补跑关联只用于展示。"""

from __future__ import annotations

import json
import html
import os
from pathlib import Path
import re
from typing import Callable

from pydantic import BaseModel
from pydantic_ai.messages import ModelMessagesTypeAdapter

from auction_game.ai.agent_execution import CallFailedError, ExecutionResult
from auction_game.reporting.request_records import RequestWriter


def _inputs(entry: dict) -> list[str]:
    return [part["content"] if isinstance(part["content"], str)
            else json.dumps(part["content"], ensure_ascii=False)
            for message in entry["messages"] for part in message["parts"]
            if part["part_kind"] in ("system-prompt", "user-prompt")]


def _phase_title(entry: dict) -> str:
    phase = entry["phase"]
    if phase == "bid":
        return f"{entry.get('context', '')} · 每日出价".strip(" ·")
    match = re.fullmatch(r"match_(\d+)_(speech|plan)", phase)
    if match:
        index, task = match.groups()
        return f"第{index}局局末发言" if task == "speech" else f"第{int(index) + 1}局策略规划（含首日出价）"
    return {"opening_speech": "开局发言", "opening_plan": "第1局策略规划（含首日出价）",
            "final_speech": "最终感言", "final_summary": "观众总结（不回喂）"}.get(
                phase, entry.get("context") or "模型任务")


def _error_reason(entry: dict) -> str:
    error = entry["error"]
    if error["type"] == "OutputFailedError":
        return "输出校验失败（OutputFailedError）"
    latest = entry["request_errors"][-1] if entry["request_errors"] else error
    kind, message = latest["type"], latest["message"]
    status = re.search(r"(?:status_code[=: ]+|Error code: )(\d{3})", message)
    if status:
        code = int(status[1])
        reason = {400: "请求被拒绝", 401: "鉴权失败", 403: "访问被拒绝", 408: "请求超时",
                  429: "服务限流或额度不足"}.get(code, "服务端故障" if code >= 500 else "请求失败")
        return f"{reason}（HTTP {code}）"
    if "Timeout" in kind or "Timeout" in message:
        return "请求超时"
    if kind == "IncompleteStream" or "IncompleteStream" in message:
        return "响应截断或缺少成功结束标记"
    if "Connection" in kind or "Network" in kind:
        return "网络连接失败"
    return html.escape(f"{kind}：{' '.join(message.split())[:160]}")


def _attempts(entry: dict) -> tuple[str, int]:
    """响应代表完成的网络尝试；错误各代表一次失败，不把末尾纠错提示算作请求。"""
    responses = sum(message["kind"] == "response" for message in entry["messages"])
    errors = entry["request_errors"]
    requests = max(responses, max((error["request"] for error in errors), default=0))
    counts = []
    for number in range(1, requests + 1):
        failed = [error["attempt"] for error in errors if error["request"] == number]
        counts.append(max(failed, default=0) + (number <= responses))
    total = sum(counts)
    text = f"网络尝试 {total} 次" if total else "网络尝试次数未记录"
    if len(counts) > 1 or errors:
        text += "（" + "；".join(f"请求 {i}：{count} 次" for i, count in enumerate(counts, 1)) + "）"
    return text, max(0, requests - 1)


def _correction_reason(entry: dict) -> str:
    """只摘取首次纠错提示的原因，不展示校验错误内的 input/ctx 或无效策略。"""
    content = next((part.get("content") for message in entry["messages"] for part in message["parts"]
                    if part["part_kind"] == "retry-prompt"), None)
    if isinstance(content, list):
        reasons = []
        for error in content[:2]:
            field = ".".join(str(value) for value in error.get("loc", ()))
            reason = {"int_type": "必须是整数", "missing": "缺少必填字段",
                      "extra_forbidden": "包含多余字段", "string_type": "必须是文本",
                      "string_too_short": "文本不能为空"}.get(error.get("type"), error.get("msg", "格式无效"))
            reasons.append(f"{field} {reason}".strip())
        reason = "；".join(reasons)
    elif isinstance(content, str):
        unknown = re.search(r"Unknown tool name: ['\"]([^'\"]+)['\"]", content)
        available = re.search(r"Available tools: ['\"]([^'\"]+)['\"]", content)
        if unknown:
            reason = f"误用工具 {unknown[1]}"
            if available:
                reason += f"，提示改用 {available[1]}"
        else:
            reason = content
    else:
        reason = "输出未通过校验，已要求重新提交"
    reason = " ".join(reason.split())
    return html.escape(reason[:100] + ("…" if len(reason) > 100 else ""))


def render_result(result) -> list[str]:
    return ["### 结果\n\n" + (result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))]


def render_markdown_entry(entry: dict, result_renderer: Callable = render_result, *,
                          audit_link: str = "", first_task: str | None = None,
                          previous_task: str | None = None, reruns: int = 0) -> str:
    """正文仅投影输入及最终业务结果；工具协议与完整错误留在机器审计。"""
    task_id = entry["task_id"]
    success = entry["status"] == "success"
    lines = [f'<a id="{task_id}"></a>', f"## {_phase_title(entry)}",
             f"任务：{task_id}；状态：{'成功' if success else '失败'}；任务补跑 {reruns} 次"]
    if first_task:
        lines.append(f"输入沿用[首次任务 {first_task}](#{first_task})；"
                     f"接续[上次记录 {previous_task}](#{previous_task})。")
    else:
        inputs = _inputs(entry)
        if inputs:
            # 软换行不改原文；过滤内联样式的客户端仍可能横向滚动。
            # code 隔开 pre 的起始位置，避免 HTML 吞掉原文开头的首个换行。
            blocks = "\n".join('<pre style="white-space: pre-wrap; overflow-wrap: anywhere;">'
                               '<code style="white-space: inherit; overflow-wrap: inherit;">'
                               f"{html.escape(content)}</code></pre>" for content in inputs)
            lines.append(f"<details>\n<summary>输入原文</summary>\n\n{blocks}\n\n</details>")
    attempts, corrections = _attempts(entry)
    lines.append(attempts + "。")
    if corrections:
        lines.append(f"输出纠错 {corrections} 次，{'最终通过校验' if success else '最终失败'}"
                     f"（原因：{_correction_reason(entry)}）。")
    if not success:
        lines.append(f"> 失败原因：{_error_reason(entry)}。")
    if "result" in entry:
        lines.extend(result_renderer(entry["result"]))
    lines.append(f"完整审计：[{task_id}]({audit_link})（按 task_id 定位）。" if audit_link
                 else f"完整审计任务编号：{task_id}。")
    return "\n\n".join(lines) + "\n\n---\n\n"


class AgentLog:
    """同步写 JSONL 后写 Markdown；任一失败原样抛出。"""

    def __init__(self, name: str, path: Path | None, *, readable_path=None, capture_path=None,
                 result_renderer: Callable = render_result):
        self.name = name
        self.path = path
        self.readable_path = readable_path
        self.capture = RequestWriter(capture_path)
        self.result_renderer = result_renderer
        self._task_number = 0
        self._failed_tasks: dict[tuple, tuple[str, str, int]] = {}

    def success(self, entry: dict, result: ExecutionResult) -> None:
        self._write({**entry, **({"requests": result.requests} if result.requests else {}), "status": "success",
                     "messages": ModelMessagesTypeAdapter.dump_python(result.messages, mode="json"),
                     "request_errors": result.request_errors,
                     "result": result.output.model_dump() if isinstance(result.output, BaseModel) else result.output})

    def failure(self, entry: dict, error: CallFailedError) -> None:
        self._write({**entry, **({"requests": error.requests} if error.requests else {}), "status": "failed",
                     "messages": ModelMessagesTypeAdapter.dump_python(error.messages, mode="json"),
                     "request_errors": error.request_errors,
                     "error": {"type": type(error).__name__, "message": str(error)}})

    def _write(self, entry: dict) -> None:
        if self.path is None:
            return
        self._task_number += 1
        entry = {"task_id": f"task-{self._task_number:04d}", **entry}
        requests = entry.pop("requests", [])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.capture.append(entry, requests)
        md_path = self.readable_path
        if md_path is None:
            return
        md_path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not md_path.exists()
        # 只关联连续失败任务，成功后结束关联；不读取或重写已有 Markdown。
        key = (entry["phase"], entry["task"], entry.get("context", ""), tuple(_inputs(entry)))
        first_task, previous_task, reruns = self._failed_tasks.get(key, (None, None, 0))
        audit_link = Path(os.path.relpath(self.path, md_path.parent)).as_posix()
        with md_path.open("a", encoding="utf-8") as file:
            if is_new:
                file.write(f"# {self.name} · 私人对话（含私有策略）\n\n")
            file.write(render_markdown_entry(entry, self.result_renderer, audit_link=audit_link,
                                            first_task=first_task, previous_task=previous_task, reruns=reruns))
        if entry["status"] == "failed":
            self._failed_tasks[key] = (first_task or entry["task_id"], entry["task_id"], reruns + 1)
        else:
            self._failed_tasks.pop(key, None)
