"""保存完整任务记录，并生成便于阅读的私人日志；补跑关系保存在 JSON 中。续跑时打开已有日志，成功记录按顺序取用。"""

from __future__ import annotations

import json
import html
from pathlib import Path
import re

from pydantic_ai.messages import ModelMessagesTypeAdapter

from auction_game.agent.tasks import TASKS
from auction_game.llm.execution import CallFailedError, ExecutionResult, TaskCancelledError


class ResumeError(Exception):
    """这场比赛续不上：场次还没开始，或日志和这次重走流程的结果对不上。"""


def logged_inputs(entry: dict) -> list[str]:
    """任务记录里发给模型的输入原文；成功记录只有一条，是当次的完整请求文本。"""
    return [part["content"] for message in entry["messages"] for part in message["parts"]
            if part["part_kind"] in ("system-prompt", "user-prompt")]


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


def render_markdown_entry(entry: dict, title: str, first_title: str | None = None,
                          previous_title: str | None = None, result: list[str] = ()) -> str:
    """正文只展示输入和最终结果；原始回复及完整错误保存在 JSON 记录中。result 是结果的各个小节。

    标题只含中文和数字，链接锚点在各 Markdown 阅读器里一致。
    """
    status = entry["status"]
    lines = [f"## {title}"]
    if entry.get("reruns", 0):
        lines.append({"success": "补跑后成功。", "failed": "补跑仍失败。", "cancelled": "补跑时被取消。"}[status])
    if first_title:
        lines.append(f"输入与[首次输入](#{first_title})相同；上次失败见[{previous_title}](#{previous_title})。")
    else:
        inputs = logged_inputs(entry)
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
    lines.extend(result)
    if "history_note" in entry:
        lines.append(entry["history_note"])
    return "\n\n".join(lines) + "\n\n---\n\n"


def _system_markdown(text: str) -> str:
    return f"## 系统提示\n\n{text}\n\n---\n\n"


class AgentLog:
    """同步写 JSONL 后写 Markdown；写入异常向上传递。"""

    def __init__(self, name: str, path: Path, *, readable_path: Path):
        self.name = name
        self.path = path
        self.readable_path = readable_path
        self.system_text: str | None = None  # 续跑时日志里的系统提示；新日志为 None
        self._task_number = 0
        self._failed_tasks: dict[tuple, tuple[str, str, int]] = {}
        self._titles: dict[str, str] = {}
        self._title_counts: dict[str, int] = {}
        self._recorded: list[dict] = []  # 续跑时待取用的成功记录，按日志顺序
        self._restore: tuple[int, str] | None = None  # 续跑时 JSONL 里完整记录的字节数和可读日志全文

    @classmethod
    def open(cls, name: str, path: Path, *, readable_path: Path) -> AgentLog:
        """打开已有日志用于续跑：成功记录排队待取用，任务编号、标题和补跑关联接着原来的算。

        末尾没有换行符的半行是强关时没写完的，不算记录。这里不改文件，restore_files 才截掉半行、重写可读日志。
        """
        log = cls(name, path, readable_path=readable_path)
        data = path.read_bytes() if path.exists() else b""
        complete = data[:data.rfind(b"\n") + 1]
        # 按 \n 切：JSON 字符串里可以有 U+2028 这类 splitlines 也会切开的字符。
        records = [json.loads(line) for line in complete.decode("utf-8").split("\n") if line.strip()]
        if not records:
            raise ResumeError(f"{name} 的任务日志里还没有系统提示：这场还没开始，请重新开一场。")
        log.system_text = records[0]["content"]
        markdown = [log._heading(), _system_markdown(log.system_text)]
        for entry in records[1:]:
            log._task_number += 1
            title = log._register(entry)
            result = TASKS[entry["task"]].output.from_log(entry["result"]).render() if entry["status"] == "success" else ()
            markdown.append(render_markdown_entry(entry, title, log._titles.get(entry["first_task"]),
                                                  log._titles.get(entry["previous_task"]), result))
            if entry["status"] == "success":
                log._recorded.append(entry)
        log._restore = len(complete), "".join(markdown)
        return log

    def next_recorded(self) -> dict | None:
        """续跑时下一条待取用的成功记录；取完或不是续跑时为 None。"""
        return self._recorded[0] if self._recorded else None

    def take_recorded(self) -> None:
        del self._recorded[0]

    def restore_files(self) -> None:
        """续跑核对通过后调用一次：截掉 JSONL 末尾的半行，按全部记录重写可读日志。

        强关可能停在一行写到一半，也可能停在 JSONL 已写、Markdown 没写之间。
        """
        size, markdown = self._restore
        with self.path.open("r+b") as file:
            file.truncate(size)
        self.readable_path.parent.mkdir(parents=True, exist_ok=True)
        self.readable_path.write_text(markdown, encoding="utf-8")

    def system_prompt(self, text: str) -> None:
        """在任务记录之前写一次系统提示原文。"""
        self._append({"task": "system", "content": text}, _system_markdown(text))

    def success(self, entry: dict, result: ExecutionResult) -> None:
        self._write({**entry, "status": "success",
                     "messages": ModelMessagesTypeAdapter.dump_python(result.messages, mode="json"),
                     "request_errors": result.request_errors,
                     "result": result.output.to_log()}, result.output.render())

    def failure(self, entry: dict, error: CallFailedError) -> None:
        self._write({**entry, "status": "failed",
                     "messages": ModelMessagesTypeAdapter.dump_python(error.messages, mode="json"),
                     "request_errors": error.request_errors,
                     "error": {"type": type(error).__name__, "message": str(error)}})

    def cancelled(self, entry: dict, error: TaskCancelledError) -> None:
        self._write({**entry, "status": "cancelled",
                     "messages": ModelMessagesTypeAdapter.dump_python(error.messages, mode="json"),
                     "request_errors": error.request_errors})

    def _write(self, entry: dict, result: list[str] = ()) -> None:
        self._task_number += 1
        entry = {"task_id": f"task-{self._task_number:04d}", **entry}
        first_task, previous_task, reruns = self._failed_tasks.get(self._rerun_key(entry), (None, None, 0))
        entry.update(first_task=first_task, previous_task=previous_task, reruns=reruns)
        title = self._register(entry)
        self._append(entry, render_markdown_entry(entry, title, self._titles.get(first_task),
                                                  self._titles.get(previous_task), result))

    @staticmethod
    def _rerun_key(entry: dict) -> tuple:
        return entry["title"], entry["task"], entry["context"], tuple(logged_inputs(entry))

    def _register(self, entry: dict) -> str:
        """记下一条已带编号和补跑关联的记录，返回它在可读日志里的标题。"""
        title = re.sub(r"[^\w\u4e00-\u9fff]", "", entry["title"])
        if entry["reruns"]:
            title += f"补跑{entry['reruns']}"
        count = self._title_counts.get(title, 0) + 1
        self._title_counts[title] = count
        title = title if count == 1 else f"{title}记录{count}"
        self._titles[entry["task_id"]] = title
        # 只关联相同输入的连续失败；成功后结束关联。
        key = self._rerun_key(entry)
        if entry["status"] == "failed":
            self._failed_tasks[key] = (entry["first_task"] or entry["task_id"], entry["task_id"], entry["reruns"] + 1)
        else:
            self._failed_tasks.pop(key, None)
        return title

    def _heading(self) -> str:
        return f"# {self.name} · 私人对话（含私有策略）\n\n"

    def _append(self, record: dict, markdown: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.readable_path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self.readable_path.exists()
        with self.readable_path.open("a", encoding="utf-8") as file:
            if is_new:
                file.write(self._heading())
            file.write(markdown)
