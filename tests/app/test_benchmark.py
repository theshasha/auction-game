"""批量对照入口、合法策略与样本比较；全部离线且不写项目日志。"""

from types import SimpleNamespace

import pytest

from auction_game.game import benchmark
from auction_game.reporting import run_output
from auction_game.configuration import ConfigurationError
from tests.support.factories import make_day, make_log, make_obs, make_standings
from auction_game.game.game_rules import GameSettings
from auction_game.game.players import BOT_NAMES


def test_benchmark_uses_one_game_without_logs(monkeypatch, capsys, tmp_path):
    game = GameSettings(13, 4, (2, 1, 3), 9, 1, 5, 2, 0)
    run_tournament = benchmark.run_tournament
    loaded = []
    calls = []
    strategy_records = []

    def load():
        loaded.append(True)
        return game

    def tournament(players, actual_game):
        assert actual_game is game
        calls.append([p.name for p in players])
        if players[0].name == "最高可付价玩家":
            for coins, expected_bid in [(13, 9), (9, 9), (4, 4), (0, 0)]:
                obs = make_obs(my_name=players[0].name, coins=coins, game=game)
                assert players[0].decide_bid(obs) == (expected_bid, None)
        result = run_tournament(players, actual_game)
        strategy_records.extend(
            record for log in result.match_logs for day in log.days for record in day.bids
            if record.player == "最高可付价玩家"
        )
        return result

    monkeypatch.setattr(benchmark, "load_game_settings", load)
    monkeypatch.setattr(benchmark, "run_tournament", tournament)
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(run_output, "LOG_DIR", log_dir)
    assert benchmark.run_benchmark(2) == 0

    assert loaded == [True]
    assert not log_dir.exists()
    assert calls == [BOT_NAMES] * 2 + [["最高可付价玩家"] + BOT_NAMES[:5]] * 2
    assert strategy_records
    assert all(record.status == "ok" and not record.is_super for record in strategy_records)
    output = capsys.readouterr().out
    assert "两组各 2 场，共 4 场" in output
    assert "每日最高可付价（上限 9，不使用超级出价）+ 5 随机" in output
    assert "获胜价=9（出价上限）" in output
    assert "以下比较均取第二组" in output


def test_game_error_stops_before_tournament(monkeypatch, capsys):
    def load():
        raise ConfigurationError("game.yaml：max_bid 必须是非负整数")

    def tournament(*args, **kwargs):
        raise AssertionError("参数错误时不应启动比赛")

    monkeypatch.setattr(benchmark, "load_game_settings", load)
    monkeypatch.setattr(benchmark, "run_tournament", tournament)
    assert benchmark.run_benchmark(1) == 1
    assert "配置读取失败：game.yaml：max_bid" in capsys.readouterr().out


@pytest.mark.parametrize("count", [0, -1, True, 1.5, "2", None])
def test_invalid_count_fails_before_loading_configuration(monkeypatch, capsys, count):
    monkeypatch.setattr(benchmark, "load_game_settings", lambda: pytest.fail("非法场数不应加载配置"))
    monkeypatch.setattr(benchmark, "run_tournament", lambda *args: pytest.fail("非法场数不应启动比赛"))
    assert benchmark.run_benchmark(count) == 1
    assert "场数必须是正整数" in capsys.readouterr().out


@pytest.mark.parametrize("strategy_wins,comparison", [(6, "高于"), (5, "等于"), (4, "低于")])
def test_comparison_describes_only_second_group(monkeypatch, capsys, strategy_wins, comparison):
    game = GameSettings(13, 4, (2, 1, 3), 9, 1, 5, 2, 0)
    monkeypatch.setattr(benchmark, "load_game_settings", lambda: game)
    calls = []

    def tournament(players, actual_game):
        calls.append([p.name for p in players])
        wins = {p.name: [100] for p in players}
        if players[0].name == "最高可付价玩家":
            wins = {p.name: [5] for p in players}
            wins[players[0].name] = [strategy_wins]
        return SimpleNamespace(
            playoff_count=0,
            match_logs=[
                make_log(game=game, days=[
                    make_day(1, [6, 5, 4, 3, 2, 0], 3, "甲", 6, [], super_bids={"乙": 8}),
                    make_day(2, [0] * 6, 12, None, None, [0], is_void=True),
                ]),
                make_log(match_index=2, game=game, days=[
                    make_day(1, [9] * 6, 9, None, None, [9], is_void=True),
                ]),
            ],
            final_standings=make_standings(wins),
        )

    monkeypatch.setattr(benchmark, "run_tournament", tournament)
    assert benchmark.run_benchmark(1) == 0
    assert len(calls) == 2
    output = capsys.readouterr().out
    assert f"最高可付价玩家平均总胜场 {strategy_wins:.2f}" in output
    assert "随机对手平均 5.00" in output
    assert "奖池均值包含全部日期（含流局日）" in output
    assert ("第 1 天：非流局日全部有效出价均值（含超级出价数值） 4.00，"
            "奖池均值（含流局日） 6.00") in output
    assert ("第 2 天：非流局日全部有效出价均值（含超级出价数值） —（全流局），"
            "奖池均值（含流局日） 12.00") in output
    assert f"本批样本中，最高可付价玩家平均胜场{comparison}随机对手。" in output
    assert all(term not in output for term in ["显著", "占优", "规则需要", "极点", "Whale", "鲸鱼"])
