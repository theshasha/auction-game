"""生成或检查项目根目录的 RULES.md；比赛入口在开赛前用 sync_root_rules 同步它。"""

from __future__ import annotations

import argparse
import sys

from auction_game.configuration import ConfigurationError, load_game_settings, load_players
from auction_game.game.rules import RulesError, render_rules
from auction_game.game.settings import GameSettings
from auction_game.paths import RULES_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只检查 RULES.md 是否为最新生成结果")
    args = parser.parse_args()
    try:
        text = render_rules(load_game_settings(), _identity_names())
        if args.check:
            if RULES_PATH.read_text(encoding="utf-8") != text:
                print("RULES.md 已过期，比赛启动将自动同步；也可执行 python -m auction_game.cli.rules。", file=sys.stderr)
                return 1
            print("RULES.md 与当前配置一致。")
        else:
            _write_if_changed(text)
            print(f"已生成 {RULES_PATH}")
    except (ConfigurationError, RulesError, OSError) as exc:
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
