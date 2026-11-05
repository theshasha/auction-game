"""检查真实模型连通和系统提示是否送达，可能收费；每个配置至多请求一次，不重试。"""

from __future__ import annotations

import sys
import time
import argparse
import secrets

from auction_game.configuration import ConfigurationError, load_configuration
from auction_game.llm.client import MissingApiKeyError, make_model_factory
from auction_game.llm.execution import AgentExecutor
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, SystemPromptPart
from pydantic_ai.usage import UsageLimits

# 问法不提"系统提示"：Grok 实测会回答核对码，但拒绝透露系统提示的内容。
PROBE_PROMPT = "本次检查的核对码是什么？只回复核对码，不知道就回复“无”。"
PROBE_AGENT = Agent(output_type=str, retries=0)


def probe_history(code: str) -> list[ModelRequest]:
    return [ModelRequest(parts=[SystemPromptPart(f"你是一个简洁的助手。本次检查的核对码是 {code}。")])]


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
        for key in args.model or []:
            if key not in config["models"]:
                raise ConfigurationError(f"models.yaml：未知模型配置名 {key!r}")
    except ConfigurationError as exc:
        print(f"[配置错误] {exc}")
        return 1
    timeout = config["llm"]["timeout_seconds"]

    targets = ({key: config["models"][key] for key in args.model} if args.model
               else {pc.execution.key: pc.execution for pc in players})
    failures = 0
    code = secrets.token_hex(4)
    print(f"连通检查：{len(targets)} 个检查目标，会请求真实服务，可能收费。每个目标至多请求一次，不重试。")
    for model in targets.values():
        label = f"{model.key}（{model.provider} / {model.protocol} / {model.model}）"
        probe_budget = min(args.max_tokens, model.max_tokens)
        print(f"[探测] {label}: 比赛预算={model.max_tokens}，本次探测预算={probe_budget}，"
              f"推理={model.capabilities['reasoning_status']}；"
              f"{model.capabilities['reasoning_detail']}")
        try:
            executor = AgentExecutor(make_model_factory(
                provider=model.provider,
                channel=config["llm"]["channels"][model.provider],
                model=model.model,
                timeout=timeout,
                protocol=model.protocol,
                max_tokens=probe_budget,
                model_settings=model.model_settings,
                request_attempts=1,
            ))
        except MissingApiKeyError as exc:
            print(f"[配置错误] {label}: {exc}")
            failures += 1
            continue

        started = time.monotonic()
        try:
            result = executor(PROBE_AGENT, PROBE_PROMPT, probe_history(code),
                              usage_limits=UsageLimits(request_limit=1))
        except Exception as exc:
            print(f"[调用失败] {label}: {type(exc).__name__}: {exc}")
            failures += 1
            continue
        elapsed = time.monotonic() - started
        preview = " ".join(result.output.split())[:80]
        if not preview:
            print(f"[调用失败] {label}: 回复为空")
            failures += 1
            continue
        detail = f"耗时 {elapsed:.1f}s  输入 {result.messages[-1].usage.input_tokens} token  回复: {preview}"
        if code not in result.output:
            print(f"[系统提示未生效] {label}  {detail}")
            failures += 1
            continue
        print(f"[通过] {label}  {detail}")

    print(f"\n检查完成：共 {len(targets)} 个检查目标，失败 {failures} 个。")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
