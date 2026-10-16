"""合成公共数据验证离线同步、来源边界与有效策略；没有真实模型名单。"""

from copy import deepcopy
import json

import pytest
from auction_game.configuration import load_configuration, load_model_catalog
from auction_game.model_capabilities import DEFAULT_MAX_TOKENS, effective_capabilities
from auction_game.cli.sync_models import build_snapshot, main


def public_data():
    return {"official": {"models": {
        "source-a": {"id": "source-a", "limit": {"context": 9999, "output": 900},
                     "reasoning": True, "reasoning_options": [{"type": "toggle"}], "tool_call": True},
        "source-b": {"id": "source-b", "limit": {"context": 5000, "output": 0}, "reasoning": False},
        "source-c": {"id": "source-c", "limit": {"context": 9999, "input": 8000, "output": 1900},
                     "reasoning": True, "reasoning_options": [{"type": "effort", "values": ["high"]}]},
    }}}


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
    assert [p["max_tokens"] for p in players] == [900, DEFAULT_MAX_TOKENS]
    assert players[0]["capabilities"]["limit"]["input"] is None
    assert players[0]["capabilities"]["reasoning_status"] == "no_high"
    assert players[0]["model_settings"] == {}
    assert players[1]["capabilities"]["output_fallback_reason"]
    _, testing = load_configuration(*config_files, test_model=True)
    assert all(p["max_tokens"] == 1900 and p["model_settings"] == {"openai_reasoning_effort": "high"}
               and p["capabilities"] == config["models"]["substitute"]["capabilities"] for p in testing)


@pytest.mark.parametrize("bad", ["not json", "[]", '{"official":{"models":[]}}',
                                  '{"official":{"models":{},"models":{}}}',
                                  '{"official":{"models":{"source-a":{"id":"wrong","limit":{}}}}}',
                                  '{"official":{"models":{"source-a":{"id":"source-a","limit":{},"reasoning":"yes"}}}}',
                                  '{"official":{"models":{"source-a":{"id":"source-a","limit":{},"reasoning_options":{}}}}}'])
def test_failed_import_keeps_snapshot(config_files, tmp_path, bad):
    output = tmp_path / "model_capabilities.json"
    before = output.read_bytes()
    source = tmp_path / "broken.json"
    source.write_text(bad, encoding="utf-8")
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


@pytest.mark.parametrize("output", [None, 0, -1, True, 1.5, "900"])
def test_invalid_output_falls_back_without_conflating_input(config_files, output):
    catalog = load_model_catalog(config_files[2], resolve_capabilities=False)
    data = public_data()
    data["official"]["models"]["source-c"]["limit"]["output"] = output
    snapshot = build_snapshot(catalog, data, "synthetic")
    effective = effective_capabilities("substitute", catalog["substitute"], snapshot)
    assert effective["max_tokens"] == DEFAULT_MAX_TOKENS
    assert effective["capabilities"]["limit"] == {"context": 9999, "input": 8000, "output": None}
    assert effective["capabilities"]["output_fallback_reason"]


def test_exact_source_missing_canonical_guard_and_unknown_reasoning(config_files):
    catalog = load_model_catalog(config_files[2], resolve_capabilities=False)
    data = public_data()
    item = data["official"]["models"].pop("source-c")
    data["official"]["models"]["Source-C"] = item
    snapshot = build_snapshot(catalog, data, "synthetic")
    effective = effective_capabilities("substitute", catalog["substitute"], snapshot)
    assert effective["max_tokens"] == DEFAULT_MAX_TOKENS and not effective["model_settings"]
    assert effective["capabilities"]["reasoning_status"] == "unknown"
    data = public_data()
    source = catalog["substitute"]["models_dev"]
    source["canonical_model_id"] = "official/expected"
    with pytest.raises(ValueError, match="canonical_model_id"):
        build_snapshot(catalog, data, "synthetic")
    data["official"]["models"]["source-c"]["canonical_model_id"] = "official/expected"
    data["official"]["models"]["source-c"].pop("reasoning_options")
    snapshot = build_snapshot(catalog, data, "synthetic")
    assert effective_capabilities("substitute", catalog["substitute"], snapshot)["capabilities"]["reasoning_status"] == "unknown"
    changed = deepcopy(catalog["substitute"])
    changed["models_dev"]["model"] = "new-source"
    assert effective_capabilities("substitute", changed, snapshot)["max_tokens"] == DEFAULT_MAX_TOKENS


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
def test_high_uses_protocol_setting_without_manual_binding(config_files, protocol):
    catalog = load_model_catalog(config_files[2], resolve_capabilities=False)
    catalog["substitute"]["protocol"] = protocol
    snapshot = build_snapshot(catalog, public_data(), "synthetic")
    effective = effective_capabilities("substitute", catalog["substitute"], snapshot)
    assert effective["model_settings"] == ({} if protocol == "anthropic" else {"openai_reasoning_effort": "high"})
    assert effective["capabilities"]["reasoning_status"] == ("unknown" if protocol == "anthropic" else "high")


@pytest.mark.parametrize("options", [[{"type": "toggle"}], [{"type": "budget_tokens", "min": 1, "max": 10000}],
                                     [{"type": "effort", "values": ["low", "medium"]}]])
def test_options_without_high_are_reported_without_conversion(config_files, options):
    catalog = load_model_catalog(config_files[2], resolve_capabilities=False)
    data = public_data()
    data["official"]["models"]["source-c"]["reasoning_options"] = options
    effective = effective_capabilities("substitute", catalog["substitute"], build_snapshot(catalog, data, "synthetic"))
    assert effective["model_settings"] == {}
    assert effective["capabilities"]["reasoning_status"] == "no_high"
    assert effective["capabilities"]["reasoning_options"] == options


def test_output_budget_is_not_reduced_by_input_or_context(config_files):
    catalog = load_model_catalog(config_files[2], resolve_capabilities=False)
    data = public_data()
    data["official"]["models"]["source-c"]["limit"]["output"] = 9000
    effective = effective_capabilities("substitute", catalog["substitute"], build_snapshot(catalog, data, "synthetic"))
    assert effective["max_tokens"] == 9000
