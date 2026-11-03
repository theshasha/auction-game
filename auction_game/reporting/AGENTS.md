# 日志与统计

## 按任务查代码

- 场次文件布局在 `run_paths.py`，场次创建、README 和终端战报在 `run_output.py`。
- 任务日志由 `auction_game/agent/log.py` 写，改之前读 `auction_game/agent/AGENTS.md`。
- `analyze.py` 只读日志计算统计，口径见根目录 `docs/analyze.md`。

## 原则

- 统计和展示只读本场日志和配置快照，不读当前配置。
