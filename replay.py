"""从可选模型边界审计导出单玩家关键节点；不生成回复或维护比赛历史。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx2 as http
from pydantic import TypeAdapter
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models import ModelRequestParameters, override_allow_model_requests

from analyze import load_match_logs
from configuration import load_configuration
from llm_client import ModelFactory


def _json(value):
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```"


def protocol_tools(body):
    """只取 SDK 实际发送的工具及选择字段；保留 null/false，不转换协议。"""
    return {key: body[key] for key in ("tools", "tool_choice", "parallel_tool_calls") if key in body}


async def capture_protocol_definition(factory, record):
    """生产公开模型入口序列化一次；MockTransport 在收包后立即用 400 结束。

    无需生成回复或重跑 Agent；400 不触发生产基础设施重试。只保存 JSON 正文，
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
                           http.MockTransport(handle))
    messages = ModelMessagesTypeAdapter.validate_python(record["messages"])
    parameters = TypeAdapter(ModelRequestParameters).validate_python(record["parameters"])
    with override_allow_model_requests(True):
        async with offline.open() as model:
            try:
                async with model.request_stream(messages, record["settings"], parameters):
                    raise RuntimeError("离线捕获应在模拟传输边界结束")
            except ModelHTTPError as exc:
                if exc.status_code != 400 or len(bodies) != 1:
                    raise
    return {"source": "offline_sdk_mock_transport", "protocol": factory.protocol,
            "model": factory.model, "body": bodies[0], "definition": protocol_tools(bodies[0])}


def _protocol_records(entries, player, path):
    """按定义/设置复用 SDK 捕获；已有旁路文件用于原样重新导出。"""
    if path.exists():
        snapshots = json.loads(path.read_text(encoding="utf-8"))
    else:
        config, players = load_configuration()  # YAML 配置不读取 .env 或环境密钥。
        pc = next(pc for pc in players if pc["name"] == player)
        factory = ModelFactory(pc["protocol"], pc["model"],
                               config["llm"]["channels"][pc["provider"]]["base_url"],
                               "offline-capture-not-a-secret", config["llm"]["timeout_seconds"], pc["max_tokens"])
        snapshots = {}
        for entry in entries:
            for record in entry["requests"]:
                key = json.dumps([record["parameters"], record["settings"]], sort_keys=True, ensure_ascii=False)
                if key not in snapshots:
                    snapshots[key] = asyncio.run(capture_protocol_definition(factory, record))
        path.write_text(json.dumps(snapshots, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return snapshots


def _response_lines(response):
    """展示实际回复的业务内容，调用 ID 及规范消息对象留在调试记录。"""
    lines = []
    for part in response["parts"]:
        kind = part["part_kind"]
        if kind == "tool-call":
            args = part["args"]
            lines.append(f"调用工具 `{part['tool_name']}`，原始业务参数（strategy 仅自己可见）：")
            # 字符串原样保留，真实无效 JSON 同样可读，不以最终合法结果替换。
            lines.append("```json\n" + args + "\n```" if isinstance(args, str) else _json(args))
        elif kind == "text":
            lines.append(part["content"])
        elif kind == "thinking":
            lines.append("思考内容：\n\n" + part["content"])
        else:
            lines.append(f"其他响应部分（{kind}）见本环节调试记录。")
    return lines


def export_replay(run_dir: Path, player: str, *, simulated=False) -> Path:
    """读取实际日志；模拟标签由调用者显式指定，也可用于获授权的真实记录。"""
    audit = run_dir / f"ai_{player}.jsonl"
    entries = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    matches = load_match_logs(run_dir)
    if not entries or not all(entry.get("requests") for entry in entries):
        raise ValueError("该玩家缺少完整模型边界记录，请启动比赛时指定 record_player")
    protocol_path = run_dir / f"protocol_{player}.json"
    snapshots = _protocol_records(entries, player, protocol_path)
    records_path = run_dir / f"requests_{player}.jsonl"
    with records_path.open("w", encoding="utf-8") as file:
        for entry in entries:
            for record in entry["requests"]:
                file.write(json.dumps({"task_id": entry["task_id"], "task": entry["task"],
                                       "phase": entry["phase"], "context": entry["context"],
                                       **record}, ensure_ascii=False) + "\n")

    selected = {"opening_speech": "比赛开头 · 开局发言",
                "opening_plan": "第一局规划 · 首日缓存",
                "match_1_speech": "第一局结束 · 局末发言",
                "match_1_plan": "第二局规划 · 接收实际反馈",
                "final_speech": "实际比赛结尾 · 最终感言",
                "final_summary": "实际比赛结尾 · 观众总结"}
    debug_path = run_dir / f"debug_{player}.md"
    debug = [f"# {player} · 调试记录", "逻辑请求来源：运行时模型边界捕获，包含框架内部字段。",
             f"协议请求来源：[离线 SDK 捕获]({protocol_path.name})；只保存 JSON 正文，无鉴权头。"]
    label = "模拟离线运行（FunctionModel，无真实服务调用）" if simulated else "已记录的模型运行"
    lines = [f"# {player} · 单玩家关键节点回放", f"> {label}。所有回复来自本次运行记录。",
              "参数与名单来自 game.yaml / players.yaml；本场规则见 [RULES.md](RULES.md)。",
              f"完整历史与设置见[调试记录]({debug_path.name})；[逻辑请求原始记录]({records_path.name})"
              f"包含框架模型边界参数，非 HTTP 报文。[任务结果与工具应答]({audit.name})保留完整审计。",
              f"工具定义来自[SDK 离线捕获]({protocol_path.name})：SDK 在离线模拟传输中生成的请求定义，"
              "未发送到真实中转。使用当前玩家配置的生产适配器与假密钥，按定义及设置复用捕获；"
              "它与上述运行时逻辑记录分开保存，捕获过程不生成或替换比赛回复。",
             "重生成：`python offline_demo.py`；选择其他玩家：`python offline_demo.py --player 玩家名`。"
             "本文件仅选关键节点，完整比赛已照常执行。",
             "## 系统规则与人设（全文仅一次）"]
    first_messages = entries[0]["requests"][0]["messages"]
    lines.extend(part["content"] for message in first_messages for part in message["parts"]
                 if part["part_kind"] == "system-prompt")
    schemas = {}
    prior_count = 0
    for number, entry in enumerate(entries, 1):
        anchor = f"环节-{number}"
        debug.extend([f'<a id="{anchor}"></a>', f"## 环节 {number}", _json(entry)])
        phase = entry["phase"]
        title = selected.get(phase)
        if phase == "bid" and entry["context"] == "第1局第2天":
            title = "第一局第二天 · 正常出价"
        if not title:
            if entry["status"] == "success" and entry["task"] != "summary":
                prior_count += 1
            continue
        if phase == "match_1_speech":
            lines.append("\n> 跳转：省略第一局第三天起的日常请求；下文局末提示仍保留程序实际给出的整局反馈。")
        if phase == "final_speech":
            lines.append("\n> 跳转：省略第二局其余日常出价及中间各局，直接到实际最后一局结束。")
        history = (f"沿用系统规则与人设，以及此前 {prior_count} 次已完成任务的提示、回复与工具应答（含跳过部分）。"
                   if prior_count else "此时只有系统规则与人设，没有先前任务历史。")
        purpose = {"speech": "根据当前比赛阶段生成私有策略与公开发言。", "plan": "读取本局信息，规划并给出首日出价。",
                   "bid": "读取昨日结果与当前余额，生成当天出价。", "summary": "读取完整比赛历史，为观众生成总结。"}[entry["task"]]
        lines.extend([f"## {title}", purpose + history,
                      f"精确历史、调用标识与设置见[本环节调试记录]({debug_path.name}#{anchor})。"])
        for record in entry["requests"]:
            if len(entry["requests"]) > 1:
                lines.append("### 第一次请求" if record["request"] == 1 else "### 纠正后请求")
            # 首次是实际新增 user prompt；纠错则展示本次边界最后 request 的纠错信息。
            parts = ([part for message in entry["messages"] for part in message["parts"]
                      if part["part_kind"] == "user-prompt"] if record["request"] == 1 else
                     [part for part in record["messages"][-1]["parts"] if part["part_kind"] == "retry-prompt"])
            lines.append("**渲染后的新增提示全文**")
            lines.extend(part["content"] if isinstance(part["content"], str) else _json(part["content"]) for part in parts)
            capture_key = json.dumps([record["parameters"], record["settings"]], sort_keys=True, ensure_ascii=False)
            snapshot = snapshots[capture_key]
            schema = snapshot["definition"]
            key = json.dumps([snapshot["protocol"], schema], ensure_ascii=False, sort_keys=True)
            if not schema.get("tools"):
                lines.append("**回复方式：直接返回文本，无工具。**")
                if schema:
                    lines.append(_json(schema))
            elif key not in schemas:
                label = {"speech": "发言", "plan": "规划", "bid": "出价"}[entry["task"]]
                schema_title = f"{label}工具定义（{snapshot['protocol']}）"
                schema_anchor = f"工具定义-{len(schemas) + 1}"
                schemas[key] = (schema_title, schema_anchor)
                lines.extend([f'<a id="{schema_anchor}"></a>', f"### {schema_title}", _json(schema)])
            else:
                schema_title, schema_anchor = schemas[key]
                lines.append(f"工具定义沿用前文的[{schema_title}](#{schema_anchor})。")
            reply_label = "第一次回复" if len(entry["requests"]) > 1 and record["request"] == 1 else "纠正后回复" if record["request"] > 1 else "回复"
            lines.append(f"**{'模拟' if simulated else '记录'}{reply_label}**")
            if "response" in record:
                lines.extend(_response_lines(record["response"]))
            else:
                lines.append("本次请求未取得完整回复，失败详情见调试记录。")
            if record.get("errors"):
                lines.append(_json(record["errors"]))
        lines.append("**程序处理**")
        if entry["status"] != "success":
            lines.extend(["任务失败，审计保留；本次结果不提交玩家历史或首日缓存。", _json(entry.get("error"))])
        elif entry["task"] == "plan":
            lines.append("校验通过后保存私有策略与首日缓存。")
            if phase == "opening_plan":
                day = matches[0]["match"]["days"][0]
                bid = next(b for b in day["bids"] if b["player"] == player and not b["is_super"])
                lines.append(f"第一局第 1 天消费缓存，不重复请求模型；实际正常出价 {bid['bid']}，"
                             f"当日获胜者：{day['winner']}。")
            else:
                lines.append("本节点只展示下一局规划衔接，随后跳过该局的日常流程。")
        elif entry["task"] == "bid":
            day = matches[0]["match"]["days"][1]
            lines.append(f"按实时余额校验后交给结算：获胜者 {day['winner']}，赢得 {day['wins_taken']} 胜场。")
        elif entry["task"] == "summary":
            lines.append("总结只写日志并给观众看，请求与回复不追加玩家历史。")
        else:
            lines.append("私有 strategy 保留在自身历史；仅 speech 投递给其他玩家。")
            if phase == "match_1_speech":
                lines.append("本节点的局日志与排名就是实际反馈；随后收集他人的公开局末发言进行第二局规划，无独立反馈请求。")
            if phase == "final_speech":
                lines.append("最后一局跳过普通局末发言，直接进行本次最终感言；随后只有观众总结。")
        if entry["status"] == "success" and entry["task"] != "summary":
            prior_count += 1
    standings = matches[-1]["standings_after_match"]
    lines.extend(["## 实际比赛结果", f"共 {len(matches)} 局，最终胜场：" + "；".join(
        f"{entry['player']} {entry['total_wins']}" for entry in standings),
        "离线结果只验证流程；真实服务的工具与协议兼容性须另行授权验证。"])
    path = run_dir / f"replay_{player}.md"
    debug_path.write_text("\n\n".join(debug) + "\n", encoding="utf-8")
    path.write_text("\n\n".join(lines) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("player")
    parser.add_argument("--simulated", action="store_true")
    args = parser.parse_args()
    print(export_replay(args.directory, args.player, simulated=args.simulated))
