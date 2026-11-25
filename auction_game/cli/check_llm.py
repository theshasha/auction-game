"""检查真实模型连通、系统提示是否送达、同一模型能否同时处理多个请求和有没有缓存，可能收费；都不重试。"""

from __future__ import annotations

import sys
import time
import argparse
import hashlib
import re
import secrets
import traceback
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from auction_game.configuration import ConfigurationError, load_configuration
from auction_game.llm.client import MissingEnvError, make_model_factory
from auction_game.llm.execution import (AgentExecutor, CallFailedError, OutputFailedError, request_error_category,
                                        request_error_reason, request_status)
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
# 测速要一段足够长的输出，等第一个字的时间才不会占大头；数数的内容固定，各模型输出长度相近。
SPEED_PROMPT = "从 1 数到 200，每个数字单独占一行，不要输出其他内容。"
# 输出约 600–1000 token，推理档位高的模型还会先思考；预算留宽，免得被截断算作失败。
SPEED_BUDGET = 8192
# 核对请求只有几十个字，各模型分词不同差两三倍属于正常；到这个数说明渠道往请求里加了内容。
EXTRA_INPUT_TOKENS = 1000
# 表格各列：名称、Outcome 的字段、宽度。状态最长四个汉字。
COLUMNS = (("连通", "connect", 10), ("耗时", "elapsed", 8), ("速度", "speed", 12),
           ("并发", "concurrency", 10), ("缓存", "cache", 0))


def probe_history(code: str) -> list[ModelRequest]:
    return [ModelRequest(parts=[SystemPromptPart(f"你是一个简洁的助手。本次检查的核对码是 {code}。")])]


@dataclass
class Outcome:
    """一个模型的检查结果：表格各列、要处理的问题、不算失败的提醒，以及 --verbose 才显示的细节。"""

    connect: str = "-"
    speed: str = "-"
    concurrency: str = "-"
    cache: str = "-"
    elapsed: str = "-"
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)


def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, size: int) -> str:
    """补空格到 size 宽；超宽时也留两个空格，列不会粘在一起。"""
    return text + " " * max(size - width(text), 2)


def latest_error(exc: CallFailedError) -> dict:
    return exc.request_errors[-1] if exc.request_errors else {"type": type(exc).__name__, "message": str(exc)}


def channel_message(exc: CallFailedError) -> str | None:
    """渠道报错正文里的说明，只取第一句，不带句末标点。"""
    body = getattr(exc.__cause__, "body", None)
    inner = body.get("error", body) if isinstance(body, dict) else None
    message = inner.get("message") if isinstance(inner, dict) else inner
    if not isinstance(message, str) or not message.strip():
        return None
    # 只在句号后接大写字母处断句，"e.g. check" 这类缩写不断开。
    first = re.split(r"(?<=\.)\s+(?=[A-Z])", " ".join(message.split()))[0].rstrip(".。")
    return first if len(first) <= 100 else first[:100] + "…"


def short_error(exc: CallFailedError) -> str:
    """一句话的失败原因，带上渠道返回的说明；完整原文放进 --verbose 的细节。"""
    if isinstance(exc, OutputFailedError):
        return "回复为空或无法读成文本"
    latest = latest_error(exc)
    category = request_error_category(latest["type"], latest["message"])
    message = channel_message(exc)
    if category is None:
        return f"{latest['type']}：{message}" if message else request_error_reason(latest["type"], latest["message"])
    return f"{category}：{message}" if message else category


def raw_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def program_error(outcome: Outcome, exc: Exception) -> None:
    """程序自身出错：打印调用栈，和渠道的调用失败分开报，免得当成渠道问题反复重查。"""
    traceback.print_exception(exc)
    outcome.problems.append(f"本程序出错，不是渠道的问题（调用栈见上方）：{raw_error(exc)}")


def cache_verdict(outcome: Outcome, first: RequestUsage, second: RequestUsage) -> None:
    """两次同样的长请求，看第 2 次的缓存命中；第 1 次也可能命中上一次运行留下的缓存。"""
    outcome.cache = "有" if second.cache_read_tokens else "没有"
    if not second.cache_read_tokens:
        outcome.notes.append("没有缓存，比赛照常进行，只是费用高一些；有的渠道缓存不稳定，可以再查一次确认。")
    outcome.details.append(f"缓存：第 1 次输入 {first.input_tokens}、命中 {first.cache_read_tokens}；"
                           f"第 2 次输入 {second.input_tokens}、命中 {second.cache_read_tokens}")


def concurrency_verdict(outcome: Outcome, single: float, total: float, durations: list[float]) -> None:
    """全部成功时的判断。渠道把同时到达的请求排队处理时不报错，只是总耗时接近单次的 n 倍；
    超过单次与 n 倍的中点就算排队。单次耗时本身有波动，这只是粗略的提示。"""
    n = len(durations)
    queued = total >= single * (n + 1) / 2
    outcome.concurrency = "排队" if queued else "正常"
    if queued:
        outcome.notes.append(f"同时 {n} 个请求时在排队处理，比赛不会失败但会变慢；单次耗时有波动，偶尔误报，可以再查一次。")
    each = "、".join(f"{d:.1f}" for d in sorted(durations))
    outcome.details.append(f"并发：同时 {n} 个，总耗时 {total:.1f}s，单次 {single:.1f}s；各次 {each}s")


def check(model, config, args, code: str) -> Outcome:
    outcome = Outcome()
    probe_budget = min(args.max_tokens, model.max_tokens)
    outcome.details.append(f"{model.provider} / {model.protocol} / {model.model}；比赛预算 {model.max_tokens}，"
                           f"本次探测预算 {probe_budget}，推理 {model.capabilities['reasoning_status']}"
                           f"（{model.capabilities['reasoning_detail']}）")
    def build(budget: int) -> AgentExecutor:
        return AgentExecutor(make_model_factory(
            provider=model.provider,
            channel=config["llm"]["channels"][model.provider],
            model=model.model,
            timeout=config["llm"]["timeout_seconds"],
            protocol=model.protocol,
            max_tokens=budget,
            model_settings=model.model_settings,
            request_attempts=1,
        ))

    try:
        executor = build(probe_budget)
    except MissingEnvError as exc:
        outcome.connect = "配置错误"
        outcome.problems.append(str(exc))
        return outcome

    def probe():
        started = time.monotonic()
        result = executor(PROBE_AGENT, PROBE_PROMPT, probe_history(code), usage_limits=UsageLimits(request_limit=1))
        return result, time.monotonic() - started

    try:
        result, elapsed = probe()
    except CallFailedError as exc:
        outcome.connect = "失败"
        outcome.problems.append(short_error(exc))
        outcome.details.append(f"错误原文：{raw_error(exc)}")
        return outcome
    except Exception as exc:
        outcome.connect = "程序错误"
        program_error(outcome, exc)
        return outcome
    preview = " ".join(result.output.split())[:80]
    input_tokens = result.messages[-1].usage.input_tokens
    outcome.details.append(f"核对：耗时 {elapsed:.1f}s，输入 {input_tokens} token，回复 {preview or '（空）'}")
    outcome.elapsed = f"{elapsed:.1f}s"
    if input_tokens >= EXTRA_INPUT_TOKENS:
        outcome.notes.append(f"核对请求只有几十个字，渠道却计了 {input_tokens} 个输入 token，可能往请求里加了内容。")
    if not preview:
        outcome.connect = "失败"
        outcome.problems.append("回复只有空白")
        return outcome
    if code not in result.output:
        outcome.connect = "提示丢失"
        outcome.problems.append(f"回复里没有核对码，渠道丢弃或替换了系统提示，模型看不到规则。回复：{preview}")
        return outcome
    outcome.connect = "正常"

    # 速度、并发和缓存各查各的，一项失败不影响其他项；程序错误时跳过剩下的检查。
    # 测速排在并发前面，免得刚被限流的渠道连带测不准。
    speed_budget = min(SPEED_BUDGET, model.max_tokens)
    started = time.monotonic()
    try:
        usage = build(speed_budget)(PROBE_AGENT, SPEED_PROMPT, [], usage_limits=UsageLimits(request_limit=1)).messages[-1].usage
    except CallFailedError as exc:
        outcome.speed = "没查成"
        outcome.problems.append(f"测速失败：{short_error(exc)}")
        outcome.details.append(f"测速错误原文：{raw_error(exc)}")
    except Exception as exc:
        outcome.speed = "程序错误"
        program_error(outcome, exc)
        return outcome
    else:
        spent = time.monotonic() - started
        outcome.speed = f"{usage.output_tokens / spent:.0f} tok/s"
        outcome.details.append(f"测速：输出 {usage.output_tokens} token（含思考），耗时 {spent:.1f}s，预算 {speed_budget}")

    concurrency = args.concurrency
    if concurrency == 1:
        outcome.concurrency = "不查"
    else:
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(probe) for _ in range(concurrency)]
        total = time.monotonic() - started
        exceptions = [exc for f in futures if (exc := f.exception())]
        bug = next((exc for exc in exceptions if not isinstance(exc, CallFailedError)), None)
        if bug is not None:
            outcome.concurrency = "程序错误"
            program_error(outcome, bug)
            return outcome
        errors = [short_error(exc) for exc in exceptions]
        errors += ["回复里没有核对码" for f in futures
                   if not f.exception() and code not in f.result()[0].output]
        if errors:
            limited = any(request_status(latest_error(exc)["message"]) == 429 for exc in exceptions)
            outcome.concurrency = "被限流" if limited else "失败"
            outcome.problems.append(f"同时 {concurrency} 个请求有 {len(errors)} 个失败：{errors[0]}。"
                                    "比赛里会自动重发，但额度不够时会频繁失败、需要补跑。")
            outcome.details += [f"并发错误原文：{raw_error(exc)}" for exc in exceptions]
        else:
            concurrency_verdict(outcome, elapsed, total, [f.result()[1] for f in futures])
    try:
        usages = [executor(PROBE_AGENT, CACHE_PROMPT, [ModelRequest(parts=[SystemPromptPart(CACHE_SYSTEM)])],
                           usage_limits=UsageLimits(request_limit=1)).messages[-1].usage for _ in range(2)]
    except CallFailedError as exc:
        outcome.cache = "没查成"
        outcome.problems.append(f"缓存检查失败：{short_error(exc)}")
        outcome.details.append(f"缓存错误原文：{raw_error(exc)}")
    except Exception as exc:
        outcome.cache = "程序错误"
        program_error(outcome, exc)
    else:
        cache_verdict(outcome, *usages)
    return outcome


def print_items(title: str, items: list[tuple[str, list[str]]]) -> None:
    print(f"\n{title}")
    for key, lines in items:
        print(f"- {key}：{lines[0]}")
        for line in lines[1:]:
            print(f"  {line}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--model", action="append", metavar="KEY", help="只检查 models.yaml 中指定的模型配置名；可重复")
    selection.add_argument("--test-model", action="store_true", help="只检查 config.test_model 指定的测试模型")
    parser.add_argument("--max-tokens", type=int, default=2048, help="检查输出预算，默认 2048，不改变比赛预算")
    # 比赛里同时打到一个模型的请求数是用它的席位数，团队赛一队三人同模型时是 3。
    parser.add_argument("--concurrency", type=int, default=3,
                        help="每个模型同时发几个核对请求，默认 3，为 1 时不查并发")
    parser.add_argument("--verbose", action="store_true",
                        help="每个模型下面加显示渠道、预算、推理档位、各次用量和错误原文")
    args = parser.parse_args(argv)
    if args.max_tokens <= 0:
        parser.error("--max-tokens 必须是正整数")
    if args.concurrency <= 0:
        parser.error("--concurrency 必须是正整数")
    try:
        config, players = load_configuration(test_model=args.test_model)
        for key in args.model or []:
            if key not in config["models"]:
                raise ConfigurationError(f"models.yaml：未知模型配置名 {key!r}")
    except ConfigurationError as exc:
        print(f"[配置错误] {exc}")
        return 1

    targets = ({key: config["models"][key] for key in args.model} if args.model
               else {pc.execution.key: pc.execution for pc in players})
    code = secrets.token_hex(4)
    concurrency_step = f"同时发 {args.concurrency} 个看并发，" if args.concurrency > 1 else ""
    print(f"连通检查：{len(targets)} 个模型，会请求真实服务，可能收费。"
          f"每个模型先核对系统提示，再让它数数测速，{concurrency_step}最后发两次长请求看缓存，都不重试。")
    key_width = max(width("模型"), *map(width, targets)) + 2
    print(pad("模型", key_width) + "".join(pad(name, size) for name, _, size in COLUMNS).rstrip())
    problems, notes = [], []
    for key, model in targets.items():
        outcome = check(model, config, args, code)
        print(pad(key, key_width) + "".join(pad(getattr(outcome, attr), size) for _, attr, size in COLUMNS).rstrip())
        if args.verbose:
            for line in outcome.details:
                print(f"    {line}")
        if outcome.problems:
            problems.append((key, outcome.problems))
        if outcome.notes:
            notes.append((key, outcome.notes))

    if problems:
        print_items(f"要处理的（{len(problems)} 个）：", problems)
    if notes:
        print_items("值得留意（不算失败）：", notes)
    if problems:
        again = " ".join(f"--model {key}" for key, _ in problems)
        print(f"\n单独重查：python -m auction_game.cli.check_llm {again}")
    else:
        print("\n没有要处理的问题。")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
