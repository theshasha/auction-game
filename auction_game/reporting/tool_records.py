"""默认轻量工具审计：请求前写入，按协议与原始工具字段去重。"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from importlib.metadata import version
import json
from threading import Lock

from auction_game.reporting.request_records import digest


def protocol_tools(body):
    """只取 SDK 实际发送的字段；保留缺失、null 与 false 的区别。"""
    return {key: body[key] for key in ("tools", "tool_choice", "parallel_tool_calls") if key in body}


SDK_VERSIONS = {name: version(name) for name in ("pydantic-ai-slim", "openai", "anthropic")}
current_tool_task = ContextVar("tool_audit_task", default=None)
current_request_attempt = ContextVar("tool_audit_request_attempt", default=None)


@dataclass
class ToolTask:
    writer: object
    player: str
    number: int
    entry: dict
    write_error: Exception | None = None

    def record(self, protocol, model, definition, *, request, attempt, source="http_pre_send"):
        try:
            key = self.writer.record({"player": self.player, "number": self.number,
                                       **{key: self.entry[key] for key in ("task_id", "task", "phase", "context")},
                                       "request": request, "attempt": attempt, "source": source,
                                       "model": model, "versions": SDK_VERSIONS}, protocol, definition)
            definitions = self.entry.setdefault("tool_definitions", [])
            if key not in definitions:
                definitions.append(key)
            return key
        except Exception as exc:
            self.write_error = exc
            raise


@contextmanager
def tool_task(writer, player, number, entry):
    token = current_tool_task.set(ToolTask(writer, player, number, entry) if writer else None)
    try:
        yield
    finally:
        current_tool_task.reset(token)


async def record_http_tools(request):
    """公开 HTTP request hook；只提取 JSON 工具字段，不保留请求对象。"""
    task = current_tool_task.get()
    position = current_request_attempt.get()
    if task is not None and position is not None:
        protocol, model, logical, attempt = position
        task.record(protocol, model, protocol_tools(json.loads(request.content)),
                    request=logical, attempt=attempt)


class ToolWriter:
    """每场共享一个写入器；锁覆盖定义、引用及可读页，写入错误直接传播。"""

    def __init__(self, paths):
        self.paths = paths
        self.lock = Lock()
        self.definitions = {}
        self.references = []
        if not paths.tool_definitions.exists():
            paths.tool_definitions.write_text("{}\n", encoding="utf-8")
        if not paths.tool_requests.exists():
            paths.tool_requests.write_text("", encoding="utf-8")
        self._render()

    def record(self, reference, protocol, definition):
        value = {"protocol": protocol, "definition": definition}
        key = digest(value)
        reference = {**reference, "definition_id": key}
        with self.lock:
            if key not in self.definitions:
                definitions = {**self.definitions, key: value}
                self.paths.tool_definitions.write_text(
                    json.dumps(definitions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                self.definitions = definitions
            with self.paths.tool_requests.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(reference, ensure_ascii=False) + "\n")
            self.references.append(reference)
            self._render()
        return key

    def _render(self):
        lines = ["# 本场工具定义", "工具字段原样保存，按协议与内容去重；失败与重发保留引用。无鉴权头或完整请求。",
                 "来源：`http_pre_send` = 发送前实际请求定义；`offline_sdk_generated` = 离线 SDK 生成（FunctionModel，无真实服务调用）。",
                 "引用中的 task_id 对应玩家 JSONL；request / attempt 为逻辑请求 / 网络尝试序号。",
                 "[结构化定义](data/tool_definitions.json) · [请求引用与模型/SDK 来源](data/tool_requests.jsonl)"]
        if not self.references:
            lines.append("尚无请求记录。")
        for key, value in self.definitions.items():
            tools = value["definition"].get("tools") or []
            names = " / ".join(tool.get("function", tool)["name"] for tool in tools) or "文本任务"
            lines.extend([f'<a id="{key}"></a>', f"## {names} · {value['protocol']} · `{key[:12]}`"])
            if not value["definition"].get("tools"):
                lines.append("无输出工具：任务直接返回文本。")
            lines.append("```json\n" + json.dumps(value["definition"], ensure_ascii=False, indent=2) + "\n```")
            lines.append("使用引用：")
            for ref in self.references:
                if ref["definition_id"] == key:
                    lines.append(f"- [{ref['player']}](data/players/{ref['number']:03d}.jsonl) · "
                                 f"`{ref['task_id']}` · {ref['task']} / {ref['phase']} / {ref['context']} · "
                                 f"request {ref['request']} / attempt {ref['attempt']} · "
                                 f"{ref['source']} · `{ref['model']}`")
        self.paths.tools.write_text("\n\n".join(lines) + "\n", encoding="utf-8")
