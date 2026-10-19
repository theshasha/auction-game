"""连通检查的用户提示；使用假模型，不调用真实服务。"""

from dataclasses import replace

import pytest

from auction_game.cli import check_llm
from auction_game.configuration import load_configuration
from tests.support.factories import fake_task_runner
from tests.support.wire import Harness, PROTOCOLS, reply


def test_help_names_model_configuration_and_separate_check_budget(capsys):
    with pytest.raises(SystemExit) as exc:
        check_llm.main(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "模型配置名" in output and "目录键" not in output
    assert "不改变比赛预算" in output


def test_check_reports_targets_cost_and_budget(config_files, monkeypatch, capsys):
    monkeypatch.setattr(check_llm, "load_configuration", lambda: load_configuration(*config_files))
    runner = fake_task_runner(["OK"])
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return runner.executor.factory

    monkeypatch.setattr(check_llm, "make_model_factory", factory)
    assert check_llm.main(["--model", "substitute", "--max-tokens", "100"]) == 0
    output = capsys.readouterr().out
    assert "1 个检查目标，会请求真实服务，可能收费" in output
    assert "每个目标至多请求一次，不重试" in output
    assert "比赛预算=789，本次探测预算=100" in output
    assert len(calls) == len(runner.calls) == 1
    assert calls[0]["max_tokens"] == 100 and calls[0]["request_attempts"] == 1
    assert check_llm.main(["--model", "absent"]) == 1
    assert "models.yaml：未知模型配置名 'absent'" in capsys.readouterr().out
    assert len(calls) == 1


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("text", ["OK", " OK", "OK\n", "ok", '{"reply":"OK"}', "pong"])
def test_probe_accepts_only_exact_ok_with_no_tools_or_correction(
    protocol, text, config_files, monkeypatch, capsys,
):
    config, players = load_configuration(*config_files)
    config["models"]["substitute"]["protocol"] = protocol
    monkeypatch.setattr(check_llm, "load_configuration", lambda: (config, players))
    harness = Harness(protocol, [reply(text=text)])

    def factory(**kwargs):
        assert kwargs["request_attempts"] == 1
        return replace(harness.factory, request_attempts=kwargs["request_attempts"])

    monkeypatch.setattr(check_llm, "make_model_factory", factory)
    assert check_llm.main(["--model", "substitute"]) == (0 if text == "OK" else 1)
    output = capsys.readouterr().out
    assert ("[通过]" if text == "OK" else "[调用失败]") in output
    assert len(harness.requests) == harness.closes == 1
    assert all(stream.closed for stream in harness.streams)
    body = harness.requests[0][1]
    assert "tools" not in body and "tool_choice" not in body
    assert "response_format" not in body
    if protocol == "responses":
        assert "format" not in body.get("text", {})


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_probe_transport_failure_has_no_retry(protocol, config_files, monkeypatch, capsys):
    config, players = load_configuration(*config_files)
    config["models"]["substitute"]["protocol"] = protocol
    monkeypatch.setattr(check_llm, "load_configuration", lambda: (config, players))
    harness = Harness(protocol, ["connect"])
    monkeypatch.setattr(check_llm, "make_model_factory", lambda **kwargs: replace(
        harness.factory, request_attempts=kwargs["request_attempts"],
    ))
    assert check_llm.main(["--model", "substitute"]) == 1
    assert "[调用失败]" in capsys.readouterr().out
    assert len(harness.requests) == harness.closes == 1
