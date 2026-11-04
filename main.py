"""命令行解析：用真实模型跑个人赛，--team 时跑团队赛，--test-model 时全员使用测试模型。"""

from __future__ import annotations

import argparse

from auction_game import runners


def main() -> int:
    parser = argparse.ArgumentParser(description="唯一高价拍卖游戏：使用真实模型比赛，可能收费")
    parser.add_argument("--team", action="store_true", help="团队赛，阵容读 configs/teams.yaml")
    parser.add_argument("--test-model", action="store_true", help="全员使用配置指定的测试模型，仍可能收费")
    args = parser.parse_args()
    return runners.run_ai(test_model=args.test_model, team=args.team)


if __name__ == "__main__":
    raise SystemExit(main())
