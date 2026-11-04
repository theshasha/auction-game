"""生成或检查项目根目录的 RULES.md 和 RULES_TEAM.md；比赛入口在开赛前用 sync_root_rules 同步自己那份。"""

from __future__ import annotations

import argparse
import sys

from auction_game.configuration import ConfigurationError, load_game_settings, load_players, load_teams
from auction_game.game.lineup import Lineup
from auction_game.game.rules import RulesError, render_rules
from auction_game.game.settings import GameSettings
from auction_game.paths import RULES_PATH, TEAM_RULES_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只检查 RULES.md 和 RULES_TEAM.md 是否为最新生成结果")
    args = parser.parse_args()
    try:
        game = load_game_settings()
        documents = {path: render_rules(game, _identity_lineup(team=team))
                     for path, team in ((RULES_PATH, False), (TEAM_RULES_PATH, True))}
        if args.check:
            stale = [path.name for path, text in documents.items()
                     if not path.exists() or path.read_text(encoding="utf-8") != text]
            if stale:
                print(f"{'、'.join(stale)} 已过期，比赛启动将自动同步；也可执行 python -m auction_game.cli.rules。",
                      file=sys.stderr)
                return 1
            print("RULES.md 和 RULES_TEAM.md 与当前配置一致。")
        else:
            for path, text in documents.items():
                _write_if_changed(path, text)
                print(f"已生成 {path}")
    except (ConfigurationError, RulesError, OSError) as exc:
        print(f"规则生成失败：{exc}", file=sys.stderr)
        return 1
    return 0


def _identity_lineup(*, team: bool) -> Lineup:
    # 根规则用配置中的角色名；只需目录和名单，不读能力快照。
    return Lineup(tuple(profile for profile, _ in (load_teams() if team else load_players())))


def _write_if_changed(path, text):
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")


def sync_root_rules(game: GameSettings, *, team: bool = False) -> None:
    """比赛启动前同步根规则：个人赛是 RULES.md，团队赛是 RULES_TEAM.md；在创建场次和调用模型之前执行。"""
    _write_if_changed(TEAM_RULES_PATH if team else RULES_PATH, render_rules(game, _identity_lineup(team=team)))


if __name__ == "__main__":
    raise SystemExit(main())
