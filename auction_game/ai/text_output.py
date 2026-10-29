"""文本提交协议：长正文保留换行，只有出价字段使用 JSON。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re


TASK_FORMATS = {
    "bid": '<strategy>\n仅自己可见的出价分析和策略，可正常分段。\n</strategy>\n'
           '<decision>\n{"bid": 正常出价, "super_bid": 超级出价或 null}\n</decision>',
    "speech": '<strategy>\n仅自己可见的发言策略。\n</strategy>\n'
              '<speech>\n其他玩家都能看到的公开发言。\n</speech>',
}
TASK_FORMATS["plan"] = TASK_FORMATS["bid"]
LETTER_FORMAT = "<收件人名字>\n只有收件人能看到的私信。\n</收件人名字>"
MAX_LETTERS = 2
# 第 2 天起的出价只附这一句，完整格式在本局规划请求里。
BRIEF_BID_FORMAT = "请按本局规划时的格式提交：strategy 和 decision 两块，私信块可以不写。"
# 读取阵容时，玩家名也不能和这些标签相同，原因见 docs/letters.md 的"名字"。
FIXED_TAGS = ("strategy", "decision", "speech")


@dataclass(frozen=True)
class LetterNames:
    """可以写信的任务用的名单：本人和其他玩家，按配置顺序。"""

    sender: str
    recipients: tuple[str, ...]


class OutputFormatError(ValueError):
    """可由模型重新提交修正的格式错误。"""


def output_requirement(task: str, letters: LetterNames | None = None) -> str:
    """完整格式说明；传了名单时加私信块，不传表示这个任务不能写信。"""
    if task in ("memory", "summary"):
        return "请直接返回完整正文，正常换行，不加格式标签或 JSON 外壳。"
    hint = "请按以下格式提交完整结果，标签各占一行，块外只留空白：\n"
    if task in ("bid", "plan"):
        hint += "decision 是只含 bid 和 super_bid 的 JSON 对象，出价写不加引号的整数，不使用超级出价时填 null。\n"
    if letters is None:
        return hint + TASK_FORMATS[task]
    hint += (f"私信块可以不写，最多 {MAX_LETTERS} 块。标签是收件人的名字，可以写给{'、'.join(letters.recipients)}，"
             "每人最多 1 封，每封 200 字左右。\n")
    return f"{hint}{TASK_FORMATS[task]}\n{LETTER_FORMAT}"


def _marker_pattern(tags: tuple[str, ...]) -> re.Pattern:
    """单独占一行的开始或结束标签；第 1 组是结束标签的斜杠，第 2 组是标签名。"""
    names = "|".join(re.escape(tag) for tag in tags)
    return re.compile(rf"^[ \t]*<(/?)({names})>[ \t]*\r?$", re.MULTILINE)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise OutputFormatError(f"decision 含重复字段 {key}。")
        result[key] = value
    return result


def parse_text_output(task: str, text: str, letters: LetterNames | None = None) -> dict | str:
    """完整匹配协议边界，只裁剪字段两端空白，保留正文内部原文。

    传了名单时，本场玩家名的标签也算块的边界，包括本人，用来报"写给自己"。
    """
    if task in ("memory", "summary"):
        body = text.strip()
        if not body:
            raise OutputFormatError("正文不能为空白。")
        return {"memory": body} if task == "memory" else body

    second = "decision" if task in ("bid", "plan") else "speech"
    names = () if letters is None else (letters.sender, *letters.recipients)
    markers = list(_marker_pattern((*FIXED_TAGS, *names)).finditer(text))
    expected = [("", "strategy"), ("/", "strategy"), ("", second), ("/", second)]
    if ([marker.groups() for marker in markers[:4]] != expected
            or any(marker[2] in FIXED_TAGS for marker in markers[4:])):
        if letters is None:
            raise OutputFormatError(f"必须依次提交 strategy 与 {second} 两块，标签各占一行且各出现一次。")
        raise OutputFormatError(f"必须先依次提交 strategy 与 {second} 两块，标签各占一行且各出现一次，私信块放在最后。")
    letter_markers = markers[4:]
    pairs = list(zip(letter_markers[::2], letter_markers[1::2]))
    if len(letter_markers) % 2 or any(start[1] or not end[1] or start[2] != end[2] for start, end in pairs):
        raise OutputFormatError("每个私信块以 <名字> 开始、以同一名字的 </名字> 结束，标签各占一行。")
    blocks = [(markers[0], markers[1]), (markers[2], markers[3]), *pairs]
    gaps = [text[:markers[0].start()], text[blocks[-1][1].end():],
            *(text[before[1].end():after[0].start()] for before, after in zip(blocks, blocks[1:]))]
    if any(gap.strip() for gap in gaps):
        if letters is None:
            raise OutputFormatError("协议块外只允许空白，请直接提交两块正文。")
        raise OutputFormatError(f"协议块外只允许空白。私信块的标签必须是其他玩家的名字：{'、'.join(letters.recipients)}。")

    written = [{"to": start[2], "text": text[start.end():end.start()].strip()} for start, end in pairs]
    recipients = [letter["to"] for letter in written]
    if letters is not None and letters.sender in recipients:
        raise OutputFormatError("私信不能写给自己。")
    if len(written) > MAX_LETTERS:
        raise OutputFormatError(f"最多写 {MAX_LETTERS} 封私信。")
    repeated = next((name for name in recipients if recipients.count(name) > 1), None)
    if repeated is not None:
        raise OutputFormatError(f"每名玩家最多写 1 封私信，写给{repeated}的有 {recipients.count(repeated)} 封。")
    empty = next((letter["to"] for letter in written if not letter["text"]), None)
    if empty is not None:
        raise OutputFormatError(f"写给{empty}的私信正文不能为空。")

    strategy = text[markers[0].end():markers[1].start()].strip()
    body = text[markers[2].end():markers[3].start()].strip()
    if not strategy or not body:
        raise OutputFormatError(f"strategy 与 {second} 都必须有非空正文。")
    if second == "speech":
        return {"strategy": strategy, "speech": body, "letters": written}
    try:
        decision = json.loads(body, object_pairs_hook=_unique_object)
    except OutputFormatError:  # 保留重复字段的具体报错
        raise
    except (ValueError, RecursionError) as exc:  # 嵌套过深的 JSON 只让本任务纠错，不中断比赛
        raise OutputFormatError("decision 必须是一个完整 JSON 对象，不加代码围栏或尾随正文。") from exc
    if not isinstance(decision, dict) or set(decision) != {"bid", "super_bid"}:
        raise OutputFormatError("decision 必须且只能包含 bid 和 super_bid 两个字段。")
    return {"strategy": strategy, **decision, "letters": written}
