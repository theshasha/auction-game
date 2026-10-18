"""文本提交协议：长正文保留换行，只有出价字段使用 JSON。"""

from __future__ import annotations

import json
import re


TASK_FORMATS = {
    "bid": '<strategy>\n你的私人策略，可正常分段。\n</strategy>\n'
           '<decision>\n{"bid": 2, "super_bid": null}\n</decision>',
    "speech": '<strategy>\n你的私人发言策略。\n</strategy>\n'
              '<speech>\n向其他玩家公开的完整发言。\n</speech>',
}
TASK_FORMATS["plan"] = TASK_FORMATS["bid"]
_MARKER = re.compile(r"^[ \t]*(</?(?:strategy|decision|speech)>)[ \t]*\r?$", re.MULTILINE)


class OutputFormatError(ValueError):
    """可由模型重新提交修正的格式错误。"""


def output_requirement(task: str) -> str:
    if task in ("memory", "summary"):
        return "请直接返回完整正文，正常换行，不加格式标签或 JSON 外壳。"
    hint = "请按以下格式提交完整结果，标签各占一行，正文正常换行，块外只留空白：\n"
    if task in ("bid", "plan"):
        hint += "decision 只包含 bid 和 super_bid；两者必填，使用整数，不使用超级出价时填 JSON null。\n"
    return hint + TASK_FORMATS[task]


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise OutputFormatError(f"decision 含重复字段 {key}。")
        result[key] = value
    return result


def _invalid_constant(value: str):
    raise OutputFormatError(f"decision 含非法 JSON 数值 {value}。")


def parse_text_output(task: str, text: str) -> dict | str:
    """完整匹配协议边界，只裁剪字段两端空白，保留正文内部原文。"""
    if task in ("memory", "summary"):
        body = text.strip()
        if not body:
            raise OutputFormatError("正文不能为空白。")
        return {"memory": body} if task == "memory" else body

    second = "decision" if task in ("bid", "plan") else "speech"
    expected = ["<strategy>", "</strategy>", f"<{second}>", f"</{second}>"]
    markers = list(_MARKER.finditer(text))
    if [m.group(1) for m in markers] != expected:
        raise OutputFormatError(f"必须依次提交 strategy 与 {second} 两块，标签各占一行且各出现一次。")
    if (text[:markers[0].start()].strip()
            or text[markers[1].end():markers[2].start()].strip()
            or text[markers[3].end():].strip()):
        raise OutputFormatError("协议块外只允许空白，请直接提交两块正文。")
    strategy = text[markers[0].end():markers[1].start()].strip()
    body = text[markers[2].end():markers[3].start()].strip()
    if not strategy or not body:
        raise OutputFormatError(f"strategy 与 {second} 都必须有非空正文。")
    if second == "speech":
        return {"strategy": strategy, "speech": body}
    try:
        decision = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except OutputFormatError:
        raise
    except (ValueError, RecursionError) as exc:
        raise OutputFormatError("decision 必须是一个完整 JSON 对象，不加代码围栏或尾随正文。") from exc
    if not isinstance(decision, dict) or set(decision) != {"bid", "super_bid"}:
        raise OutputFormatError("decision 必须且只能包含 bid 和 super_bid 两个字段。")
    return {"strategy": strategy, **decision}
