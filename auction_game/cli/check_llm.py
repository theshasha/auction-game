"""收费连通探测：已授权排查可做单次最小请求；批量扩大范围须重新确认。"""

from __future__ import annotations

import sys
import time
import argparse
import json

from auction_game.configuration import ConfigurationError, channel_model_profile, load_configuration
from auction_game.ai.llm_client import MissingApiKeyError, make_model_factory
from auction_game.ai.agent_execution import AgentExecutor
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, ToolOutput
from pydantic_ai.messages import ModelRequest, SystemPromptPart
from pydantic_ai.usage import UsageLimits

PING_HISTORY = [ModelRequest(parts=[SystemPromptPart("你是一个简洁的助手。")])]
PING_PROMPT = "调用 ping 工具，reply 填写 OK。"

class PingOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    reply: str = Field(min_length=1)


PING_AGENT = Agent(output_type=ToolOutput(PingOutput, name="ping"), retries=0)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--model", action="append", metavar="KEY", help="只探测指定目录键；可重复")
    selection.add_argument("--test-model", action="store_true", help="仅探测 config.test_model")
    parser.add_argument("--max-tokens", type=int, default=2048, help="探测覆盖预算，默认 2048，不改变比赛预算")
    args = parser.parse_args(argv)
    if args.max_tokens <= 0:
        parser.error("--max-tokens 必须是正整数")
    try:
        config, players = load_configuration(test_model=True) if args.test_model else load_configuration()
        if args.model:
            selected = []
            for key in args.model:
                if key not in config["models"]:
                    raise ConfigurationError(f"models.yaml：未知目录键 {key!r}")
                selected.append({"model_key": key, **config["models"][key]})
            players = selected
    except ConfigurationError as exc:
        print(f"[配置错误] {exc}")
        return 1
    timeout = config["llm"]["timeout_seconds"]

    # 身份别名只在有效预算与设置一致时合并，避免把未知配置当作已验证 high。
    targets = {}
    for player in players:
        channel = config["llm"]["channels"][player["provider"]]
        key = (channel["base_url"], channel["api_key_env"], player["protocol"], player["model"],
               player["max_tokens"], json.dumps(player["model_settings"], sort_keys=True),
               json.dumps(channel_model_profile(channel, player["protocol"]), sort_keys=True))
        targets.setdefault(key, player)
    failures = 0
    for player in targets.values():
        protocol_suffix = f" / {player['protocol']}" if player.get("protocol") else ""
        label = f"{player['model_key']}（{player['provider']}{protocol_suffix} / {player['model']}）"
        probe_budget = min(args.max_tokens, player["max_tokens"])
        print(f"[探测覆盖] {label}: 比赛预算={player['max_tokens']}，本次探测预算={probe_budget}，"
              f"推理={player['capabilities']['reasoning_status']}；"
              f"{player['capabilities']['reasoning_detail']}；已授权排查内单次最小请求，批量扩大范围须重新确认")
        print(f"  渠道 profile={json.dumps(channel_model_profile(config['llm']['channels'][player['provider']], player['protocol']), sort_keys=True)}")
        try:
            executor = AgentExecutor(make_model_factory(
                provider=player["provider"],
                channel=config["llm"]["channels"][player["provider"]],
                model=player["model"],
                timeout=timeout,
                protocol=player["protocol"],
                max_tokens=probe_budget,
                model_settings=player["model_settings"],
                request_attempts=1,
            ))
        except (MissingApiKeyError, ValueError) as exc:
            print(f"[配置错误] {label}: {exc}")
            failures += 1
            continue

        started = time.monotonic()
        try:
            reply = executor(PING_AGENT, PING_PROMPT, PING_HISTORY,
                             usage_limits=UsageLimits(request_limit=1)).output.reply
        except Exception as exc:
            print(f"[调用失败] {label}: {type(exc).__name__}: {exc}")
            failures += 1
            continue
        elapsed = time.monotonic() - started
        preview = " ".join(reply.split())[:80]
        print(f"[通过] {label}  耗时 {elapsed:.1f}s  回复: {preview}")

    print(f"\n共 {len(targets)} 个执行配置（每个至多 1 次请求，无重试），失败 {failures} 个")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
