"""命令行解析与模式分派：默认 Bot 整场，--ai 交互对局，--benchmark 批量对照。"""

from __future__ import annotations

import argparse

from auction_game.game import benchmark
from auction_game import runners


def positive_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("场数必须是正整数") from None
    if count <= 0:
        raise argparse.ArgumentTypeError("场数必须是正整数")
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="唯一高价拍卖游戏")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（仅 Bot 模式），用于复现对局")
    parser.add_argument("--ai", action="store_true", help="使用真实模型比赛，可能收费")
    parser.add_argument("--test-model", action="store_true", help="配合 --ai：全员使用配置指定的测试模型，仍可能收费")
    parser.add_argument("--benchmark", type=positive_count, metavar="N", default=None,
                        help="策略对照：N 为正整数，跑 N 场纯随机 + N 场最高可付价策略对照，共 2N 场（不写日志）")
    args = parser.parse_args()
    if args.test_model and (not args.ai or args.benchmark is not None):
        parser.error("--test-model 需要 --ai，且不能用于 --benchmark")

    if args.benchmark is not None:
        return benchmark.run_benchmark(args.benchmark)
    elif args.ai:
        return runners.run_ai(test_model=True) if args.test_model else runners.run_ai()
    else:
        return runners.run_bots(args.seed)


if __name__ == "__main__":
    raise SystemExit(main())
