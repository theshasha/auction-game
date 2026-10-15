"""运行输出与本场日志展示。"""

from types import SimpleNamespace

from run_output import print_final_result


def test_final_result_counts_actual_regular_matches(capsys):
    result = SimpleNamespace(
        match_logs=[SimpleNamespace(is_playoff=value) for value in (False, False, True)],
        playoff_count=1,
        playoff_cap_hit=False,
        final_standings=[SimpleNamespace(rank=1, player="甲", total_wins=8, wins_per_match=[3, 3, 2])],
    )
    print_final_result(result)
    assert "共进行 3 局（正赛 2 局 + 加赛 1 局）" in capsys.readouterr().out
