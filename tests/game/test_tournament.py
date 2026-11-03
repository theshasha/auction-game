"""赛程：正赛打完后第一名总分并列就加赛，到上限为止；比赛结束时的结果。"""

from dataclasses import replace

import pytest

from auction_game.game.engine import compute_standings
from auction_game.game.tournament import TournamentResult, first_place_tied, has_next_match
from tests.support.factories import TEST_GAME, scored_log

NAMES = ["甲", "乙", "丙"]
GAME = replace(TEST_GAME, regular_matches=2, max_playoffs=2)
# 一局由一人独得 3 分。
JIA, YI, BING = ([3, 0, 0], [3, 0, 0]), ([0, 3, 0], [0, 3, 0]), ([0, 0, 3], [0, 0, 3])


def played(*matches, game=GAME):
    """matches 是各局的 (拍卖分, 总分)，各按 NAMES 顺序；正赛之后的局是加赛。"""
    return [scored_log(index, dict(zip(NAMES, auction)), dict(zip(NAMES, scores)),
                       game=game, is_playoff=index > game.regular_matches)
            for index, (auction, scores) in enumerate(matches, 1)]


def next_after(logs, game=GAME):
    return has_next_match(logs, compute_standings(NAMES, logs), game)


def test_regular_matches_continue_even_with_unique_leader():
    assert next_after(played(JIA))


@pytest.mark.parametrize("matches, expected", [
    ((JIA, JIA), False),
    ((JIA, YI), True),
    ((JIA, YI, JIA), False),
    ((JIA, YI, BING), True),  # 并列的人变了，仍是第一名并列
    ((JIA, YI, BING, BING), False),
], ids=["regular-unique", "regular-tied", "playoff-decides", "tie-persists", "playoff-decides-later"])
def test_playoffs_continue_while_first_place_is_tied(matches, expected):
    assert next_after(played(*matches)) is expected


def test_playoffs_stop_at_cap_even_if_tied():
    assert next_after(played(JIA, YI, BING)) is True
    logs = played(JIA, YI, BING, ([3, 3, 3], [3, 3, 3]))
    assert first_place_tied(compute_standings(NAMES, logs))
    assert next_after(logs) is False
    assert next_after(played(JIA, YI, game=replace(GAME, max_playoffs=0)), replace(GAME, max_playoffs=0)) is False


@pytest.mark.parametrize("last, expected", [
    (([0, 3, 0], [1, 3, 0]), False),  # 拍卖分并列，命中分让甲总分领先
    (([0, 1, 0], [0, 3, 0]), True),   # 甲拍卖分领先，乙靠命中分总分追平
], ids=["auction-tie-total-unique", "auction-unique-total-tie"])
def test_tie_is_judged_by_total_score(last, expected):
    assert next_after(played(JIA, last)) is expected


def test_first_place_tied_ignores_ties_below_first():
    standings = compute_standings(NAMES, played(([3, 1, 1], [3, 1, 1])))
    assert not first_place_tied(standings)


@pytest.mark.parametrize("matches, playoffs, cap_hit", [
    ((JIA, JIA), 0, False),
    ((JIA, YI, JIA), 1, False),
    ((JIA, YI, BING, ([3, 3, 3], [3, 3, 3])), 2, True),
])
def test_result_counts_playoffs_and_keeps_tied_champions_at_cap(matches, playoffs, cap_hit):
    logs = played(*matches)
    result = TournamentResult(logs, compute_standings(NAMES, logs))
    assert result.playoff_count == playoffs
    assert result.playoff_cap_hit is cap_hit
