# 日志与统计

## 按任务查代码

- 场次文件布局在 `run_paths.py`，场次创建、README 和终端战报在 `run_output.py`，任务日志在 `agent_log.py`。
- `analyze.py` 只读日志计算统计，口径见根目录 `docs/analyze.md`。

## 原则

- 统计和展示只读本场日志和配置快照，不读当前配置。
- 每个任务先写 JSONL，再写可读 Markdown。都写成功后调用方才更新状态，写入异常向上传递。
- 私人日志包含私有策略、记忆和 thinking，面向用户的文档要提醒分享前检查。
