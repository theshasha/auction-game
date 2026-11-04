"""阵容的名单和阵营。"""

from auction_game.game.lineup import Lineup, PlayerProfile


def profile(name, team=None):
    return PlayerProfile(name, "无", "无", None if team else 10, team)


def test_team_lineup_groups_players_in_seat_order():
    lineup = Lineup((profile("甲", "红队"), profile("乙", "蓝队"), profile("丙", "红队"), profile("丁", "蓝队")))
    assert lineup.teams == {"红队": ("甲", "丙"), "蓝队": ("乙", "丁")}
    assert lineup.team_of == {"甲": "红队", "乙": "蓝队", "丙": "红队", "丁": "蓝队"}


def test_individual_lineup_has_no_teams():
    lineup = Lineup((profile("甲"), profile("乙")))
    assert lineup.teams == {}
    assert lineup.team_of is None
