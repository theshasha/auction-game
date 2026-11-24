"""不开赛，按当前配置打印一场的预估花费；不调用模型，不写任何文件。

用法：
    python -m auction_game.cli.estimate [--team] [--test-model]
"""

from __future__ import annotations

import argparse

from auction_game.configuration import ConfigurationError, load_configuration, load_game_settings
from auction_game.runners import estimate_lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="按当前配置和 logs/ 下的历史场次预估一场的花费")
    parser.add_argument("--team", action="store_true", help="团队赛，阵容读 configs/teams.yaml")
    parser.add_argument("--test-model", action="store_true", help="全员用 config.yaml 的 test_model，按测试参数")
    args = parser.parse_args(argv)
    # 不走 prepare_start：它会改写根目录的规则文件。
    try:
        _, player_configs = load_configuration(test_model=args.test_model, team=args.team)
        game = load_game_settings(test_model=args.test_model)
    except ConfigurationError as exc:
        print(f"[配置错误] {exc}")
        return 1
    for line in estimate_lines(player_configs, game, team=args.team):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
