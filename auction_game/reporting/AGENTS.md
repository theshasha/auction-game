# 日志与统计

任务日志由 `auction_game/agent/log.py` 写，改之前读 `auction_game/agent/AGENTS.md`。统计口径见 `docs/analyze.md`。

## 原则

- 统计和展示只读本场日志和配置快照，不读当前配置。
- `analyze.py` 只读日志和计算，打印和命令行入口在 `auction_game/cli/analyze.py`。
