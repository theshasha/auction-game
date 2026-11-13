"""离线假模型赛的测试准备：合成配置、阵容和比赛参数。"""

from copy import deepcopy
from dataclasses import replace

from auction_game import runners
from auction_game.configuration import load_configuration
from auction_game.game.settings import GameSettings

# 每天奖池相同，正赛 3 局打平后加赛一局，假模型脚本设计的各个阶段都会出现。
OFFLINE_GAME = GameSettings(
    initial_coins=12, daily_salary=6, daily_pool=(3, 3, 3), max_bid=10,
    super_cost=3, super_bounty=5, super_hit_score=1,
    regular_matches=3, max_playoffs=5,
)


def configure_offline_demo(monkeypatch, config_files, teams_file, *, team=False, game=OFFLINE_GAME):
    """让 generate_demo 读合成配置和 game：个人赛 4 人改名甲乙丙丁，团队赛 6 人同 TEAM_OF。

    配置按测试模型读一次，之后固定不变，generate_demo 传的 test_model 只影响场次类型和快照。
    返回 (config, 玩家配置)；比赛开始前改动它们，这场比赛就用改过的值。
    """
    config, configs = load_configuration(*config_files, test_model=True, team=team, teams_path=teams_file)
    if not team:
        configs = [replace(pc, profile=replace(pc.profile, name=name), identity=replace(pc.identity, name=name))
                   for name, pc in zip("甲乙丙丁", configs * 2)]
    monkeypatch.setattr(runners, "load_configuration", lambda **_: deepcopy((config, configs)))
    monkeypatch.setattr(runners, "load_game_settings", lambda **_: game)
    return config, configs
