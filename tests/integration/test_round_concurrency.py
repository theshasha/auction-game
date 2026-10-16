"""完整假模型赛的所有任务阶段采用同一份启动并发配置。"""

import pytest
import yaml

from auction_game import runners
from auction_game.ai import ai_speech
from auction_game.cli import offline_demo
from auction_game.configuration import load_configuration
from auction_game.controller import run_parallel_round
from auction_game.reporting.run_paths import RunPaths


@pytest.mark.parametrize("limit", [1, 2])
def test_offline_all_rounds_use_configured_concurrency(config_files, tmp_path, monkeypatch, capsys, limit):
    config_path = config_files[0]
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["llm"]["max_concurrency"] = limit
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    loads = []

    def load(**kwargs):
        loads.append(True)
        return load_configuration(*config_files, **kwargs)

    monkeypatch.setattr(runners, "load_configuration", load)
    monkeypatch.setattr(offline_demo, "load_configuration", load)
    monkeypatch.setattr("auction_game.ai.llm_client._require_env",
                        lambda *args: pytest.fail("离线赛禁止读取真实密钥"))
    rounds = []
    controllers = set()

    def collect(tasks, controller, label):
        assert controller.max_concurrency == limit
        controllers.add(id(controller))
        rounds.append(label)
        return run_parallel_round(tasks, controller, label)

    monkeypatch.setattr(runners, "run_parallel_round", collect)
    monkeypatch.setattr(ai_speech, "run_parallel_round", collect)
    run_dir = tmp_path / "offline"
    offline_demo.generate_demo(run_dir, test_model=True)
    assert loads == [True]
    assert len(controllers) == 1
    assert "开局发言" in rounds
    assert "第一局策略规划（含首日出价）" in rounds
    assert "下一局策略规划（含首日出价）" in rounds
    assert any("局末发言" in label for label in rounds)
    assert any("天出价" in label for label in rounds)
    assert "最终感言" in rounds
    assert "观众总结（不回喂）" in rounds
    assert RunPaths(run_dir).read_config()["max_concurrency"] == limit
    assert f"整轮任务并发上限：{limit}" in capsys.readouterr().out
