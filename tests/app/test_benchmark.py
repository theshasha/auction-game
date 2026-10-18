"""批量对照入口、合法策略与样本比较；全部离线且不写项目日志。"""

from types import SimpleNamespace

import pytest

from auction_game.game import benchmark
from auction_game.reporting import run_output
from auction_game.configuration import ConfigurationError
from tests.support.factories import make_day, make_log, make_obs, make_standings
from auction_game.game.game_rules import GameSettings
from auction_game.game.players import BOT_NAMES


@pytest.fixture(autouse=True)
def isolate_root_rules(tmp_path, monkeypatch):
    monkeypatch.setattr("auction_game.game.rules.RULES_PATH", tmp_path / "RULES.md")


def test_benchmark_uses_one_game_without_logs(monkeypatch, capsys, tmp_path):
    game = GameSettings(
        initial_coins=13, daily_salary=4, daily_pool=(2, 1, 3), max_bid=9,
        super_cost=1, super_bounty=5, super_hit_score=2,
        regular_matches=2, max_playoffs=0,
    )
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
    assert "最高可付价对照：2 场（上限 9，无超级，5 个随机对手）" in output
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


@pytest.mark.parametrize("strategy_score,rank", [
    pytest.param(26, 1, id="higher"),
    pytest.param(25, 1, id="equal"),
    pytest.param(24, 6, id="lower"),
])
def test_comparison_describes_only_second_group(monkeypatch, capsys, strategy_score, rank):
    game = GameSettings(
        initial_coins=13, daily_salary=4, daily_pool=(2, 1, 3), max_bid=9,
        super_cost=1, super_bounty=5, super_hit_score=2,
        regular_matches=2, max_playoffs=0,
    )
    monkeypatch.setattr(benchmark, "load_game_settings", lambda: game)
    calls = []

    def tournament(players, actual_game):
        calls.append([p.name for p in players])
        auction_scores = {p.name: [100] for p in players}
        scores = {p.name: [400] for p in players}
        if players[0].name == "最高可付价玩家":
            auction_scores = {p.name: [5] for p in players}
            auction_scores[players[0].name] = [6]
            scores = {p.name: [25] for p in players}
            scores[players[0].name] = [strategy_score]
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
            final_standings=make_standings(auction_scores, scores_history=scores),
        )

    monkeypatch.setattr(benchmark, "run_tournament", tournament)
    assert benchmark.run_benchmark(1) == 0
    assert len(calls) == 2
    output = capsys.readouterr().out
    assert f"最高可付价玩家平均总分 {strategy_score:.2f} 分（平均排名 {rank:.2f}）" in output
    assert "随机对手平均总分 25.00 分" in output
    explanation = "均价不含流局日，含超级数值；奖池均值含流局日。"
    assert output.count(explanation) == 1
    assert output.index(explanation) < output.index("===== 纯随机对照")
    assert output.count("第 1 天：平均出价 4.00，平均拍卖奖池 6.00 分") == 2
    assert output.count("第 2 天：平均出价 —（全流局），平均拍卖奖池 12.00 分") == 2
    assert "本批样本" not in output
    assert "胜场" not in output
    assert all(term not in output for term in ["显著", "占优", "规则需要", "极点", "Whale", "鲸鱼"])
