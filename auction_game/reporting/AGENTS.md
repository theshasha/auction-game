# 日志与回放

源码入口：`run_paths.py` 定义共同布局；`agent_log.py` 写任务审计；`tool_records.py` 写发送前工具定义；`request_records.py` 做可选完整捕获；`replay.py` 导出回放；`analyze.py` 做只读统计。

## 易损约束

- 历史读取以本场 `run_config.json` 为准，身份与实际执行模型分开；只支持当前格式，缺字段明确报错。历史保持原地，分析不使用当前配置补值。统计口径见根 `docs/analyze.md`。
- 所有任务日志写成功后，调用方才能提交内存状态；跨文件没有事务，失败可留下部分审计，异常必须传播。改失败审计时保留相邻请求合并与 interrupted 占位响应的回归。
- 默认工具审计仅取发送前实际存在的工具字段，保留缺失、null、false 的区别，失败请求也留痕；不保存密钥、鉴权头或完整 body。FunctionModel 定义须标注离线 SDK 生成，与真实发送来源分开。
- 可选完整捕获保留失败、纠错及回滚分叉，按实际边界无损重建并校验哈希。回放用本场快照、假密钥与 MockTransport；复用已存工具定义，缺定义明确要求离线补捕获，避免用当前 SDK 静默改写历史证据。

## 回归入口

`tests/ai/test_agent_log.py`、`test_tool_records.py`、`test_request_records.py` 验证审计及重建；`tests/app/test_run_paths.py`、`test_run_output.py`、`test_analyze.py` 验证场次产物及统计；完整回放复用 `tests/integration/test_offline_demo.py`。
