"""拍卖分与总分的数据契约；计分规则由引擎测试验证。"""

from dataclasses import asdict
import json

from auction_game.game.models import MatchLog, StandingsEntry
from tests.support.factories import TEST_GAME, make_day, make_log, make_obs, make_standings


def test_match_snapshot_keeps_total_scores_separate_from_auction_scores():
    log = make_log(auction_scores={"甲": 2, "乙": 0}, scores={"甲": 8, "乙": 2}, days=[])
    snapshot = json.loads(json.dumps(log.to_dict()))

    assert snapshot["auction_scores"] == {"甲": 2, "乙": 0}
    assert snapshot["scores"] == {"甲": 8, "乙": 2}
    assert "win_score" not in snapshot["game"]
    assert "wins" not in snapshot
    assert snapshot["game"]["super_hit_score"] == 2
    snapshot["scores"]["甲"] = 99
    assert log.scores["甲"] == 8

    # to_dict 本身也必须隔离容器，不能只依靠 JSON 往返得到副本。
    log.to_dict()["scores"]["甲"] = 100
    assert log.scores["甲"] == 8
    log.to_dict()["auction_scores"]["甲"] = 100
    assert log.auction_scores["甲"] == 2


def test_day_public_view_and_full_log_publish_direct_auction_points():
    day = make_day(1, [5, 4, 3, 2, 1, 0], 6, "甲", 5, [], auction_score=6)
    public = day.public_view()
    full = make_log(days=[day]).to_dict()["days"][0]
    assert public["pool"] == full["pool"] == 6
    assert public["auction_score"] == full["auction_score"] == 6
    assert "wins_taken" not in public
    assert "wins_taken" not in full

    void = make_day(2, [0] * 6, 3, None, None, [0], is_void=True)
    assert void.public_view()["auction_score"] == 0
    assert void.public_view()["winner"] == "流局"


def test_standings_roundtrip_keeps_completed_scores_and_live_total():
    entry = StandingsEntry(
        player="甲", total_auction_score=3, auction_scores_per_match=[1], rank=1,
        total_score=16, scores_per_match=[6],
    )
    snapshot = json.loads(json.dumps(asdict(entry)))
    assert snapshot["total_auction_score"] == 3
    assert snapshot["auction_scores_per_match"] == [1]
    assert snapshot["total_score"] == 16
    assert snapshot["scores_per_match"] == [6]
    assert StandingsEntry(**snapshot) == entry
    snapshot["scores_per_match"].append(10)
    assert entry.scores_per_match == [6]


def test_factories_accept_explicit_total_scores():
    history = {"甲": [6], "乙": [8]}
    standings = make_standings(
        {"甲": [1], "乙": [0]}, scores_history=history, total_scores={"甲": 16, "乙": 8},
    )
    assert {entry.player: entry.total_score for entry in standings} == {"甲": 16, "乙": 8}
    assert [(entry.player, entry.rank) for entry in standings] == [("甲", 1), ("乙", 2)]
    history["甲"].append(20)
    assert next(entry for entry in standings if entry.player == "甲").scores_per_match == [6]

    obs = make_obs(total_auction_score=2, match_score=6, total_score=14)
    assert obs.my_match_score == 6
    assert obs.my_total_score == 14
    assert obs.my_total_auction_score == 2
    snapshot = json.loads(json.dumps(asdict(obs)))
    assert snapshot["my_match_score"] == 6
    assert snapshot["my_total_score"] == 14
    assert snapshot["my_match_auction_score"] == 0
    assert snapshot["my_total_auction_score"] == 2
    assert "my_match_wins" not in snapshot
    assert "my_total_wins" not in snapshot


def test_standings_factory_ranks_explicit_total_scores():
    auction_scores = {"甲": [3], "乙": [1]}
    standings = make_standings(auction_scores, scores_history={"甲": [4], "乙": [6]})
    assert [(entry.player, entry.total_score, entry.rank) for entry in standings] == [
        ("乙", 6, 1), ("甲", 4, 2),
    ]
    assert [entry.total_auction_score for entry in standings] == [1, 3]

    standings = make_standings(auction_scores, total_scores={"甲": 4, "乙": 6})
    assert [(entry.player, entry.total_score, entry.rank) for entry in standings] == [
        ("乙", 6, 1), ("甲", 4, 2),
    ]
    assert [entry.scores_per_match for entry in standings] == [[1], [3]]

    tied = make_standings(auction_scores, scores_history={"甲": [6], "乙": [6]})
    assert [entry.rank for entry in tied] == [1, 1]


def test_empty_score_containers_are_independent():
    first = MatchLog(match_index=1, is_playoff=False, game=TEST_GAME)
    second = MatchLog(match_index=2, is_playoff=False, game=TEST_GAME)
    first.scores["甲"] = 9
    assert second.scores == {}
    assert make_log(auction_scores={"甲": 0, "乙": 0}).scores == {"甲": 0, "乙": 0}

    first_entry = StandingsEntry("甲", 0, [])
    second_entry = StandingsEntry("乙", 0, [])
    first_entry.scores_per_match.append(9)
    assert second_entry.scores_per_match == []
