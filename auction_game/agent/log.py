"""保存完整任务记录，并生成便于阅读的私人日志；补跑关系保存在 JSON 中。"""

from __future__ import annotations

import json
import html
from pathlib import Path
import re

from pydantic_ai.messages import ModelMessagesTypeAdapter

from auction_game.llm.execution import CallFailedError, ExecutionResult, TaskCancelledError
from auction_game.agent.tasks import render_task_result


def _inputs(entry: dict) -> list[str]:
    return [part["content"] for message in entry["messages"] for part in message["parts"]
            if part["part_kind"] in ("system-prompt", "user-prompt")]


def _phase_title(entry: dict) -> str:
    phase = entry["phase"]
    if phase == "bid":
        return f"{entry.get('context', '')} · 每日出价".strip(" ·")
    match = re.fullmatch(r"match_(\d+)_(speech|plan|memory)", phase)
    if match:
        index, task = match.groups()
        if task == "memory":
            return f"第{index}局累计私有记忆"
        return f"第{index}局局末发言" if task == "speech" else f"第{int(index) + 1}局策略规划（含首日出价）"
    return {"opening_speech": "开局发言", "opening_plan": "第1局策略规划（含首日出价）",
            "final_speech": "最终感言", "final_summary": "观众总结（仅供观众）"}[phase]


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
    if kind == "IncompleteStream":
        return html.escape(f"响应未正常结束（{message}）")
    if "Connection" in kind or "Network" in kind:
        return "网络连接失败"
    return html.escape(f"{kind}：{' '.join(message.split())[:160]}")


def _correction_reason(entry: dict) -> str:
    """只取首次纠错提示的第一行；后续行是完整重交模板。"""
    content = next(part["content"] for message in entry["messages"] for part in message["parts"]
                   if part["part_kind"] == "retry-prompt")
    reason = " ".join(content.split("\n", 1)[0].split())
    return html.escape(reason[:100] + ("…" if len(reason) > 100 else ""))


def render_markdown_entry(entry: dict, title: str,
                          first_title: str | None = None, previous_title: str | None = None) -> str:
    """正文只展示输入和最终结果；原始回复及完整错误保存在 JSON 记录中。

    标题只含中文和数字，链接锚点在各 Markdown 阅读器里一致。
    """
    status = entry["status"]
    lines = [f"## {title}"]
    if entry.get("reruns", 0):
        lines.append({"success": "补跑后成功。", "failed": "补跑仍失败。", "cancelled": "补跑时被取消。"}[status])
    if first_title:
        lines.append(f"输入与[首次输入](#{first_title})相同；上次失败见[{previous_title}](#{previous_title})。")
    else:
        inputs = _inputs(entry)
        if inputs:
            lines.append("### 输入原文\n\n" + "\n\n".join(inputs))
    corrections = sum(part["part_kind"] == "retry-prompt"
                      for message in entry["messages"] for part in message["parts"])
    if corrections:
        outcome = {"success": "已通过", "failed": "仍失败", "cancelled": "未完成"}[status]
        lines.append(f"纠错 {corrections} 次，{outcome}（{_correction_reason(entry)}）。")
    if status == "failed":
        lines.append(f"> 失败原因：{_error_reason(entry)}。")
    elif status == "cancelled":
        lines.append("> 已取消：用户按 q 退出比赛，请求被中断。")
    if "result" in entry:
        lines.extend(render_task_result(entry["result"]))
    if "history_note" in entry:
        lines.append(entry["history_note"])
    return "\n\n".join(lines) + "\n\n---\n\n"


class AgentLog:
    """同步写 JSONL 后写 Markdown；写入异常向上传递。"""

    def __init__(self, name: str, path: Path, *, readable_path: Path):
        self.name = name
        self.path = path
        self.readable_path = readable_path
        self._task_number = 0
        self._failed_tasks: dict[tuple, tuple[str, str, int]] = {}
        self._titles: dict[str, str] = {}
        self._title_counts: dict[str, int] = {}

    def system_prompt(self, text: str) -> None:
        """在任务记录之前写一次系统提示原文。"""
        self._append({"task": "system", "content": text}, f"## 系统提示\n\n{text}\n\n---\n\n")

    def success(self, entry: dict, result: ExecutionResult) -> None:
        self._write({**entry, "status": "success",
                     "messages": ModelMessagesTypeAdapter.dump_python(result.messages, mode="json"),
                     "request_errors": result.request_errors,
                     "result": result.output})

    def failure(self, entry: dict, error: CallFailedError) -> None:
        self._write({**entry, "status": "failed",
                     "messages": ModelMessagesTypeAdapter.dump_python(error.messages, mode="json"),
                     "request_errors": error.request_errors,
                     "error": {"type": type(error).__name__, "message": str(error)}})

    def cancelled(self, entry: dict, error: TaskCancelledError) -> None:
        self._write({**entry, "status": "cancelled",
                     "messages": ModelMessagesTypeAdapter.dump_python(error.messages, mode="json"),
                     "request_errors": error.request_errors})

    def _write(self, entry: dict) -> None:
        self._task_number += 1
        entry = {"task_id": f"task-{self._task_number:04d}", **entry}
        # 只关联相同输入的连续失败；成功后结束关联。
        key = (entry["phase"], entry["task"], entry.get("context", ""), tuple(_inputs(entry)))
        first_task, previous_task, reruns = self._failed_tasks.get(key, (None, None, 0))
        entry.update(first_task=first_task, previous_task=previous_task, reruns=reruns)
        title = re.sub(r"[^\w\u4e00-\u9fff]", "", _phase_title(entry))
        if reruns:
            title += f"补跑{reruns}"
        count = self._title_counts.get(title, 0) + 1
        self._title_counts[title] = count
        title = title if count == 1 else f"{title}记录{count}"
        self._titles[entry["task_id"]] = title
        self._append(entry, render_markdown_entry(entry, title, self._titles.get(first_task),
                                                  self._titles.get(previous_task)))
        if entry["status"] == "failed":
            self._failed_tasks[key] = (first_task or entry["task_id"], entry["task_id"], reruns + 1)
        else:
            self._failed_tasks.pop(key, None)

    def _append(self, record: dict, markdown: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.readable_path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self.readable_path.exists()
        with self.readable_path.open("a", encoding="utf-8") as file:
            if is_new:
                file.write(f"# {self.name} · 私人对话（含私有策略）\n\n")
            file.write(markdown)
