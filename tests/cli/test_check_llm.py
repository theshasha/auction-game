"""连通检查的目标、预算提示、系统提示探测、测速、并发、缓存和结果汇总。"""

from dataclasses import replace
import json
import re

import pytest
from pydantic_ai.usage import RequestUsage

from auction_game.cli import check_llm
from auction_game.configuration import load_configuration
from tests.support.factories import fake_task_runner
from tests.support.wire import Harness, PROTOCOLS, reply

CODE = "c0de1234"
COUNT = "\n".join(str(i) for i in range(1, 201))
# 不查并发的测试都带上，只发核对、测速和缓存请求。
SERIAL = ["--concurrency", "1"]


@pytest.fixture
def fixed_code(monkeypatch):
    monkeypatch.setattr(check_llm.secrets, "token_hex", lambda nbytes: CODE)


def probe_with(protocol, script, config_files, monkeypatch):
    """让 check_llm 用 Harness 的传输层跑一个目标，返回 harness。"""
    config, players = load_configuration(*config_files)
    config["models"]["substitute"] = replace(config["models"]["substitute"], protocol=protocol)
    monkeypatch.setattr(check_llm, "load_configuration", lambda **kwargs: (config, players))
    harness = Harness(protocol, script)
    monkeypatch.setattr(check_llm, "make_model_factory", lambda **kwargs: replace(
        harness.factory, request_attempts=kwargs["request_attempts"],
    ))
    return harness


def row(output, key):
    """表格里这个模型的一行，按表头的列名取值；列之间至少隔两个空格。"""
    lines = output.splitlines()
    header = next(line for line in lines if line.startswith("模型"))
    cells = next(line for line in lines if line.startswith(f"{key} "))
    return dict(zip(re.split(r"\s{2,}", header.strip())[1:], re.split(r"\s{2,}", cells.strip())[1:], strict=True))


def test_help_names_model_configuration_and_separate_check_budget(capsys):
    with pytest.raises(SystemExit) as exc:
        check_llm.main(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "模型配置名" in output
    assert "不改变比赛预算" in output


def test_check_reports_targets_cost_and_budget(config_files, monkeypatch, capsys, fixed_code):
    monkeypatch.setattr(check_llm, "load_configuration", lambda **kwargs: load_configuration(*config_files, **kwargs))
    runner = fake_task_runner([CODE, COUNT, "好", "好"])
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return runner.executor.factory

    monkeypatch.setattr(check_llm, "make_model_factory", factory)
    assert check_llm.main(["--model", "substitute", "--model", "substitute", "--max-tokens", "100",
                           "--verbose", *SERIAL]) == 0
    output = capsys.readouterr().out
    assert "1 个模型，会请求真实服务，可能收费" in output
    assert "都不重试" in output
    assert "比赛预算 789，本次探测预算 100" in output
    # 核对用 --max-tokens，测速另用一份宽的预算，都不超过比赛预算。
    assert [(call["max_tokens"], call["request_attempts"]) for call in calls] == [(100, 1), (789, 1)]
    assert len(runner.calls) == 4
    assert check_llm.main(["--model", "absent"]) == 1
    assert "models.yaml：未知模型配置名 'absent'" in capsys.readouterr().out
    assert len(calls) == 2


def test_default_output_leaves_out_channel_and_budget(config_files, monkeypatch, capsys, fixed_code):
    monkeypatch.setattr(check_llm, "load_configuration", lambda **kwargs: load_configuration(*config_files, **kwargs))
    runner = fake_task_runner([CODE, COUNT, "好", "好"])
    monkeypatch.setattr(check_llm, "make_model_factory", lambda **kwargs: runner.executor.factory)
    assert check_llm.main(["--model", "substitute", *SERIAL]) == 0
    output = capsys.readouterr().out
    assert "预算" not in output and "推理" not in output
    cells = row(output, "substitute")
    assert (cells["连通"], cells["并发"], cells["缓存"]) == ("正常", "不查", "没有")
    assert cells["速度"].endswith(" tok/s")
    assert "没有要处理的问题" in output


def test_test_model_checks_one_target_with_its_settings(config_files, monkeypatch, fixed_code):
    monkeypatch.setattr(check_llm, "load_configuration", lambda **kwargs: load_configuration(*config_files, **kwargs))
    runner = fake_task_runner([CODE, COUNT, "好", "好"])
    calls = []
    monkeypatch.setattr(check_llm, "make_model_factory",
                        lambda **kwargs: calls.append(kwargs) or runner.executor.factory)
    assert check_llm.main(["--test-model", *SERIAL]) == 0
    # 核对和测速各建一次客户端。
    assert [(call["model"], call["protocol"], call["model_settings"]) for call in calls] == [
        ("test-model-c", "responses", {"openai_reasoning_effort": "high"})] * 2


def test_code_changes_between_runs_and_only_the_system_prompt_has_it(config_files, monkeypatch):
    monkeypatch.setattr(check_llm, "load_configuration", lambda **kwargs: load_configuration(*config_files, **kwargs))
    runner = fake_task_runner(["无", "无"])
    monkeypatch.setattr(check_llm, "make_model_factory", lambda **kwargs: runner.executor.factory)
    check_llm.main(["--model", "substitute", *SERIAL])
    check_llm.main(["--model", "substitute", *SERIAL])
    (first_system, first_prompt), (second_system, _) = [
        [part.content for message in call for part in message.parts] for call in runner.calls]
    assert first_system != second_system
    code = first_system.split("核对码是 ")[1].rstrip("。")
    assert code not in first_prompt


@pytest.mark.parametrize(("protocol", "input_tokens"), [("chat", 0), ("responses", 2), ("anthropic", 2),
                                                          ("vertex", 2)])
def test_probe_sends_the_code_and_passes_when_it_comes_back(
        protocol, input_tokens, config_files, monkeypatch, capsys, fixed_code):
    harness = probe_with(protocol, [reply(text=f"核对码：{CODE}"), reply(text=COUNT), reply(text="好"), reply(text="好")],
                         config_files, monkeypatch)
    assert check_llm.main(["--model", "substitute", "--verbose", *SERIAL]) == 0
    output = capsys.readouterr().out
    assert row(output, "substitute")["连通"] == "正常"
    assert f"输入 {input_tokens} token" in output
    assert f"回复 核对码：{CODE}" in output
    assert CODE in json.dumps(harness.requests[0][1])
    # 通过后测速，再发两次完全相同的长请求看缓存；测试传输层不报缓存命中。
    assert len(harness.requests) == harness.closes == 4
    assert "从 1 数到 200" in json.dumps(harness.requests[1][1], ensure_ascii=False)
    assert harness.requests[2][1] == harness.requests[3][1]
    assert CODE not in json.dumps(harness.requests[2][1])
    assert row(output, "substitute")["缓存"] == "没有"
    assert "值得留意（不算失败）：\n- substitute：没有缓存" in output


@pytest.mark.parametrize("text", ["OK", "无"])
def test_reply_without_the_code_means_the_system_prompt_was_lost(text, config_files, monkeypatch, capsys, fixed_code):
    harness = probe_with("chat", [reply(text=text)], config_files, monkeypatch)
    assert check_llm.main(["--model", "substitute", *SERIAL]) == 1
    output = capsys.readouterr().out
    cells = row(output, "substitute")
    assert cells["连通"] == "提示丢失" and cells["耗时"] != "-"
    assert (cells["速度"], cells["并发"], cells["缓存"]) == ("-", "-", "-")
    assert "要处理的（1 个）：\n- substitute：回复里没有核对码" in output
    assert f"回复：{text}" in output
    assert len(harness.requests) == 1


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_probe_transport_failure_has_no_retry(protocol, config_files, monkeypatch, capsys):
    harness = probe_with(protocol, ["connect"], config_files, monkeypatch)
    assert check_llm.main(["--model", "substitute", *SERIAL]) == 1
    output = capsys.readouterr().out
    assert row(output, "substitute")["连通"] == "失败"
    assert "- substitute：网络连接失败" in output
    assert "单独重查：python -m auction_game.cli.check_llm --model substitute" in output
    assert len(harness.requests) == harness.closes == 1


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_http_failure_is_one_line_and_raw_error_only_with_verbose(protocol, config_files, monkeypatch, capsys):
    probe_with(protocol, [503], config_files, monkeypatch)
    assert check_llm.main(["--model", "substitute", *SERIAL]) == 1
    output = capsys.readouterr().out
    assert "- substitute：服务端故障（HTTP 503）：bad config\n" in output
    assert "错误原文" not in output
    probe_with(protocol, [503], config_files, monkeypatch)
    check_llm.main(["--model", "substitute", "--verbose", *SERIAL])
    assert "错误原文：CallFailedError: " in capsys.readouterr().out


class StreamError(Exception):
    """流中途收到的渠道报错：文字里没有状态码，正文在 body 里。"""

    def __init__(self, body):
        super().__init__(json.dumps(body))
        self.body = body


def failed_with(body, kind="APIStatusError"):
    exc = check_llm.CallFailedError("失败", request_errors=[{"type": kind, "message": json.dumps(body)}])
    exc.__cause__ = StreamError(body)
    return exc


@pytest.mark.parametrize(("body", "expected"), [
    ({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, "APIStatusError：Overloaded"),
    ({"error": "upstream failed."}, "APIStatusError：upstream failed"),
    ({"message": "Resource has been exhausted (e.g. check quota). Please retry later."},
     "APIStatusError：Resource has been exhausted (e.g. check quota)"),
])
def test_channel_message_is_shown_once_and_cut_at_the_first_sentence(body, expected):
    assert check_llm.short_error(failed_with(body)) == expected


def test_empty_reply_is_reported_in_chinese(config_files, monkeypatch, capsys):
    probe_with("chat", [reply(text="")], config_files, monkeypatch)
    assert check_llm.main(["--model", "substitute", *SERIAL]) == 1
    output = capsys.readouterr().out
    assert row(output, "substitute")["连通"] == "失败"
    assert "- substitute：回复为空或无法读成文本\n" in output


def test_missing_env_is_a_configuration_error(config_files, monkeypatch, capsys):
    monkeypatch.setattr(check_llm, "load_configuration", lambda **kwargs: load_configuration(*config_files, **kwargs))

    def factory(**kwargs):
        raise check_llm.MissingEnvError("缺少环境变量 HUB_KEY")

    monkeypatch.setattr(check_llm, "make_model_factory", factory)
    assert check_llm.main(["--model", "substitute", *SERIAL]) == 1
    output = capsys.readouterr().out
    assert row(output, "substitute") == {"连通": "配置错误", "耗时": "-", "速度": "-", "并发": "-", "缓存": "-"}
    assert "- substitute：缺少环境变量 HUB_KEY" in output


def test_program_error_is_reported_apart_and_next_target_is_checked(config_files, monkeypatch, capsys, fixed_code):
    monkeypatch.setattr(check_llm, "load_configuration", lambda **kwargs: load_configuration(*config_files, **kwargs))
    runner = fake_task_runner([CODE, COUNT, "好", "好"])
    real_executor = check_llm.AgentExecutor

    def broken(*args, **kwargs):
        raise AttributeError("程序里的错")

    monkeypatch.setattr(check_llm, "AgentExecutor",
                        lambda factory: broken if factory is None else real_executor(factory))
    monkeypatch.setattr(check_llm, "make_model_factory",
                        lambda **kwargs: None if kwargs["model"] == "test-model-a" else runner.executor.factory)
    assert check_llm.main(["--model", "first", "--model", "substitute", *SERIAL]) == 1
    captured = capsys.readouterr()
    assert row(captured.out, "first")["连通"] == "程序错误"
    assert "- first：本程序出错，不是渠道的问题" in captured.out and "AttributeError: 程序里的错" in captured.out
    assert "Traceback" in captured.err
    assert row(captured.out, "substitute")["连通"] == "正常" and len(runner.calls) == 4
    assert "要处理的（1 个）" in captured.out


def test_cache_verdict_looks_at_the_second_request():
    hit = check_llm.Outcome()
    check_llm.cache_verdict(hit, RequestUsage(input_tokens=5000), RequestUsage(input_tokens=5000, cache_read_tokens=4096))
    assert hit.cache == "有" and not hit.notes
    miss = check_llm.Outcome()
    check_llm.cache_verdict(miss, RequestUsage(input_tokens=5000, cache_read_tokens=4096), RequestUsage(input_tokens=5000))
    assert miss.cache == "没有" and miss.notes


def test_cache_request_failure_counts_as_failure(config_files, monkeypatch, capsys, fixed_code):
    harness = probe_with("chat", [reply(text=CODE), reply(text=COUNT), 503], config_files, monkeypatch)
    assert check_llm.main(["--model", "substitute", *SERIAL]) == 1
    output = capsys.readouterr().out
    cells = row(output, "substitute")
    assert (cells["连通"], cells["并发"], cells["缓存"]) == ("正常", "不查", "没查成")
    assert "- substitute：缓存检查失败：服务端故障（HTTP 503）" in output
    assert len(harness.requests) == 3


def test_speed_failure_counts_as_failure_and_the_rest_still_runs(config_files, monkeypatch, capsys, fixed_code):
    harness = probe_with("chat", [reply(text=CODE), 503, reply(text="好"), reply(text="好")], config_files, monkeypatch)
    assert check_llm.main(["--model", "substitute", *SERIAL]) == 1
    output = capsys.readouterr().out
    cells = row(output, "substitute")
    assert (cells["速度"], cells["缓存"]) == ("没查成", "没有")
    assert "- substitute：测速失败：服务端故障（HTTP 503）" in output
    assert len(harness.requests) == 4


def test_concurrency_check_sends_three_at_once_by_default(config_files, monkeypatch, capsys, fixed_code):
    harness = probe_with("chat", [reply(text=CODE), reply(text=COUNT)] + [reply(text=CODE)] * 3
                         + [reply(text="好")] * 2, config_files, monkeypatch)
    assert check_llm.main(["--model", "substitute", "--verbose"]) == 0
    output = capsys.readouterr().out
    assert "同时发 3 个看并发" in output
    assert "同时 3 个，总耗时" in output
    assert [body for _, body in harness.requests[2:5]] == [harness.requests[0][1]] * 3
    assert len(harness.requests) == harness.closes == 7
    assert row(output, "substitute")["缓存"] == "没有"


def test_concurrency_failure_still_runs_the_cache_check(config_files, monkeypatch, capsys, fixed_code):
    harness = probe_with("chat", [reply(text=CODE), reply(text=COUNT), reply(text=CODE), 429, reply(text="无"),
                                  reply(text=CODE), reply(text="好"), reply(text="好")], config_files, monkeypatch)
    assert check_llm.main(["--model", "substitute", "--concurrency", "4"]) == 1
    output = capsys.readouterr().out
    cells = row(output, "substitute")
    assert (cells["并发"], cells["缓存"]) == ("被限流", "没有")
    assert "- substitute：同时 4 个请求有 2 个失败：服务限流或额度不足（HTTP 429）：bad config。" in output
    assert "要处理的（1 个）" in output
    assert len(harness.requests) == 8


def test_concurrent_reply_without_the_code_is_a_failure(config_files, monkeypatch, capsys, fixed_code):
    probe_with("chat", [reply(text=CODE), reply(text=COUNT), reply(text=CODE), reply(text="无"), reply(text=CODE),
                        reply(text="好"), reply(text="好")], config_files, monkeypatch)
    assert check_llm.main(["--model", "substitute"]) == 1
    output = capsys.readouterr().out
    assert row(output, "substitute")["并发"] == "失败"
    assert "- substitute：同时 3 个请求有 1 个失败：回复里没有核对码。" in output


def test_concurrency_verdict_marks_queueing_past_halfway_to_serial():
    fast, slow = check_llm.Outcome(), check_llm.Outcome()
    check_llm.concurrency_verdict(fast, 4.0, 5.0, [4.0, 4.5, 5.0])
    check_llm.concurrency_verdict(slow, 4.0, 11.0, [4.0, 8.0, 11.0])
    assert (fast.concurrency, fast.notes) == ("正常", [])
    assert slow.concurrency == "排队" and "排队" in slow.notes[0]
