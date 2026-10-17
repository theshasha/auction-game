"""从已保存的模型请求记录导出单玩家关键节点；不生成回复或修改比赛历史。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from importlib.metadata import version

import httpx2 as http
from pydantic import TypeAdapter
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models import ModelRequestParameters, override_allow_model_requests

from auction_game.reporting.analyze import load_match_logs
from auction_game.ai.llm_client import ModelFactory
from auction_game.reporting.run_paths import RunPaths
from auction_game.reporting.request_records import load_requests, digest
from auction_game.reporting.run_output import write_readme
from auction_game.reporting.tool_records import protocol_tools, tool_task


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
    with override_allow_model_requests(True), tool_task(None, None, None, None):
        async with offline.open() as model:
            try:
                async with model.request_stream(messages, record["settings"], parameters):
                    raise RuntimeError("离线捕获应在模拟传输边界结束")
            except ModelHTTPError as exc:
                if exc.status_code != 400 or len(bodies) != 1:
                    raise
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
            lines.append(f"调用工具 `{part['tool_name']}`，原始业务参数（strategy 仅自己可见）：")
            # 字符串原样保留，真实无效 JSON 同样可读，不以最终合法结果替换。
            lines.append("```json\n" + args + "\n```" if isinstance(args, str) else _json(args))
        elif kind == "text":
            lines.append(part["content"])
        elif kind == "thinking":
            lines.append("思考内容：\n\n" + part["content"])
        else:
            lines.append(f"其他回复内容（{kind}）见完整任务记录，可按需导出调试文件。")
    return lines


def export_replay(run_dir: Path, player: str, *, capture_definitions=False, debug=False) -> Path:
    """读取实际日志；模拟标签取场次类型，重新导出复用原 SDK 定义。"""
    paths = RunPaths(run_dir)
    config = paths.read_config()
    number = paths.number(player)
    audit = paths.player(number)
    entries = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    load_requests(paths.capture(number), entries)
    pc = next(pc for pc in config["players"] if pc["name"] == player)
    matches = load_match_logs(run_dir)
    if not entries or not all(entry.get("requests") for entry in entries):
        raise ValueError("该玩家缺少完整模型边界记录，请启动比赛时指定 record_player")
    protocol_path = paths.definitions(number)
    snapshots = _protocol_records(entries, player, protocol_path, config, capture=capture_definitions)

    selected = {"opening_speech": "比赛开头 · 开局发言",
                "opening_plan": "第一局规划 · 首日缓存",
                "match_1_speech": "第一局结束 · 局末发言",
                "match_1_memory": "第一局结束 · 累计私有记忆",
                "match_1_plan": "第二局规划 · 接收实际反馈",
                "final_speech": "实际比赛结尾 · 最终感言",
                "final_summary": "实际比赛结尾 · 观众总结"}
    debug_path = run_dir / "debug" / f"{number:03d}.md"
    debug_lines = [f"# {player} · 调试记录", "逻辑请求来源：运行时模型边界捕获，包含框架内部字段。",
             f"协议请求来源：[离线 SDK 捕获](../{protocol_path.relative_to(run_dir).as_posix()})；仅必要定义，无鉴权头。",
             "本场执行能力与有效设置：", _json(pc["execution_model"])]
    simulated = config["run_type"] == "offline"
    label = "模拟离线运行（FunctionModel，无真实服务调用）" if simulated else "已记录的模型运行"
    lines = [f"# {player} · 单玩家关键节点回放", f"> {label}。所有回复来自本次运行记录。",
              f"运行模式：{config['mode']}；身份模型：{pc['identity_model']['model']}；"
              f"实际执行：{pc['execution_model']['protocol']} / {pc['execution_model']['model']}。",
              "参数与名单来自本场 [run_config.json](data/run_config.json)；规则见 [RULES.md](RULES.md)。",
              *(["[本场默认工具记录](tools.md)保留发送前定义或明确标注的离线 SDK 定义与任务引用。"]
                if paths.tools.exists() else []),
              f"[增量模型边界捕获]({paths.capture(number).relative_to(run_dir).as_posix()})"
              f"包含框架传给模型的参数，不含 HTTP 报文。[任务结果与工具应答]({audit.relative_to(run_dir).as_posix()})保留完整记录。",
              f"工具定义来自[SDK 离线捕获]({protocol_path.relative_to(run_dir).as_posix()})：SDK 在离线模拟传输中生成的请求定义，"
              "未发送到真实中转。使用本场执行配置的生产适配器与假密钥，按定义及设置复用捕获；"
              "它与上述运行时逻辑记录分开保存，捕获过程不生成或替换比赛回复。",
              "本文件仅选关键节点，完整比赛已照常执行。完整调试 Markdown 仅按需使用 --debug 导出。",
             "## 系统规则与人设（全文仅一次）"]
    first_messages = entries[0]["requests"][0]["messages"]
    lines.extend(part["content"] for message in first_messages for part in message["parts"]
                 if part["part_kind"] == "system-prompt")
    schemas = {}
    for number, entry in enumerate(entries, 1):
        anchor = f"环节-{number}"
        if debug:
            debug_lines.extend([f'<a id="{anchor}"></a>', f"## 环节 {number}", _json(entry)])
        phase = entry["phase"]
        title = selected.get(phase)
        if phase == "bid" and entry["context"] == "第1局第2天":
            title = "第一局第二天 · 正常出价"
        if not title:
            continue
        if phase == "match_1_speech":
            lines.append("\n> 跳转：省略第一局第三天起的日常请求；下文局末提示仍保留程序实际给出的整局反馈。")
        if phase == "final_speech":
            lines.append("\n> 跳转：省略第二局其余日常出价及中间各局，直接到实际最后一局结束。")
        has_history = any(message["kind"] == "response" for message in entry["requests"][0]["messages"])
        history = ("沿用系统规则与人设，以及本次请求携带的任务提示与有效结果；已完成任务的内部 thinking 只保存在日志中。"
                   if has_history else "沿用系统规则与人设；跨局重置后仅保留最新累计记忆，成绩由本次提示提供。")
        purpose = {"speech": "根据当前比赛阶段生成私有策略与公开发言。", "plan": "读取本局信息，规划并给出首日出价。",
                    "bid": "读取昨日结果与当前余额，生成当天出价。", "memory": "读取全部已公布局末发言，整理累计私有记忆。",
                    "summary": "回顾全场记录（不含内部 thinking），为观众生成总结。"}[entry["task"]]
        lines.extend([f"## {title}", purpose + history])
        for record in entry["requests"]:
            if len(entry["requests"]) > 1:
                lines.append("### 第一次请求" if record["request"] == 1 else "### 纠正后请求")
            # 首次是实际新增 user prompt；纠错则展示本次边界最后 request 的纠错信息。
            parts = ([part for message in entry["messages"] for part in message["parts"]
                      if part["part_kind"] == "user-prompt"] if record["request"] == 1 else
                     [part for part in record["messages"][-1]["parts"] if part["part_kind"] == "retry-prompt"])
            lines.append("**渲染后的新增提示全文**")
            lines.extend(part["content"] if isinstance(part["content"], str) else _json(part["content"]) for part in parts)
            capture_key = digest([record["parameters"], record["settings"]])
            snapshot = snapshots[capture_key]
            schema = snapshot["definition"]
            key = json.dumps([snapshot["protocol"], schema], ensure_ascii=False, sort_keys=True)
            if not schema.get("tools"):
                lines.append("**回复方式：直接返回文本，无工具。**")
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
                lines.append(f"工具定义沿用前文的[{schema_title}](#{schema_anchor})。")
            reply_label = "第一次回复" if len(entry["requests"]) > 1 and record["request"] == 1 else "纠正后回复" if record["request"] > 1 else "回复"
            lines.append(f"**{'模拟' if simulated else '记录'}{reply_label}**")
            if "response" in record:
                lines.extend(_response_lines(record["response"]))
            else:
                lines.append("本次请求未取得完整回复，失败详情见任务记录；可按需导出完整调试文件。")
            if record.get("errors"):
                lines.append(_json(record["errors"]))
        lines.append("**程序处理**")
        if entry["status"] != "success":
            lines.extend(["任务失败，详情已记录；本次结果不加入玩家历史，也不会用作首日出价。", _json(entry.get("error"))])
        elif entry["task"] == "plan":
            lines.append("校验通过后保存私有策略与首日缓存。")
            if phase == "opening_plan":
                day = matches[0]["match"]["days"][0]
                bid = next(b for b in day["bids"] if b["player"] == player and not b["is_super"])
                lines.append(f"第一局第 1 天使用已保存的出价，不重复请求模型；实际正常出价 {bid['bid']}，"
                             f"当日获胜者：{day['winner']}。")
            else:
                lines.append("本节点只展示下一局规划衔接，随后跳过该局的日常流程。")
        elif entry["task"] == "bid":
            day = matches[0]["match"]["days"][1]
            lines.append(f"按实时余额校验后交给结算：获胜者 {day['winner']}，赢得 {day['wins_taken']} 胜场。")
        elif entry["task"] == "summary":
            lines.append("总结只写日志并给观众看，请求与回复不追加玩家历史。")
        elif entry["task"] == "memory":
            lines.append(f"日志成功后整体替换累计私有记忆（{entry['memory_chars']} 字符，非 token），清除历局对话；记忆仅本人可见。")
        else:
            lines.append("私有 strategy 保留在自身历史；仅 speech 投递给其他玩家。")
            if phase == "match_1_speech":
                lines.append("本节点的局日志与排名就是实际反馈；全部发言公布后独立整理累计记忆，再带最新成绩进行第二局规划。")
            if phase == "final_speech":
                lines.append("最后一局跳过普通局末发言，直接进行本次最终感言；随后只有观众总结。")
    standings = matches[-1]["standings_after_match"]
    lines.extend(["## 实际比赛结果", f"共 {len(matches)} 局，最终胜场：" + "；".join(
        f"{entry['player']} {entry['total_wins']}" for entry in standings),
        "离线结果只验证流程；真实服务的工具与协议兼容性须另行授权验证。"])
    path = run_dir / "replay.md"
    if debug:
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_text("\n\n".join(debug_lines) + "\n", encoding="utf-8")
    path.write_text("\n\n".join(lines) + "\n", encoding="utf-8")
    write_readme(paths, matches[-1]["standings_after_match"])
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
