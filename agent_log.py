"""任务审计写入及规范消息 Markdown 投影；不保存会话状态。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from pydantic import BaseModel
from pydantic_ai.messages import ModelMessagesTypeAdapter

from agent_execution import CallFailedError, ExecutionResult


def render_messages(messages: list[dict]) -> list[str]:
    """通用规范消息投影，保留工具 ID 与纠错关联。"""
    lines = []
    for message in messages:
        lines.append(f"### {message['kind']}")
        for part in message["parts"]:
            kind = part["part_kind"]
            content = part.get("content", part.get("args", ""))
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            label = " / ".join(str(part[k]) for k in ("tool_name", "tool_call_id") if part.get(k))
            lines.append(f"**{kind}{': ' + label if label else ''}**\n\n{content}")
    return lines


def render_result(result) -> list[str]:
    return ["### 结果\n\n" + (result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))]


def render_markdown_entry(entry: dict, result_renderer: Callable = render_result) -> str:
    """通用审计展示；业务结果展示由调用方提供。"""
    lines = [f"## {entry['phase']}", f"任务：{entry['task']}；状态：{entry['status']}"]
    if entry.get("context"):
        lines.append(entry["context"])
    lines.extend(render_messages(entry["messages"]))
    for error in entry["request_errors"]:
        lines.append(f"> 请求 {error['request']} / 尝试 {error['attempt']}：{error['type']}: {error['message']}")
    if "error" in entry:
        lines.append(f"> {entry['error']['type']}: {entry['error']['message']}")
    if "result" in entry:
        lines.extend(result_renderer(entry["result"]))
    return "\n\n".join(lines) + "\n\n---\n\n"


class AgentLog:
    """同步写 JSONL 后写 Markdown；任一失败原样抛出。"""

    def __init__(self, name: str, path: Path | None, *, result_renderer: Callable = render_result):
        self.name = name
        self.path = path
        self.result_renderer = result_renderer

    def success(self, entry: dict, result: ExecutionResult) -> None:
        self._write({**entry, "status": "success",
                     "messages": ModelMessagesTypeAdapter.dump_python(result.messages, mode="json"),
                     "request_errors": result.request_errors,
                     "result": result.output.model_dump() if isinstance(result.output, BaseModel) else result.output})

    def failure(self, entry: dict, error: CallFailedError) -> None:
        self._write({**entry, "status": "failed",
                     "messages": ModelMessagesTypeAdapter.dump_python(error.messages, mode="json"),
                     "request_errors": error.request_errors,
                     "error": {"type": type(error).__name__, "message": str(error)}})

    def _write(self, entry: dict) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        md_path = self.path.with_suffix(".md")
        is_new = not md_path.exists()
        with md_path.open("a", encoding="utf-8") as file:
            if is_new:
                file.write(f"# {self.name} 对话记录\n\n")
            file.write(render_markdown_entry(entry, self.result_renderer))
