"""连通检查的用户提示；使用假模型，不调用真实服务。"""

import pytest

from auction_game.cli import check_llm
from auction_game.configuration import load_configuration
from tests.support.factories import fake_task_runner


def test_help_names_model_configuration_and_separate_check_budget(capsys):
    with pytest.raises(SystemExit) as exc:
        check_llm.main(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "模型配置名" in output and "目录键" not in output
    assert "不改变比赛预算" in output


def test_check_reports_targets_cost_budget_and_compatibility(config_files, monkeypatch, capsys):
    monkeypatch.setattr(check_llm, "load_configuration", lambda: load_configuration(*config_files))
    runner = fake_task_runner([{"reply": "OK"}])
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
    assert "生效兼容设置=" in output and "supports_forced_tool_choice" in output
    assert len(calls) == len(runner.calls) == 1
    assert calls[0]["max_tokens"] == 100 and calls[0]["request_attempts"] == 1
    assert check_llm.main(["--model", "absent"]) == 1
    assert "models.yaml：未知模型配置名 'absent'" in capsys.readouterr().out
    assert len(calls) == 1
