"""从已保存的模型请求记录导出单玩家关键节点；不生成回复或修改比赛历史。"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import threading
from pathlib import Path
from importlib.metadata import version

import httpx2 as http
from pydantic import TypeAdapter
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models import ModelRequestParameters, override_allow_model_requests

from auction_game.reporting.analyze import load_match_logs
from auction_game.ai.llm_client import ModelFactory
from auction_game.game.engine import calculate_day_scores
from auction_game.game.game_rules import GameSettings
from auction_game.reporting.run_paths import RunPaths
from auction_game.reporting.request_records import load_requests, digest
from auction_game.reporting.run_output import write_readme
from auction_game.reporting.tool_records import protocol_tools, tool_task


# SDK 的请求许可是进程全局变量，离线并发捕获必须串行修改并恢复。
_capture_lock = threading.Lock()


def _json(value):
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```"


async def capture_protocol_definition(factory, record):
    """生产公开模型入口序列化一次；MockTransport 在收包后立即用 400 结束。

    无需生成回复或重跑 Agent；400 不触发生产基础设施重试。只保存必要工具定义，
    不保存 Request 对象、鉴权头或假密钥。外部工厂仅提供公开路由配置。
    """
    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        return http.Response(400, json={"error": {"type": "invalid_request_error",
                                                "message": "offline SDK capture complete"}})

    # 不采用外部传输，也不解析任何密钥；所有 HTTP 请求均由此 MockTransport 截获。
    offline = ModelFactory(factory.protocol, factory.model, factory.base_url,
                           "offline-capture-not-a-secret", factory.timeout, factory.max_tokens,
                           http.MockTransport(handle), model_settings=factory.model_settings,
                           model_profile=factory.model_profile)
    messages = ModelMessagesTypeAdapter.validate_python(record["messages"])
    parameters = TypeAdapter(ModelRequestParameters).validate_python(record["parameters"])
    while not _capture_lock.acquire(blocking=False):
        await asyncio.sleep(0.001)
    try:
        with override_allow_model_requests(True), tool_task(None, None, None, None):
            async with offline.open() as model:
                try:
                    async with model.request_stream(messages, record["settings"], parameters):
                        raise RuntimeError("离线捕获应在模拟传输边界结束")
                except ModelHTTPError as exc:
                    if exc.status_code != 400 or len(bodies) != 1:
                        raise
    finally:
        _capture_lock.release()
    return {"source": "offline_sdk_mock_transport", "protocol": factory.protocol,
             "model": factory.model, "model_profile": factory.model_profile,
             "versions": {name: version(name) for name in ("pydantic-ai-slim", "openai", "anthropic")},
             "definition": protocol_tools(bodies[0])}


def _protocol_records(entries, player, path, config, *, capture=False):
    """按定义/设置复用 SDK 捕获；已有旁路文件用于原样重新导出。"""
    if path.exists():
        snapshots = json.loads(path.read_text(encoding="utf-8"))
    else:
        if not capture:
            raise ValueError("缺少 SDK 工具定义，请显式执行 --capture-definitions 离线补捕获；不会联网或自动替换版本")
        pc = next(pc["execution_model"] for pc in config["players"] if pc["name"] == player)
        factory = ModelFactory(pc["protocol"], pc["model"],
                               pc["base_url"], "offline-capture-not-a-secret",
                               config["timeout_seconds"], pc["max_tokens"],
                               model_settings=pc.get("model_settings", {}),
                               model_profile=pc.get("model_profile", {}))
        snapshots = {}
        for entry in entries:
            for record in entry["requests"]:
                key = digest([record["parameters"], record["settings"]])
                if key not in snapshots:
                    snapshots[key] = asyncio.run(capture_protocol_definition(factory, record))
        path.write_text(json.dumps(snapshots, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for entry in entries:
        for record in entry["requests"]:
            key = digest([record["parameters"], record["settings"]])
            if key not in snapshots:
                raise ValueError("SDK 定义缺少引用；请核对捕获文件，禁止静默用新 SDK 替换")
    return snapshots


def _response_lines(response):
    """展示实际回复的业务内容，调用 ID 及规范消息对象留在调试记录。"""
    lines = []
    for part in response["parts"]:
        kind = part["part_kind"]
        if kind == "tool-call":
            args = part["args"]
            lines.append(f"调用工具 `{part['tool_name']}`：")
            # 字符串原样保留，真实无效 JSON 同样可读，不以最终合法结果替换。
            lines.append("```json\n" + args + "\n```" if isinstance(args, str) else _json(args))
        elif kind == "text":
            lines.append(part["content"])
        elif kind == "thinking":
            lines.append("思考内容：\n\n" + part["content"])
        else:
            lines.append(f"其他回复内容（{kind}）见完整任务记录，可按需导出调试文件。")
    return lines


def _entry_day(entry):
    """规划缓存用于下一局首日，日常出价的局日取实际审计上下文。"""
    if entry["task"] == "plan":
        if entry["phase"] == "opening_plan":
            return 1, 1
        match = re.fullmatch(r"match_(\d+)_plan", entry["phase"])
        if match:
            return int(match[1]) + 1, 1
    if entry["task"] == "bid":
        match = re.search(r"第(\d+)局第(\d+)天", entry["context"])
        if match:
            return tuple(map(int, match.groups()))
    return None


def _select_entries(entries, matches):
    """保留阶段骨架，每类结算事件只取首次，同一请求合并展示。"""
    phases = {"opening_speech": "比赛开头 · 开局发言",
              "opening_plan": "第一局规划 · 首日缓存",
              "match_1_speech": "第一局结束 · 局末发言",
              "match_1_memory": "第一局结束 · 累计私有记忆",
              "match_1_plan": "第二局规划 · 接收实际反馈",
              "final_speech": "实际比赛结尾 · 最终感言",
              "final_summary": "实际比赛结尾 · 观众总结"}
    selected = {i: phases[entry["phase"]] for i, entry in enumerate(entries) if entry["phase"] in phases}
    nodes = {}
    first_bid = True
    for i, entry in enumerate(entries):
        key = _entry_day(entry)
        if key is not None:
            # 失败后补跑可能使用同一局日；有结算时优先展示成功请求。
            if key not in nodes or entry["status"] == "success":
                nodes[key] = i
        if entry["task"] == "bid" and first_bid:
            title = "第一局第二天" if key == (1, 2) else entry["context"]
            selected[i] = f"{title} · 正常出价"
            first_bid = False
        if entry["status"] != "success":
            selected.setdefault(i, f"{entry['context']} · 失败审计")
    seen = set()
    for payload in matches:
        match = payload["match"]
        for day in match["days"]:
            events = []
            uses = day["super_uses"]
            if any(not use["hit"] for use in uses):
                events.append("首次超级落空")
            if any(use["hit"] for use in uses):
                events.append("首次超级命中")
            if day["is_void"]:
                events.append("首次流局")
            if match["is_playoff"] and (day["winner"] is not None or any(use["hit"] for use in uses)):
                events.append("加赛关键日")
            i = nodes.get((match["match_index"], day["day"]))
            if i is None:
                continue
            fresh = [event for event in events if event not in seen]
            if fresh:
                selected.setdefault(i, f"第{match['match_index']}局第{day['day']}天 · 出价")
                selected[i] += " · " + " / ".join(fresh)
                seen.update(fresh)
    return selected


def _settlement_lines(match, day, player):
    game = GameSettings.from_dict(match["game"])
    scores = calculate_day_scores(
        match["auction_scores"], winner=day["winner"], auction_score=day["auction_score"],
        super_uses=day["super_uses"], game=game,
    )
    result = (f"获胜者 {day['winner']}，拍卖得分 {day['auction_score']} 分。" if day["winner"] is not None
              else "当日流局，奖池作废，拍卖得分 0 分。")
    lines = [result]
    if day["super_uses"]:
        lines.append("超级出价：" + "；".join(
            f"{use['player']} 出 {use['value']}，{'命中' if use['hit'] else '落空'}，"
            f"命中得分 {game.super_hit_score if use['hit'] else 0} 分" for use in day["super_uses"]) + "。")
    lines.append("当日总分（拍卖分 + 命中分）：" + "；".join(
        f"{name} {score} 分" for name, score in scores.items() if score or name == player) + "。")
    return lines


def export_replay(run_dir: Path, player: str, *, capture_definitions=False, debug=False) -> Path:
    """读取实际日志；模拟标签取场次类型，重新导出复用原 SDK 定义。"""
    paths = RunPaths(run_dir)
    config = paths.read_config()
    number = paths.number(player)
    audit = paths.player(number)
    capture_path = paths.capture(number)
    entries = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    load_requests(paths.capture(number), entries)
    pc = next(pc for pc in config["players"] if pc["name"] == player)
    matches = load_match_logs(run_dir) if any((paths.data / "matches").glob("*.json")) else []
    days = {(payload["match"]["match_index"], day["day"]): (payload["match"], day)
            for payload in matches for day in payload["match"]["days"]}
    if not entries or not all(entry.get("requests") for entry in entries):
        raise ValueError("该玩家缺少完整模型边界记录，请启动比赛时指定 record_player")
    protocol_path = paths.definitions(number)
    snapshots = _protocol_records(entries, player, protocol_path, config, capture=capture_definitions)

    selected = _select_entries(entries, matches)
    debug_path = run_dir / "debug" / f"{number:03d}.md"
    debug_lines = [f"# {player} · 调试记录", "逻辑请求来源：运行时模型边界捕获，包含框架内部字段。",
             f"协议请求来源：[离线 SDK 捕获](../{protocol_path.relative_to(run_dir).as_posix()})；仅必要定义，无鉴权头。",
             "本场配置模型能力与有效设置：", _json(pc["execution_model"])]
    simulated = config["run_type"] == "offline"
    label = "模拟离线运行（FunctionModel，无真实服务调用）" if simulated else "已记录的模型运行"
    execution = (f"实际执行：假模型 FunctionModel；配置模型：{pc['execution_model']['protocol']} / "
                 f"{pc['execution_model']['model']}。" if simulated else
                 f"实际执行：{pc['execution_model']['protocol']} / {pc['execution_model']['model']}。")
    lines = [f"# {player} · 单玩家关键节点回放", f"> {label}。回复均为本场原始记录，未重新生成。",
               f"运行模式：{config['mode']}；身份模型：{pc['identity_model']['model']}；{execution}",
               "[本场配置与名单](data/run_config.json) · [规则](RULES.md) · [来源与审计](#来源与审计)",
               "这里只选关键节点；私有策略、思考与记忆在对局中仅本人可见。",
               "<details>\n<summary>系统规则与人设原文</summary>\n"]
    first_messages = entries[0]["requests"][0]["messages"]
    lines.extend(part["content"] for message in first_messages for part in message["parts"]
                 if part["part_kind"] == "system-prompt")
    lines.append("</details>")
    schemas = {}
    for entry_number, entry in enumerate(entries, 1):
        anchor = f"环节-{entry_number}"
        if debug:
            debug_lines.extend([f'<a id="{anchor}"></a>', f"## 环节 {entry_number}", _json(entry)])
        phase = entry["phase"]
        title = selected.get(entry_number - 1)
        if not title:
            continue
        if phase == "final_speech":
            lines.append("\n> 跳过中间流程中的其余日常请求，进入比赛结尾。")
        lines.append(f"## {title}")
        for record in entry["requests"]:
            if len(entry["requests"]) > 1:
                lines.append("### 第一次请求" if record["request"] == 1 else "### 纠正后请求")
            # 首次是实际新增 user prompt；纠错则展示本次边界最后 request 的纠错信息。
            parts = ([part for message in entry["messages"] for part in message["parts"]
                      if part["part_kind"] == "user-prompt"] if record["request"] == 1 else
                     [part for part in record["messages"][-1]["parts"] if part["part_kind"] == "retry-prompt"])
            lines.append("**本次新增提示**")
            lines.extend(part["content"] if isinstance(part["content"], str) else _json(part["content"]) for part in parts)
            capture_key = digest([record["parameters"], record["settings"]])
            snapshot = snapshots[capture_key]
            schema = snapshot["definition"]
            key = json.dumps([snapshot["protocol"], schema], ensure_ascii=False, sort_keys=True)
            if not schema.get("tools"):
                lines.append("**直接回复文本**")
                if schema:
                    lines.append(_json(schema))
            elif key not in schemas:
                label = {"speech": "发言", "plan": "规划", "bid": "出价", "memory": "记忆"}[entry["task"]]
                schema_title = f"{label}工具定义（{snapshot['protocol']}）"
                schema_anchor = f"工具定义-{len(schemas) + 1}"
                schemas[key] = (schema_title, schema_anchor)
                lines.extend([f'<a id="{schema_anchor}"></a>', f"### {schema_title}", _json(schema)])
            else:
                schema_title, schema_anchor = schemas[key]
                lines.append(f"工具：[{schema_title}](#{schema_anchor})。")
            reply_label = "第一次回复" if len(entry["requests"]) > 1 and record["request"] == 1 else "纠正后回复" if record["request"] > 1 else "回复"
            lines.append(f"**{'模拟' if simulated else '记录'}{reply_label}**")
            if "response" in record:
                lines.extend(_response_lines(record["response"]))
            else:
                lines.append("未取得完整回复，失败详情见任务记录。")
            if record.get("errors"):
                lines.append(_json(record["errors"]))
        lines.append("**程序处理**")
        if entry["status"] != "success":
            lines.extend(["任务失败，本次结果未加入玩家对话或首日出价缓存。", _json(entry.get("error"))])
        elif entry["task"] == "plan":
            lines.append("已保存私有策略与首日出价。")
            settled = days.get(_entry_day(entry))
            if settled is not None:
                match, day = settled
                bid = next(b for b in day["bids"] if b["player"] == player and not b["is_super"])
                lines.append(f"第 {match['match_index']} 局第 1 天使用已保存的出价，不重复请求模型；"
                             f"实际正常出价 {bid['bid']}。")
                lines.extend(_settlement_lines(match, day, player))
            else:
                lines.append("尚无对应首日的已结算局日志，缓存的实际执行结果待确认。")
        elif entry["task"] == "bid":
            settled = days.get(_entry_day(entry))
            lines.append("按实时余额校验后交给结算。")
            if settled is not None:
                lines.extend(_settlement_lines(*settled, player))
            else:
                lines.append("尚无对应日期的已结算局日志。")
        elif entry["task"] == "summary":
            lines.append("总结仅供观众阅读，不加入玩家对话。")
        elif entry["task"] == "memory":
            lines.append(f"已更新累计私有记忆（{entry['memory_chars']} 字符），清除历局对话。")
        else:
            lines.append("仅 speech 发给其他玩家，strategy 留在本人对话中。")
    standings = matches[-1]["standings_after_match"] if matches else None
    if standings is not None:
        result_label = "最终总分" if any(entry["phase"] == "final_speech" for entry in entries) else "截至当前总分"
        lines.extend(["## 实际比赛结果", f"已结算 {len(matches)} 局，{result_label}：" + "；".join(
            f"{entry['player']} {entry['total_score']} 分" for entry in standings)])
    else:
        lines.extend(["## 实际比赛结果", "尚无已结算局日志；本页保留实际请求及失败审计。"])
    lines.extend(['<a id="来源与审计"></a>', "## 来源与审计",
                  f"[运行时模型边界捕获]({capture_path.relative_to(run_dir).as_posix()})保存传给模型的参数，非 HTTP 报文；"
                  f"[任务记录]({audit.relative_to(run_dir).as_posix()})保留完整结果与原始回复。",
                  *(["[本场工具记录](tools.md)：发送前实际定义或标明来源的离线 SDK 定义，含任务引用。"]
                    if paths.tools.exists() else []),
                  f"本页工具定义来自[SDK 离线捕获]({protocol_path.relative_to(run_dir).as_posix()})："
                  "使用本场配置、生产适配器与假密钥，由 MockTransport 截获，无真实服务调用。"
                  "它与运行时记录分开保存，重新导出时复用已存定义。",
                  "玩家局内沿用对话，跨局保留累计私有记忆；已完成任务的思考仅留日志。",
                  "完整调试记录可用 --debug 导出。"])
    path = run_dir / "replay.md"
    if debug:
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_text("\n\n".join(debug_lines) + "\n", encoding="utf-8")
    path.write_text("\n\n".join(lines) + "\n", encoding="utf-8")
    write_readme(paths, standings)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("player")
    parser.add_argument("--capture-definitions", action="store_true", help="缺定义时显式离线补捕获")
    parser.add_argument("--debug", action="store_true", help="按需导出完整调试 Markdown")
    args = parser.parse_args()
    print(export_replay(args.directory, args.player,
                        capture_definitions=args.capture_definitions, debug=args.debug))
