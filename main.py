"""命令行解析：用真实模型跑个人赛，--team 时跑团队赛，--test-model 时全员使用测试模型，--resume 接着跑没打完的一场。"""

from __future__ import annotations

import argparse
from pathlib import Path

from auction_game import runners


def main() -> int:
    parser = argparse.ArgumentParser(description="唯一高价拍卖游戏：使用真实模型比赛，可能收费")
    parser.add_argument("--team", action="store_true", help="团队赛，阵容读 configs/teams.yaml")
    parser.add_argument("--test-model", action="store_true", help="全员使用配置指定的测试模型，仍可能收费")
    parser.add_argument("--resume", nargs="?", const="", metavar="场次目录",
                        help="接着跑没打完的一场，仍可能收费；不写目录时取 logs/ 下最新一场没打完的")
    args = parser.parse_args()
    if args.resume is not None:
        if args.team or args.test_model:
            parser.error("--resume 按场次快照里的赛制和模型续跑，不能和 --team、--test-model 同时使用")
        return runners.resume_ai(Path(args.resume) if args.resume else None)
    return runners.run_ai(test_model=args.test_model, team=args.team)


if __name__ == "__main__":
    raise SystemExit(main())
