"""检查真实模型连通、系统提示是否送达、渠道能否承受比赛的并发上限和有没有缓存，可能收费；都不重试。"""

from __future__ import annotations

import sys
import time
import argparse
import hashlib
import secrets
from concurrent.futures import ThreadPoolExecutor

from auction_game.configuration import ConfigurationError, load_configuration
from auction_game.llm.client import MissingEnvError, make_model_factory
from auction_game.llm.execution import AgentExecutor
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, SystemPromptPart
from pydantic_ai.usage import RequestUsage, UsageLimits

# 问法不提"系统提示"：Grok 实测会回答核对码，但拒绝透露系统提示的内容。
PROBE_PROMPT = "本次检查的核对码是什么？只回复核对码，不知道就回复“无”。"
PROBE_AGENT = Agent(output_type=str, retries=0)
# 缓存检查的前缀要超过各家的最小缓存长度（常见 1024–4096 token），内容固定，换一次运行也是同一段。
CACHE_FILLER = "\n".join(f"{i:04d} {hashlib.sha256(str(i).encode()).hexdigest()[:16]}" for i in range(500))
CACHE_SYSTEM = f"你是一个简洁的助手。下面是一份编号清单，回答时用不到。\n{CACHE_FILLER}"
CACHE_PROMPT = "只回复“好”。"


def probe_history(code: str) -> list[ModelRequest]:
    return [ModelRequest(parts=[SystemPromptPart(f"你是一个简洁的助手。本次检查的核对码是 {code}。")])]


def cache_report(label: str, first: RequestUsage, second: RequestUsage) -> str:
    """两次同样的长请求，看第 2 次的缓存命中；第 1 次也可能命中上一次运行留下的缓存。"""
    tag = "[缓存]" if second.cache_read_tokens else "[无缓存]"
    return (f"{tag} {label}  第 1 次输入 {first.input_tokens}、命中 {first.cache_read_tokens}；"
            f"第 2 次输入 {second.input_tokens}、命中 {second.cache_read_tokens}")


def concurrency_report(label: str, single: float, total: float, durations: list[float]) -> str:
    """全部成功时的报告。渠道把同时到达的请求排队处理时不报错，只是总耗时接近单次的 n 倍；
    超过单次与 n 倍的中点就标 [排队]。单次耗时本身有波动，这只是粗略的提示。"""
    n = len(durations)
    tag = "[排队]" if total >= single * (n + 1) / 2 else "[并发通过]"
    each = "、".join(f"{d:.1f}" for d in sorted(durations))
    return f"{tag} {label}  同时 {n} 个，总耗时 {total:.1f}s，单次 {single:.1f}s；各次 {each}s"


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
    concurrency = config["llm"]["max_concurrency"]

    targets = ({key: config["models"][key] for key in args.model} if args.model
               else {pc.execution.key: pc.execution for pc in players})
    failures = 0
    code = secrets.token_hex(4)
    concurrency_step = (f"同时发 {concurrency} 个核对请求看能否承受并发上限，"
                        if concurrency > 1 else "")
    print(f"连通检查：{len(targets)} 个检查目标，会请求真实服务，可能收费。"
          f"每个目标先请求一次核对系统提示，通过后{concurrency_step}再发两次同样的长请求看缓存，都不重试。")
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
        except MissingEnvError as exc:
            print(f"[配置错误] {label}: {exc}")
            failures += 1
            continue

        def probe():
            started = time.monotonic()
            result = executor(PROBE_AGENT, PROBE_PROMPT, probe_history(code),
                              usage_limits=UsageLimits(request_limit=1))
            return result, time.monotonic() - started

        try:
            result, elapsed = probe()
        except Exception as exc:
            print(f"[调用失败] {label}: {type(exc).__name__}: {exc}")
            failures += 1
            continue
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

        # 并发和缓存各查各的，一项失败不影响另一项；目标只记一次失败。
        failed = False
        if concurrency > 1:
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [pool.submit(probe) for _ in range(concurrency)]
            total = time.monotonic() - started
            errors = [f"{type(exc).__name__}: {exc}" for f in futures if (exc := f.exception())]
            errors += ["回复里没有核对码" for f in futures
                       if not f.exception() and code not in f.result()[0].output]
            if errors:
                print(f"[并发失败] {label}: 同时 {concurrency} 个，失败 {len(errors)} 个；{errors[0]}")
                failed = True
            else:
                print(concurrency_report(label, elapsed, total, [f.result()[1] for f in futures]))
        try:
            usages = [executor(PROBE_AGENT, CACHE_PROMPT, [ModelRequest(parts=[SystemPromptPart(CACHE_SYSTEM)])],
                               usage_limits=UsageLimits(request_limit=1)).messages[-1].usage for _ in range(2)]
        except Exception as exc:
            print(f"[缓存检查失败] {label}: {type(exc).__name__}: {exc}")
            failed = True
        else:
            print(cache_report(label, *usages))
        failures += failed

    print(f"\n检查完成：共 {len(targets)} 个检查目标，失败 {failures} 个。")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
