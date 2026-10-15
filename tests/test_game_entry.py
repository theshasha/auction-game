"""比赛入口的参数传递与本场规则保存，全部离线执行。"""

from types import SimpleNamespace

import pytest

import main
from configuration import ConfigurationError
from game_rules import GameSettings
from players import ScriptedPlayer


@pytest.fixture
def game():
    return GameSettings(13, 4, (2, 1, 3), 9, 1, 5, 2, 0)


def test_bots_load_once_and_save_actual_rules(monkeypatch, tmp_path, game):
    loaded = []
    result = object()

    def load():
        loaded.append(True)
        return game

    def tournament(players, actual_game, **kwargs):
        assert actual_game is game
        assert [p.name for p in players] == main.BOT_NAMES
        assert callable(kwargs["on_match_end"])
        return result

    monkeypatch.setattr(main, "load_game_settings", load)
    monkeypatch.setattr(main, "make_run_dir", lambda mode: tmp_path)
    monkeypatch.setattr(main, "run_tournament", tournament)
    printed = []
    monkeypatch.setattr(main, "print_final_result", printed.append)
    main.run_bots(42)

    assert loaded == [True]
    assert printed == [result]
    assert (tmp_path / "RULES.md").read_text(encoding="utf-8") == main.render_rules(game, main.BOT_NAMES)


def test_benchmark_uses_one_game_without_logs(monkeypatch, capsys, game):
    run_tournament = main.run_tournament
    loaded = []
    calls = []

    def load():
        loaded.append(True)
        return game

    def tournament(players, actual_game):
        assert actual_game is game
        calls.append([p.name for p in players])
        if players[0].name == "Whale":
            assert players[0].decide_bid(None) == (9, None)
        return run_tournament(players, actual_game)

    monkeypatch.setattr(main, "load_game_settings", load)
    monkeypatch.setattr(main, "run_tournament", tournament)
    monkeypatch.setattr(main, "make_run_dir", lambda mode: pytest.fail("benchmark 不应写日志"))
    main.run_benchmark(2)

    assert loaded == [True]
    assert calls == [main.BOT_NAMES] * 2 + [["Whale"] + main.BOT_NAMES[:5]] * 2
    output = capsys.readouterr().out
    assert "Whale 恒出 9 + 5 随机" in output
    assert "获胜价=9（极点）" in output


@pytest.mark.parametrize("entry,args", [
    (main.run_bots, (42,)),
    (main.run_benchmark, (1,)),
    (main.run_ai, ()),
])
def test_game_errors_stop_before_clients_or_logs(monkeypatch, capsys, entry, args):
    def load():
        raise ConfigurationError("game.yaml：max_bid 必须是非负整数")

    monkeypatch.setattr(main, "load_game_settings", load)
    monkeypatch.setattr(main, "load_configuration", lambda: ({}, []))
    monkeypatch.setattr(main, "make_chat_fn", lambda **kwargs: pytest.fail("参数错误时不应创建客户端"))
    monkeypatch.setattr(main, "make_run_dir", lambda mode: pytest.fail("参数错误时不应创建日志"))
    entry(*args)
    assert "配置读取失败：game.yaml：max_bid" in capsys.readouterr().out


def test_ai_single_player_stops_before_clients_or_logs(monkeypatch, capsys, game):
    monkeypatch.setattr(main, "load_configuration", lambda: (
        {"llm": {"timeout_seconds": 30}},
        [{"name": "甲", "provider": "openai", "model": "test-model"}],
    ))
    monkeypatch.setattr(main, "load_game_settings", lambda: game)
    monkeypatch.setattr(main, "make_chat_fn", lambda **kwargs: pytest.fail("一人名单不应创建客户端"))
    monkeypatch.setattr(main, "make_run_dir", lambda mode: pytest.fail("一人名单不应创建日志"))

    main.run_ai()

    assert "错误：AI 对局至少需要 2 名玩家，请检查 players.yaml" in capsys.readouterr().out


def test_final_result_counts_actual_regular_matches(capsys):
    result = SimpleNamespace(
        match_logs=[SimpleNamespace(is_playoff=value) for value in (False, False, True)],
        playoff_count=1,
        playoff_cap_hit=False,
        final_standings=[SimpleNamespace(rank=1, player="甲", total_wins=8, wins_per_match=[3, 3, 2])],
    )
    main.print_final_result(result)
    assert "共进行 3 局（正赛 2 局 + 加赛 1 局）" in capsys.readouterr().out


def test_speech_director_passes_game_to_both_plans(monkeypatch, tmp_path, game):
    players = [ScriptedPlayer.constant(name, 0) for name in ("甲", "乙")]
    director = main.AISpeechDirector(players, None, tmp_path, game=game)
    plans = []

    def request(others, actual_game):
        assert actual_game is game
        plans.append(others)
        return "规划"

    monkeypatch.setattr(main.prompts, "opening_plan_request", request)
    monkeypatch.setattr(main.prompts, "next_match_plan_request", request)
    monkeypatch.setattr(main.prompts, "match_end_speech_request", lambda *args: "局末发言")
    monkeypatch.setattr(director, "_run_round", lambda *args: {"甲": "发言甲", "乙": "发言乙"})
    monkeypatch.setattr(director, "_publish_speeches", lambda *args: None)

    director.on_opening()
    director.on_match_end(SimpleNamespace(match_index=1), [], True)
    assert plans == [{"乙": "发言乙"}, {"甲": "发言甲"}] * 2
    director.on_match_end(SimpleNamespace(match_index=2), [], False)
    assert len(plans) == 4
