"""按本场参数和阵容渲染规则正文：个人赛用 RULES.template.md，团队赛用 RULES_TEAM.template.md。"""

from __future__ import annotations

from auction_game.game.lineup import Lineup
from auction_game.game.settings import DISCUSSION_ROUNDS, GameSettings
from auction_game.paths import TEAM_TEMPLATE_PATH, TEMPLATE_PATH

def rules_document(game: GameSettings, lineup: Lineup, *, root: bool) -> str:
    """规则文件的全文：一行生成说明加正文。root 为项目根目录那份，否则是场次目录里的本场规则。"""
    template = TEAM_TEMPLATE_PATH.name if lineup.teams else TEMPLATE_PATH.name
    if root:
        mode = "团队赛" if lineup.teams else "个人赛"
        note = (f"由 {template} 按 configs/ 里的生产参数和阵容生成，{mode}启动时自动同步；"
                "手动生成：python -m auction_game.cli.rules")
    else:
        note = f"由 {template} 按本场参数和阵容生成"
    return f"<!-- {note} -->\n" + render_rules(game, lineup)


def render_rules(game: GameSettings, lineup: Lineup) -> str:
    """按本场参数和阵容渲染规则正文，阵容有阵营时用团队赛模板；模型提示直接使用正文。"""
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
        template_path = TEAM_TEMPLATE_PATH
        values["team_lineup"] = "，".join(f"{team}有 {'、'.join(members)}" for team, members in teams.items())
        values["team_size"] = len(next(iter(teams.values())))
        values["discussion_rounds"] = DISCUSSION_ROUNDS
    else:
        template_path = TEMPLATE_PATH
    return template_path.read_text(encoding="utf-8").format_map(values).strip() + "\n"
