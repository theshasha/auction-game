"""生成或检查项目根目录的 RULES.md 和 RULES_TEAM.md；比赛启动时也会同步自己那份。"""

from __future__ import annotations

import argparse
import sys

from auction_game.configuration import ConfigurationError, load_game_settings
from auction_game.runners import root_rules


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只检查 RULES.md 和 RULES_TEAM.md 是否为最新生成结果")
    args = parser.parse_args()
    try:
        game = load_game_settings()
        documents = [root_rules(game, team=team) for team in (False, True)]
        stale = [(path, text) for path, text in documents
                 if not path.exists() or path.read_text(encoding="utf-8") != text]
        if args.check:
            if stale:
                print(f"{'、'.join(path.name for path, _ in stale)} 已过期，比赛启动将自动同步；"
                      "也可执行 python -m auction_game.cli.rules。", file=sys.stderr)
                return 1
            print("RULES.md 和 RULES_TEAM.md 与当前配置一致。")
        else:
            for path, text in stale:
                path.write_text(text, encoding="utf-8")
            for path, _ in documents:
                print(f"已生成 {path}")
    except (ConfigurationError, OSError) as exc:
        print(f"规则生成失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
