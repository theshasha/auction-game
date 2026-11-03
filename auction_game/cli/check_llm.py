"""检查真实模型连通，可能收费；每个配置至多请求一次，不重试。"""

from __future__ import annotations

import sys
import time
import argparse

from auction_game.configuration import ConfigurationError, load_configuration
from auction_game.llm.client import MissingApiKeyError, make_model_factory
from auction_game.llm.execution import AgentExecutor
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, SystemPromptPart
from pydantic_ai.usage import UsageLimits

PING_HISTORY = [ModelRequest(parts=[SystemPromptPart("你是一个简洁的助手。")])]
PING_PROMPT = "请回复 OK。"
PING_AGENT = Agent(output_type=str, retries=0)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--model", action="append", metavar="KEY", help="只检查 models.yaml 中指定的模型配置名；可重复")
    selection.add_argument("--test-model", action="store_true", help="只检查 config.test_model 指定的测试模型")
    parser.add_argument("--max-tokens", type=int, default=2048, help="检查输出预算，默认 2048，不改变比赛预算")
    args = parser.parse_args(argv)
    if args.max_tokens <= 0:
        parser.error("--max-tokens 必须是正整数")
    try:
        config, players = load_configuration(test_model=args.test_model)
        if args.model:
            players = []
            for key in args.model:
                if key not in config["models"]:
                    raise ConfigurationError(f"models.yaml：未知模型配置名 {key!r}")
                players.append({"model_key": key, **config["models"][key]})
    except ConfigurationError as exc:
        print(f"[配置错误] {exc}")
        return 1
    timeout = config["llm"]["timeout_seconds"]

    targets = {player["model_key"]: player for player in players}
    failures = 0
    print(f"连通检查：{len(targets)} 个检查目标，会请求真实服务，可能收费。每个目标至多请求一次，不重试。")
    for player in targets.values():
        label = f"{player['model_key']}（{player['provider']} / {player['protocol']} / {player['model']}）"
        probe_budget = min(args.max_tokens, player["max_tokens"])
        print(f"[探测] {label}: 比赛预算={player['max_tokens']}，本次探测预算={probe_budget}，"
              f"推理={player['capabilities']['reasoning_status']}；"
              f"{player['capabilities']['reasoning_detail']}")
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
        except MissingApiKeyError as exc:
            print(f"[配置错误] {label}: {exc}")
            failures += 1
            continue

        started = time.monotonic()
        try:
            reply = executor(PING_AGENT, PING_PROMPT, PING_HISTORY,
                             usage_limits=UsageLimits(request_limit=1)).output
        except Exception as exc:
            print(f"[调用失败] {label}: {type(exc).__name__}: {exc}")
            failures += 1
            continue
        elapsed = time.monotonic() - started
        preview = " ".join(reply.split())[:80]
        if not preview:
            print(f"[调用失败] {label}: 回复为空")
            failures += 1
            continue
        print(f"[通过] {label}  耗时 {elapsed:.1f}s  回复: {preview}")

    print(f"\n检查完成：共 {len(targets)} 个检查目标，失败 {failures} 个。")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
