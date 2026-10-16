"""按本场参数渲染规则；命令行生成或检查项目根目录的 RULES.md。"""

from __future__ import annotations

import argparse
import sys
import os
import tempfile

from auction_game.configuration import ConfigurationError, load_game_settings, load_players
from auction_game.game.game_rules import GameSettings
from auction_game.paths import TEMPLATE_PATH, RULES_PATH
GENERATED_NOTE = "<!-- 由 RULES.template.md 与本场参数生成；比赛启动自动同步；手动预览：python -m auction_game.game.rules -->\n"


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
        names = _identity_names()
        text = render_rules(game, names)
        if args.check:
            if RULES_PATH.read_text(encoding="utf-8") != text:
                print("RULES.md 已过期，比赛启动将自动同步；也可执行 python -m auction_game.game.rules。", file=sys.stderr)
                return 1
            print("RULES.md 与当前配置一致。")
        else:
            _write_if_changed(text)
            print(f"已生成 {RULES_PATH}")
    except (OSError, UnicodeError, ValueError, KeyError, IndexError, TypeError) as exc:
        print(f"规则生成失败：{exc}", file=sys.stderr)
        return 1
    return 0


def _identity_names():
    # 根文档只需配置身份；Bot 与预览无需能力快照、连接设置或密钥。
    return [player["name"] for player in load_players(resolve_capabilities=False)]


def _write_if_changed(text):
    if RULES_PATH.exists() and RULES_PATH.read_text(encoding="utf-8") == text:
        return False
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=RULES_PATH.parent,
                                     prefix=RULES_PATH.name + ".", suffix=".tmp", delete=False) as file:
        file.write(text)
        temporary = file.name
    # 同文件系统原子替换；写入或替换失败向上传播，旧文档保持完整。
    os.replace(temporary, RULES_PATH)
    return True


def sync_root_rules(game=None):
    """公共启动屏障：模型与场次创建之前完成，配置身份不被 Bot 名单覆盖。"""
    try:
        return _write_if_changed(render_rules(game if game is not None else load_game_settings(), _identity_names()))
    except (OSError, UnicodeError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise ConfigurationError(f"根规则同步失败：{exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
