"""按本场参数渲染规则；命令行生成或检查项目根目录的 RULES.md。"""

from __future__ import annotations

import argparse
import sys

from auction_game.configuration import ConfigurationError, load_game_settings, load_players
from auction_game.game.settings import GameSettings
from auction_game.paths import TEMPLATE_PATH, RULES_PATH
GENERATED_NOTE = "<!-- 由 RULES.template.md 与本场参数生成；比赛启动自动同步；手动生成：python -m auction_game.game.rules -->\n"
# 模板里结算例子的出价写死为 0~5。
EXAMPLE_MAX_BID = 5


def render_rules(game: GameSettings, names: list[str], *, include_note: bool = True) -> str:
    """按本场参数和名单渲染规则；include_note=False 只取正文，供模型提示使用。"""
    if game.max_bid < EXAMPLE_MAX_BID:
        raise ConfigurationError(
            f"max_bid 为 {game.max_bid}，小于规则例子用到的出价 {EXAMPLE_MAX_BID}；"
            "请调大 max_bid 或修改 RULES.template.md 的例子"
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
        "player_count": len(names),
        "player_names": "、".join(names),
        "pool_description": pool_description,
    }
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    body = template.format_map(values).strip() + "\n"
    return GENERATED_NOTE + body if include_note else body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只检查 RULES.md 是否为最新生成结果")
    args = parser.parse_args()
    try:
        text = render_rules(load_game_settings(), _identity_names())
        if args.check:
            if RULES_PATH.read_text(encoding="utf-8") != text:
                print("RULES.md 已过期，比赛启动将自动同步；也可执行 python -m auction_game.game.rules。", file=sys.stderr)
                return 1
            print("RULES.md 与当前配置一致。")
        else:
            _write_if_changed(text)
            print(f"已生成 {RULES_PATH}")
    except (ConfigurationError, OSError) as exc:
        print(f"规则生成失败：{exc}", file=sys.stderr)
        return 1
    return 0


def _identity_names():
    # 根规则用配置中的角色名；只需目录和名单，不读能力快照。
    return [player["name"] for player in load_players(resolve_capabilities=False)]


def _write_if_changed(text):
    if not RULES_PATH.exists() or RULES_PATH.read_text(encoding="utf-8") != text:
        RULES_PATH.write_text(text, encoding="utf-8")


def sync_root_rules(game: GameSettings) -> None:
    """比赛启动前同步根 RULES.md，在创建场次和调用模型之前执行。"""
    _write_if_changed(render_rules(game, _identity_names()))


if __name__ == "__main__":
    raise SystemExit(main())
