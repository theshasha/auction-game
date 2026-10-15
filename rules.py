"""按本场参数渲染规则；命令行生成或检查项目根目录的 RULES.md。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from configuration import load_game_settings, load_players
from game_rules import GameSettings

ROOT_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = ROOT_DIR / "RULES.template.md"
RULES_PATH = ROOT_DIR / "RULES.md"
GENERATED_NOTE = "<!-- 由 RULES.template.md 与本场参数生成；根目录文档刷新：python rules.py -->\n"


def render_rules(game: GameSettings, player_names: list[str]) -> str:
    """使用实际参赛名单生成完整规则；模板缺值或格式错误直接抛出。"""
    if len(player_names) < 2:
        raise ValueError("参赛名单至少需要两名玩家")
    if any(not isinstance(name, str) or not name.strip() for name in player_names):
        raise ValueError("玩家名字必须是非空字符串")
    if len(set(player_names)) != len(player_names):
        raise ValueError("玩家名字不能重复")
    if len(set(game.daily_pool)) == 1:
        pool_description = (
            f"每天拍卖 {game.daily_pool[0]} 个胜场，每局共 {game.wins_per_match} 个胜场。"
        )
    else:
        daily_amounts = "、".join(
            f"第 {day} 天 {amount} 个胜场"
            for day, amount in enumerate(game.daily_pool, 1)
        )
        pool_description = (
            f"各日基础奖池依次为：{daily_amounts}；每局计划共 {game.wins_per_match} 个胜场。"
        )
    values = {
        **game.to_dict(),
        "max_days": game.max_days,
        "player_count": len(player_names),
        "player_names": "、".join(player_names),
        "pool_description": pool_description,
    }
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    return GENERATED_NOTE + template.format_map(values).strip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只检查 RULES.md 是否为最新生成结果")
    args = parser.parse_args()
    try:
        game = load_game_settings()
        names = [player["name"] for player in load_players()]
        text = render_rules(game, names)
        if args.check:
            if RULES_PATH.read_text(encoding="utf-8") != text:
                print("RULES.md 已过期，请执行 python rules.py 刷新。", file=sys.stderr)
                return 1
            print("RULES.md 与当前配置一致。")
        else:
            RULES_PATH.write_text(text, encoding="utf-8")
            print(f"已生成 {RULES_PATH}")
    except (OSError, UnicodeError, ValueError, KeyError, IndexError, TypeError) as exc:
        print(f"规则生成失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
