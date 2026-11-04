"""合成公共数据验证离线同步、来源边界与有效策略；没有真实模型名单。"""

from dataclasses import replace
import json

import pytest
import yaml
from auction_game.configuration import load_configuration, load_model_entries
from auction_game.model_capabilities import DEFAULT_MAX_TOKENS, effective_capabilities
from auction_game.cli.sync_models import build_snapshot, main


def public_data():
    return {"official": {"models": {
        "source-a": {"id": "source-a", "limit": {"context": 9999, "output": 900},
                     "reasoning": True, "reasoning_options": [{"type": "toggle"}]},
        "source-b": {"id": "source-b", "limit": {"context": 5000, "output": 0}, "reasoning": False},
        "source-c": {"id": "source-c", "limit": {"context": 9999, "input": 8000, "output": 1900},
                     "reasoning": True, "reasoning_options": [{"type": "effort", "values": ["medium", "high"]}]},
    }}}


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_explicit_reasoning_override_only_changes_selected_model(config_files, effort):
    path = config_files[2]
    original = path.read_text(encoding="utf-8")
    data = yaml.safe_load(original)
    data["models"]["substitute"].update(protocol="chat", reasoning_effort=effort)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    config, normal = load_configuration(*config_files)
    _, testing = load_configuration(*config_files, test_model=True)
    assert all(p.execution.protocol == "chat" and p.execution.model_settings == {"openai_reasoning_effort": effort}
               for p in testing)
    assert config["models"]["substitute"].capabilities["reasoning_status"] == effort
    path.write_text(original, encoding="utf-8")
    _, unchanged = load_configuration(*config_files)
    assert normal == unchanged


@pytest.mark.parametrize("protocol,effort", [("anthropic", "auto"), ("responses", "auto")])
def test_invalid_reasoning_override_rejected(config_files, protocol, effort):
    path = config_files[2]
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["models"]["substitute"].update(protocol=protocol, reasoning_effort=effort)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="reasoning_effort"):
        load_configuration(*config_files)


def test_sync_preview_write_repeat_and_runtime_offline(config_files, tmp_path, capsys, monkeypatch):
    models = config_files[2]
    output = tmp_path / "model_capabilities.json"
    source = tmp_path / "public.json"
    source.write_text(json.dumps(public_data()), encoding="utf-8")
    before = output.read_bytes()
    args = ["--models", str(models), "--input", str(source)]
    assert main(args) == 0 and output.read_bytes() == before
    assert "仅预览" in capsys.readouterr().out
    assert main([*args, "--write"]) == 0
    after = output.read_bytes()
    assert after != before
    assert main([*args, "--write"]) == 0 and output.read_bytes() == after
    monkeypatch.setattr("auction_game.cli.sync_models.urlopen", lambda *a, **kw: pytest.fail("启动必须离线"))
    config, players = load_configuration(*config_files)
    assert [p.execution.max_tokens for p in players] == [900, DEFAULT_MAX_TOKENS]
    assert players[0].execution.capabilities["limit"]["input"] is None
    assert players[0].execution.capabilities["reasoning_status"] == "default"
    assert players[0].execution.model_settings == {}
    assert players[1].execution.capabilities["output_fallback_reason"]
    _, testing = load_configuration(*config_files, test_model=True)
    assert all(p.execution.max_tokens == 1900 and p.execution.model_settings == {"openai_reasoning_effort": "medium"}
               and p.execution.capabilities == config["models"]["substitute"].capabilities for p in testing)


def test_failed_import_keeps_snapshot(config_files, tmp_path):
    output = tmp_path / "model_capabilities.json"
    before = output.read_bytes()
    source = tmp_path / "broken.json"
    source.write_text("not json", encoding="utf-8")
    assert main(["--models", str(config_files[2]), "--input", str(source), "--write"]) == 1
    assert output.read_bytes() == before


def test_network_failure_and_request_headers_keep_snapshot(config_files, tmp_path, monkeypatch):
    output = tmp_path / "model_capabilities.json"
    before = output.read_bytes()
    def fail(request, **kwargs):
        assert request.get_header("User-agent") == "Mozilla/5.0"
        assert request.get_header("Accept") == "application/json"
        raise OSError("offline")
    monkeypatch.setattr("auction_game.cli.sync_models.urlopen", fail)
    assert main(["--models", str(config_files[2]), "--write"]) == 1
    assert output.read_bytes() == before


@pytest.mark.parametrize("output", [None, 0, "900"])
def test_invalid_output_falls_back_without_conflating_input(config_files, output):
    catalog = load_model_entries(config_files[2])
    data = public_data()
    data["official"]["models"]["source-c"]["limit"]["output"] = output
    snapshot = build_snapshot(catalog, data, "synthetic")
    effective = effective_capabilities(catalog["substitute"], snapshot)
    assert effective["max_tokens"] == DEFAULT_MAX_TOKENS
    assert effective["capabilities"]["limit"] == {"context": 9999, "input": 8000, "output": None}
    assert effective["capabilities"]["output_fallback_reason"]


def test_missing_or_changed_source_uses_defaults(config_files):
    catalog = load_model_entries(config_files[2])
    data = public_data()
    data["official"]["models"]["Source-C"] = data["official"]["models"].pop("source-c")
    effective = effective_capabilities(catalog["substitute"], build_snapshot(catalog, data, "synthetic"))
    assert effective["max_tokens"] == DEFAULT_MAX_TOKENS and not effective["model_settings"]
    assert effective["capabilities"]["reasoning_status"] == "default"
    assert "未找到配置指定的精确来源" in effective["capabilities"]["output_fallback_reason"]
    snapshot = build_snapshot(catalog, public_data(), "synthetic")
    original = catalog["substitute"]
    changed = replace(original, models_dev={**original.models_dev, "model": "new-source"})
    effective = effective_capabilities(changed, snapshot)
    assert effective["max_tokens"] == DEFAULT_MAX_TOKENS and not effective["model_settings"]
    assert "来源已变更" in effective["capabilities"]["output_fallback_reason"]


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
def test_medium_uses_protocol_setting_without_manual_binding(config_files, protocol):
    catalog = load_model_entries(config_files[2])
    catalog["substitute"] = replace(catalog["substitute"], protocol=protocol)
    snapshot = build_snapshot(catalog, public_data(), "synthetic")
    effective = effective_capabilities(catalog["substitute"], snapshot)
    assert effective["model_settings"] == ({"anthropic_thinking": {"type": "adaptive"}, "anthropic_effort": "medium"}
                                           if protocol == "anthropic"
                                           else {"openai_reasoning_effort": "medium"})
    assert effective["capabilities"]["reasoning_status"] == "medium"


@pytest.mark.parametrize("options", [[{"type": "toggle"}], [{"type": "budget_tokens", "min": 1, "max": 10000}],
                                     [{"type": "effort", "values": ["high"]}], []])
def test_options_without_medium_keep_channel_default(config_files, options):
    catalog = load_model_entries(config_files[2])
    data = public_data()
    data["official"]["models"]["source-c"]["reasoning_options"] = options
    effective = effective_capabilities(catalog["substitute"], build_snapshot(catalog, data, "synthetic"))
    assert effective["model_settings"] == {}
    assert effective["capabilities"]["reasoning_status"] == "default"
    assert effective["capabilities"]["reasoning_options"] == options


def test_output_budget_is_not_reduced_by_input_or_context(config_files):
    catalog = load_model_entries(config_files[2])
    data = public_data()
    data["official"]["models"]["source-c"]["limit"]["output"] = 9000
    effective = effective_capabilities(catalog["substitute"], build_snapshot(catalog, data, "synthetic"))
    assert effective["max_tokens"] == 9000
