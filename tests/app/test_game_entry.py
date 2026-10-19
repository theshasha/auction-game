"""比赛入口的参数传递与本场规则保存，全部离线执行。"""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

import main
from auction_game import runners
from auction_game.configuration import ConfigurationError
from auction_game.game.game_rules import GameSettings
from auction_game.game.players import BOT_NAMES
from auction_game.game.rules import render_rules


@pytest.fixture
def game():
    return GameSettings(
        initial_coins=13, daily_salary=4, daily_pool=(2, 1, 3), max_bid=9,
        super_cost=1, super_bounty=5, super_hit_score=2,
        regular_matches=2, max_playoffs=0,
    )


def test_bots_load_once_and_save_actual_rules(monkeypatch, tmp_path, game):
    loaded = []
    result = object()

    def load():
        loaded.append(True)
        return game

    def tournament(players, actual_game, **kwargs):
        assert actual_game is game
        assert [p.name for p in players] == BOT_NAMES
        assert callable(kwargs["on_match_end"])
        return result

    monkeypatch.setattr(runners, "load_game_settings", load)
    monkeypatch.setattr(runners, "make_run_dir", lambda mode: tmp_path / "run")
    monkeypatch.setattr(runners, "run_tournament", tournament)
    printed = []
    monkeypatch.setattr(runners, "print_final_result", printed.append)
    assert runners.run_bots(42) == 0

    assert loaded == [True]
    assert printed == [result]
    assert (tmp_path / "run" / "RULES.md").read_text(encoding="utf-8") == render_rules(game, BOT_NAMES)


@pytest.mark.parametrize("entry,args", [
    (runners.run_bots, (42,)),
    (runners.run_ai, ()),
])
def test_game_errors_stop_before_clients_or_logs(monkeypatch, capsys, entry, args):
    def load():
        raise ConfigurationError("game.yaml：max_bid 必须是非负整数")

    monkeypatch.setattr(runners, "load_game_settings", load)
    monkeypatch.setattr(runners, "load_configuration", lambda **kwargs: ({}, []))
    monkeypatch.setattr(runners, "make_model_factory", lambda **kwargs: pytest.fail("参数错误时不应创建客户端"))
    monkeypatch.setattr(runners, "make_run_dir", lambda mode: pytest.fail("参数错误时不应创建日志"))
    assert entry(*args) == 1
    assert "[配置错误] game.yaml：max_bid" in capsys.readouterr().out


@pytest.mark.parametrize("args,expected", [
    (["--seed", "42"], ("bots", 42)),
    (["--ai"], ("ai", False)),
    (["--benchmark", "2"], ("benchmark", 2)),
])
@pytest.mark.parametrize("status", [0, 1])
def test_main_dispatches_mode(monkeypatch, args, expected, status):
    calls = []

    def record(*call):
        calls.append(call)
        return status

    monkeypatch.setattr("sys.argv", ["main.py", *args])
    monkeypatch.setattr(runners, "run_bots", lambda seed: record("bots", seed))
    monkeypatch.setattr(runners, "run_ai", lambda test_model: record("ai", test_model))
    monkeypatch.setattr(main.benchmark, "run_benchmark", lambda count: record("benchmark", count))

    assert main.main() == status

    assert calls == [expected]


@pytest.mark.parametrize("count", ["0", "-1", "1.5", "abc"])
def test_main_rejects_invalid_benchmark_before_dispatch(monkeypatch, capsys, count):
    def unexpected(*args):
        pytest.fail("非法场数不应启动比赛")

    monkeypatch.setattr("sys.argv", ["main.py", "--benchmark", count])
    monkeypatch.setattr(runners, "run_bots", unexpected)
    monkeypatch.setattr(runners, "run_ai", unexpected)
    monkeypatch.setattr(main.benchmark, "run_benchmark", unexpected)
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2
    assert "场数必须是正整数" in capsys.readouterr().err


def test_main_dispatches_test_model_and_rejects_other_modes(monkeypatch):
    calls = []
    monkeypatch.setattr(runners, "run_ai", lambda **kwargs: calls.append(kwargs) or 0)
    monkeypatch.setattr(sys, "argv", ["main.py", "--ai", "--test-model"])
    assert main.main() == 0 and calls == [{"test_model": True}]
    for args in (["--test-model"], ["--ai", "--test-model", "--benchmark", "1"]):
        monkeypatch.setattr(sys, "argv", ["main.py", *args])
        with pytest.raises(SystemExit) as exc:
            main.main()
        assert exc.value.code == 2
    assert len(calls) == 1


@pytest.mark.parametrize("args,valid_game,status,writes_logs", [
    (["--seed", "42"], True, 0, True),
    (["--benchmark", "1"], True, 0, False),
    ([], False, 1, False),
    (["--ai"], False, 1, False),
    (["--benchmark", "1"], False, 1, False),
    (["--benchmark", "0"], True, 2, False),
    (["--benchmark", "-1"], True, 2, False),
    (["--benchmark", "1.5"], True, 2, False),
    (["--benchmark", "abc"], True, 2, False),
])
def test_script_exit_codes_in_isolated_checkout(tmp_path, game, args, valid_game, status, writes_logs):
    root = Path(main.__file__).parent
    for directory in ("auction_game", "configs", "data"):
        shutil.copytree(root / directory, tmp_path / directory, ignore=shutil.ignore_patterns("__pycache__"))
    for pattern in ("main.py", "RULES.template.md"):
        for path in root.glob(pattern):
            shutil.copy2(path, tmp_path / path.name)
    (tmp_path / "configs" / "game.yaml").write_text(
        yaml.safe_dump(game.to_dict()) if valid_game else "max_bid: -1\n",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    process = subprocess.run(
        [sys.executable, "-B", str(tmp_path / "main.py"), *args], cwd=elsewhere,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert process.returncode == status, process.stdout + process.stderr
    assert (tmp_path / "logs").exists() is writes_logs
    if status == 2:
        assert "场数必须是正整数" in process.stderr
