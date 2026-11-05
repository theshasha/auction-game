"""连通检查的目标、预算提示、系统提示探测和失败处理。"""

from dataclasses import replace
import json

import pytest

from auction_game.cli import check_llm
from auction_game.configuration import load_configuration
from tests.support.factories import fake_task_runner
from tests.support.wire import Harness, PROTOCOLS, reply

CODE = "c0de1234"


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


def test_help_names_model_configuration_and_separate_check_budget(capsys):
    with pytest.raises(SystemExit) as exc:
        check_llm.main(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "模型配置名" in output
    assert "不改变比赛预算" in output


def test_check_reports_targets_cost_and_budget(config_files, monkeypatch, capsys, fixed_code):
    monkeypatch.setattr(check_llm, "load_configuration", lambda **kwargs: load_configuration(*config_files, **kwargs))
    runner = fake_task_runner([CODE])
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return runner.executor.factory

    monkeypatch.setattr(check_llm, "make_model_factory", factory)
    assert check_llm.main(["--model", "substitute", "--model", "substitute", "--max-tokens", "100"]) == 0
    output = capsys.readouterr().out
    assert "1 个检查目标，会请求真实服务，可能收费" in output
    assert "每个目标至多请求一次，不重试" in output
    assert "比赛预算=789，本次探测预算=100" in output
    assert len(calls) == len(runner.calls) == 1
    assert calls[0]["max_tokens"] == 100 and calls[0]["request_attempts"] == 1
    assert check_llm.main(["--model", "absent"]) == 1
    assert "models.yaml：未知模型配置名 'absent'" in capsys.readouterr().out
    assert len(calls) == 1


def test_test_model_checks_one_target_with_its_settings(config_files, monkeypatch, fixed_code):
    monkeypatch.setattr(check_llm, "load_configuration", lambda **kwargs: load_configuration(*config_files, **kwargs))
    runner = fake_task_runner([CODE])
    calls = []
    monkeypatch.setattr(check_llm, "make_model_factory",
                        lambda **kwargs: calls.append(kwargs) or runner.executor.factory)
    assert check_llm.main(["--test-model"]) == 0
    assert [(call["model"], call["protocol"], call["model_settings"]) for call in calls] == [
        ("test-model-c", "responses", {"openai_reasoning_effort": "medium"})]


def test_code_changes_between_runs_and_only_the_system_prompt_has_it(config_files, monkeypatch):
    monkeypatch.setattr(check_llm, "load_configuration", lambda **kwargs: load_configuration(*config_files, **kwargs))
    runner = fake_task_runner(["无", "无"])
    monkeypatch.setattr(check_llm, "make_model_factory", lambda **kwargs: runner.executor.factory)
    check_llm.main(["--model", "substitute"])
    check_llm.main(["--model", "substitute"])
    (first_system, first_prompt), (second_system, _) = [
        [part.content for message in call for part in message.parts] for call in runner.calls]
    assert first_system != second_system
    code = first_system.split("核对码是 ")[1].rstrip("。")
    assert code not in first_prompt


@pytest.mark.parametrize(("protocol", "input_tokens"), [("chat", 0), ("responses", 2), ("anthropic", 2)])
def test_probe_sends_the_code_and_passes_when_it_comes_back(
        protocol, input_tokens, config_files, monkeypatch, capsys, fixed_code):
    harness = probe_with(protocol, [reply(text=f"核对码：{CODE}")], config_files, monkeypatch)
    assert check_llm.main(["--model", "substitute"]) == 0
    output = capsys.readouterr().out
    assert "[通过]" in output
    assert f"输入 {input_tokens} token" in output
    assert f"回复: 核对码：{CODE}" in output
    assert CODE in json.dumps(harness.requests[0][1])
    assert len(harness.requests) == harness.closes == 1


@pytest.mark.parametrize("text", ["OK", "无"])
def test_reply_without_the_code_means_the_system_prompt_was_lost(text, config_files, monkeypatch, capsys, fixed_code):
    harness = probe_with("chat", [reply(text=text)], config_files, monkeypatch)
    assert check_llm.main(["--model", "substitute"]) == 1
    output = capsys.readouterr().out
    assert "[系统提示未生效]" in output
    assert f"回复: {text}" in output
    assert "失败 1 个" in output
    assert len(harness.requests) == 1


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_probe_transport_failure_has_no_retry(protocol, config_files, monkeypatch, capsys):
    harness = probe_with(protocol, ["connect"], config_files, monkeypatch)
    assert check_llm.main(["--model", "substitute"]) == 1
    assert "[调用失败]" in capsys.readouterr().out
    assert len(harness.requests) == harness.closes == 1
