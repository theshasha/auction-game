# 日志与统计

任务日志由 `auction_game/agent/log.py` 写，改之前读 `auction_game/agent/AGENTS.md`。统计口径见 `docs/analyze.md`。

## 原则

- 统计和展示只读本场日志和配置快照，不读当前配置。花费的单价和倍率也取快照，`cost.py` 只接收用量、单价和倍率。
- 计算模块只读日志和计算，打印和命令行入口在 `auction_game/cli/` 的同名或对应命令里：`analyze.py` 对应 `cli/analyze.py`，`team_metrics.py` 对应 `cli/team_report.py`，`replay.py` 对应 `cli/replay.py`。
- 预估（`estimate.py`）是例外：开赛前用当前单价、倍率和 `logs/` 下的历史场次。当前配置由 `runners.estimate_lines` 读好传进来，计算仍只接收参数。开赛和 `cli/estimate.py` 都要打印，`runners` 不能导入 `cli`，所以打印行也在 `estimate.py` 里拼。
