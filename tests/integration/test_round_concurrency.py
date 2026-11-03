"""完整假模型赛的出价轮和发言轮共用同一个控制器和并发上限。"""

import yaml

from auction_game.cli import offline_demo
from auction_game.configuration import load_configuration
from auction_game.flow.controller import Controller
from auction_game.game.settings import GameSettings
from auction_game.reporting.run_paths import RunPaths


def test_offline_all_rounds_use_configured_concurrency(config_files, tmp_path, monkeypatch, capsys):
    config_path = config_files[0]
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["llm"]["max_concurrency"] = 1
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr(offline_demo, "load_configuration", lambda **kwargs: load_configuration(*config_files, **kwargs))
    monkeypatch.setattr(offline_demo, "load_game_settings", lambda **_: GameSettings(
        initial_coins=7, daily_salary=3, daily_pool=(3, 3), max_bid=7,
        super_cost=2, super_bounty=3, super_hit_score=1, regular_matches=2, max_playoffs=1,
    ))
    controllers = {"bid": set(), "speech": set()}

    run_round = Controller.run_round

    def run(controller, tasks, label):
        assert controller.max_concurrency == 1
        controllers["bid" if label.endswith("出价") else "speech"].add(id(controller))
        return run_round(controller, tasks, label)

    monkeypatch.setattr(Controller, "run_round", run)
    run_dir = tmp_path / "offline"
    offline_demo.generate_demo(run_dir, test_model=True)
    assert len(controllers["bid"]) == len(controllers["speech"]) == 1
    assert controllers["bid"] == controllers["speech"]
    assert RunPaths(run_dir).read_config()["max_concurrency"] == 1
    assert "并发上限：1" in capsys.readouterr().out
