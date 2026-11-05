"""按本场参数和阵容渲染规则正文：个人赛用 RULES.template.md，团队赛用 RULES_TEAM.template.md。"""

from __future__ import annotations

from auction_game.game.lineup import Lineup
from auction_game.game.settings import DISCUSSION_ROUNDS, GameSettings
from auction_game.paths import TEAM_TEMPLATE_PATH, TEMPLATE_PATH

GENERATED_NOTE = "<!-- 由 RULES.template.md 与本场参数生成；比赛启动自动同步；手动生成：python -m auction_game.cli.rules -->\n"
TEAM_GENERATED_NOTE = ("<!-- 由 RULES_TEAM.template.md 与本场参数生成；团队赛启动自动同步；"
                       "手动生成：python -m auction_game.cli.rules -->\n")
# 两份模板里结算例子的出价都写死为 0~5。
EXAMPLE_MAX_BID = 5


class RulesError(ValueError):
    """本场参数和规则模板的例子对不上；入口按配置错误报告。"""


def render_rules(game: GameSettings, lineup: Lineup, *, include_note: bool = True) -> str:
    """按本场参数和阵容渲染规则，阵容有阵营时用团队赛模板；include_note=False 只取正文，供模型提示使用。"""
    if game.max_bid < EXAMPLE_MAX_BID:
        raise RulesError(
            f"max_bid 为 {game.max_bid}，小于规则例子用到的出价 {EXAMPLE_MAX_BID}；"
            "请调大 max_bid 或修改规则模板的例子"
        )
    if len(set(game.daily_pool)) == 1:
        pool_description = (
            f"每天拍卖 {game.daily_pool[0]} 分，每局共 {game.auction_pool_per_match} 分拍卖奖池。"
        )
    else:
        daily_amounts = "、".join(
            f"第 {day} 天 {amount} 分"
            for day, amount in enumerate(game.daily_pool, 1)
        )
        pool_description = (
            f"各日奖池依次为：{daily_amounts}。每局奖池合计 {game.auction_pool_per_match} 分。"
        )
    values = {
        **game.to_dict(),
        "max_days": game.max_days,
        "player_count": len(lineup.names),
        "player_names": "、".join(lineup.names),
        "pool_description": pool_description,
    }
    teams = lineup.teams
    if teams:
        template_path, note = TEAM_TEMPLATE_PATH, TEAM_GENERATED_NOTE
        values["team_lineup"] = "，".join(f"{team}有 {'、'.join(members)}" for team, members in teams.items())
        values["team_size"] = len(next(iter(teams.values())))
        values["discussion_rounds"] = DISCUSSION_ROUNDS
    else:
        template_path, note = TEMPLATE_PATH, GENERATED_NOTE
    body = template_path.read_text(encoding="utf-8").format_map(values).strip() + "\n"
    return note + body if include_note else body
