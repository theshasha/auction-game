# 模型边界

源码入口：`prompts.py` 管自然语言；`agent_tasks.py` 管工具定义及业务校验；`agent_execution.py` 管通用执行；`llm_client.py` 管协议与网络；`ai_player.py` 管历史提交；`ai_speech.py` 管阶段编排。

## 易损约束

- 严格校验与一次共享纠错预算保持启用；SDK 分别计数的错误也受总请求上限约束。网络重试独立受限，完整流明确成功后才交付，截断直接失败。
- 客户端随任务事件循环创建并关闭；业务状态在全部成功日志写入后才提交。程序错误与磁盘错误原样传播，避免误当网络故障重试。
- 独立任务回传提示与合法业务结果，原始 thinking 留审计；任务内部纠错由 SDK 管理。strategy 与 memory 私有，观众总结不回写玩家。
- 局末全员发言公布后才能生成记忆，再规划下一局；记忆篇幅只作软目标。保留首日缓存仅消费一次及最后一局跳过记忆的回归。
- 协议按显式配置选择。Hub 的 forced-tool-choice 兼容使用公开 profile，保留框架推断的 context 与加密推理 include；纯文本 JSON 仍走工具纠错。Chat 保留明确结束原因检查与 `max_tokens` 兼容设置。输出预算及推理策略见根 `docs/usage.md`，渠道拒绝或截断后保持原预算。

## 回归入口

`tests/ai/test_pydantic_ai_contract.py` 验证三协议真实 SDK 序列化、共享纠错与隐私；`test_llm_client.py` 验证流、重试与资源关闭；`test_ai.py`、`test_memory.py` 验证历史事务及记忆。改阶段编排复用 `tests/integration/` 完整假模型赛。全部使用 FunctionModel 或 MockTransport 与假密钥。
