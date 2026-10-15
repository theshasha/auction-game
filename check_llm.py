"""连通性冒烟测试：对 players.yaml 中每个玩家的 provider/model 各发一条最小请求。

用法：python3 check_llm.py
超时等运行设置读取 config.yaml。
会真实调用网络 API。全部通过后再运行 main.py --ai 正式对局。
"""

from __future__ import annotations

import sys
import time

from configuration import ConfigurationError, load_configuration
from llm_client import MissingApiKeyError, make_chat_fn

PING_MESSAGES = [
    {"role": "system", "content": "你是一个简洁的助手。"},
    {"role": "user", "content": "用一句话介绍你自己。"},
]


def main() -> int:
    try:
        config, players = load_configuration()
    except ConfigurationError as exc:
        print(f"[配置错误] {exc}")
        return 1
    timeout = config["llm"]["timeout_seconds"]

    failures = 0
    for player in players:
        protocol_suffix = f" / {player['protocol']}" if player.get("protocol") else ""
        label = f"{player['name']}（{player['provider']}{protocol_suffix} / {player['model']}）"
        try:
            chat_fn = make_chat_fn(
                provider=player["provider"],
                model=player["model"],
                timeout=timeout,
                protocol=player.get("protocol"),
            )
        except (MissingApiKeyError, ValueError) as exc:
            print(f"[配置错误] {label}: {exc}")
            failures += 1
            continue

        started = time.monotonic()
        try:
            reply = chat_fn(PING_MESSAGES)
        except Exception as exc:
            print(f"[调用失败] {label}: {type(exc).__name__}: {exc}")
            failures += 1
            continue
        elapsed = time.monotonic() - started
        preview = " ".join(reply.split())[:80]
        print(f"[通过] {label}  耗时 {elapsed:.1f}s  回复: {preview}")

    print(f"\n共 {len(players)} 个端点，失败 {failures} 个")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
