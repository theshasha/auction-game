"""生成一场打完的比赛的对局回放页，写到场次目录下的 replay.html，已有时覆盖。

用法：
    python -m auction_game.cli.replay
    python -m auction_game.cli.replay logs/<目录名>

不传目录时选最新的打完了的真实场次，离线赛要显式指定。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from auction_game.reporting.replay import build_replay, render_replay
from auction_game.reporting.run_output import find_finished_run


def write_replay(run_dir: Path) -> None:
    page = render_replay(build_replay(run_dir))
    output = run_dir / "replay.html"
    output.write_text(page, encoding="utf-8")
    print(f"对局回放已写入 {output}（{len(page.encode('utf-8')) // 1024} KB）")


def main() -> None:
    parser = argparse.ArgumentParser(description="生成对局回放页")
    parser.add_argument("run_dir", nargs="?", type=Path, help="场次目录；不传时选最新的打完了的真实场次")
    args = parser.parse_args()
    run_dir = args.run_dir or find_finished_run()
    if run_dir is None:
        sys.exit("logs/ 下没有打完的真实 AI 场次；离线模拟赛请显式指定目录")
    try:
        write_replay(run_dir)
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
