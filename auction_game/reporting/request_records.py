"""可选捕获：请求前缀增量与定义引用，校验后无损重建真实边界输入。"""

from copy import deepcopy
import hashlib
import json


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _message_ref(message, entries):
    for entry in reversed(entries):
        for index, candidate in enumerate(entry["messages"]):
            if digest(candidate.get("parts")) == digest(message.get("parts")):
                return {"task": entry["task_id"], "message": index,
                        "set": {key: value for key, value in message.items() if digest(candidate.get(key)) != digest(value)
                                or key not in candidate},
                        "remove": [key for key in candidate if key not in message]}
    return {"value": message}


def _resolve_message(ref, entries):
    if "value" in ref:
        return deepcopy(ref["value"])
    try:
        result = deepcopy(entries[ref["task"]]["messages"][ref["message"]])
    except (KeyError, IndexError) as exc:
        raise ValueError(f"请求记录缺少消息引用：{ref}") from exc
    for key in ref["remove"]:
        result.pop(key, None)
    result.update(deepcopy(ref["set"]))
    return result


class RequestWriter:
    def __init__(self, path):
        self.path = path
        self.previous = []
        self.definitions = {}
        self.entries = []

    def append(self, entry, requests):
        if self.path is None or not requests:
            return
        self.entries.append(entry)
        rows = []
        for record in requests:
            definitions = {}
            refs = {}
            for field in ("parameters", "settings"):
                value = record[field]
                key = digest(value)
                if key not in self.definitions:
                    definitions[key] = value
                    self.definitions[key] = value
                refs[field] = key
            messages = record["messages"]
            prefix = 0
            for before, after in zip(self.previous, messages):
                if digest(before) != digest(after):
                    break
                prefix += 1
            saved = {"task_id": entry["task_id"], "request": record["request"],
                     "definitions": definitions, **refs, "prefix": prefix,
                     "tail": [_message_ref(message, self.entries) for message in messages[prefix:]],
                     "messages_hash": digest(messages)}
            if "response" in record:
                saved["response"] = _message_ref(record["response"], self.entries)
                saved["response_hash"] = digest(record["response"])
            if "errors" in record:
                saved["error_refs"] = [entry["request_errors"].index(error) for error in record["errors"]]
            # 每次写盘前独立重建并验证，null/false/缺失字段均参与比较。
            rebuilt = self.previous[:prefix] + [_resolve_message(ref, {e["task_id"]: e for e in self.entries})
                                               for ref in saved["tail"]]
            if digest(rebuilt) != saved["messages_hash"]:
                raise ValueError("请求增量重建校验失败")
            if "response" in record and digest(_resolve_message(saved["response"], {e["task_id"]: e for e in self.entries})) != saved["response_hash"]:
                raise ValueError("响应引用重建校验失败")
            self.previous = deepcopy(messages)
            rows.append(saved)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_requests(path, entries):
    """按原顺序恢复实际请求；缺引用或损坏直接报错，不猜历史。"""
    by_task = {entry["task_id"]: entry for entry in entries}
    definitions = {}
    previous = []
    for entry in entries:
        entry["requests"] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise ValueError("缺少完整模型边界捕获；启动时需指定 record_player") from exc
    for line in lines:
        row = json.loads(line)
        for key, value in row["definitions"].items():
            if digest(value) != key or (key in definitions and definitions[key] != value):
                raise ValueError("请求定义校验失败")
            definitions[key] = value
        try:
            entry = by_task[row["task_id"]]
            if not 0 <= row["prefix"] <= len(previous):
                raise ValueError("请求历史前缀超界")
            messages = previous[:row["prefix"]] + [_resolve_message(ref, by_task) for ref in row["tail"]]
            if digest(messages) != row["messages_hash"]:
                raise ValueError("请求历史校验失败")
            record = {"request": row["request"], "messages": deepcopy(messages),
                      "parameters": deepcopy(definitions[row["parameters"]]),
                      "settings": deepcopy(definitions[row["settings"]])}
            if "response" in row:
                record["response"] = _resolve_message(row["response"], by_task)
                if digest(record["response"]) != row["response_hash"]:
                    raise ValueError("响应引用校验失败")
            if "error_refs" in row:
                record["errors"] = [entry["request_errors"][index] for index in row["error_refs"]]
            entry["requests"].append(record)
            previous = messages
        except (KeyError, IndexError) as exc:
            raise ValueError(f"请求记录缺少引用：{exc}") from exc
    return entries
