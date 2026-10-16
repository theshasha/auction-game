# 模型执行与审计约定

修改模型边界、任务、历史或协议兼容时阅读本文。用户命令与配置政策见 [使用说明](usage.md)，费用授权见根 `AGENTS.md`。依赖版本以 `requirements.txt` 为准；使用现有 Python，测试只用 FunctionModel 或 MockTransport 与假密钥。

## 执行边界

`auction_game/ai/agent_execution.py` 的 `AgentExecutor(factory)(agent, prompt, history, deps=...)` 使用公开 `Agent.run(event_stream_handler=...)` 消费所有模型节点，包括纠错。同步线程每任务一次 `asyncio.run()`；客户端在该事件循环内创建并关闭，工厂不持有跨循环 HTTP 客户端。执行器唯一一次深拷贝输入历史，不识别拍卖任务。

`agent_tasks.TaskRunner` 在业务层定义严格输出与 `AuctionDeps(game, coins)`：bid/plan 返回 strategy、bid、必填可空 super_bid；speech 返回私有 strategy 与公开 speech；summary 返回非空文本。`strict=True, extra='forbid'`，拒绝字符串数字、布尔出价与空白策略。发言只由提示限长，不硬截断，内部 thinking 不代替业务策略。提示词集中在 `prompts.py`，规则模板在根目录。

格式与规则校验共用 `Agent(retries=1)` 的一次纠错。`game_rules.validate_submission` 使用实时余额；只采用最终合法输出。输出耗尽抛 `OutputFailedError`；预期模型/传输故障抛 `CallFailedError` 交控制器处理；程序错误原样抛出。连通工具使用独立最小 Agent 与通用执行器，不依赖比赛参数。

## 历史、隐私与暂停

AIPlayer 持有唯一 `list[ModelMessage]`，初始化放入一次 system。执行成功且日志全部写入成功后追加 `new_messages()`，保留工具调用及配对工具应答。成功规划更新首日缓存；首日消费一次且不请求模型。失败不提交历史或缓存。

检查点保存消息长度与缓存；控制器只原样保存和交还不透明快照，支持 `None`。`RoundTask.participant` 显式接入检查点参与者。暂停停止新派发，等待在途任务结束并读取所有 future，业务校验成功的结果、历史与首日缓存均保留；恢复仅补跑失败或未启动项。全部成功也要在整轮提交前等待确认。快速回车/c 记录恢复意图，先收集再继续；失败重试另等确认。q 排空在途后回滚整轮并退出；程序异常原样传播。两种暂停竞态测试必须保留。

`llm.max_concurrency` 统一解析为正整数，默认 10，拒绝布尔值；1 为串行。生产 AI 与完整离线赛的所有轮级任务共用控制器上限，一次启动读取一次，启动显示并写入本场快照。派发与暂停使用同一同步边界，最多提交上限个任务，每轮输入仍由调用方统一固定。暂停等待期间，任务内部已有的有限网络重试及一次输出纠错可能继续，不增加预算。

失败后的等待闸门必须在公布可操作提示前建立：执行期间旧回车不预放行，失败公告后收到的确认不能再次清空。连续失败每次重新等待确认，离线无人交互入口遇到任务失败则直接退出。

对手只看到 speech，strategy 留在自身历史与私人日志。观众总结读取完整历史快照与原总结提示，回复只给观众和日志，不回写玩家历史。最后一局直接最终感言，跳过普通局末发言。

## 完整流与重试预算

模型包装器通过公开 `WrapperModel.request_stream()` 缓冲 SDK 完整流，确认成功状态后交付 `CompletedStreamedResponse`。SDK `max_retries=0`；每个请求只允许一次基础设施重发：网络/超时/读取故障、408、429、5xx；400/401/403、配置错误与截断立即失败。按异常类型和明确状态码判断，普通 ModelAPIError 不一概重发。

输出纠错最多两次请求，每次最多两次 HTTP 尝试，混合故障最多四次。失败流的部分响应不进入规范历史；网络重发只重发当前请求，不重做已经完成的纠错节点。request/attempt 按任务内编号，客户端按任务新建。

成功 finish_reason 仅接受 stop/tool_call，length 或缺原因直接失败，即使已有合法工具 JSON。Anthropic 的公开规范响应在已有 message_delta.stop_reason 后无法单独识别缺失的原始 message_stop；目前以明确成功原因为边界。单次响应缓冲期间不向 UI 实时展示 token，底层仍为流式。若要求原始终止帧完整，需另议 SSE 传输检查。

## 协议兼容

ModelFactory 按显式 protocol 选择 Chat、Responses 或 Anthropic，不猜品牌。Chat profile 保持 `openai_chat_streaming_requires_finish_reason=True` 与 `openai_chat_supports_max_completion_tokens=False`，实际使用 max_tokens；Responses 使用 max_output_tokens；Anthropic 使用 max_tokens。超时进入 HTTP read timeout。输出预算、64000 回退和 high 的单一说明在使用文档。

渠道 `chat_profile.supports_forced_tool_choice` 与 `responses_profile.supports_forced_tool_choice` 只接受布尔，分别作用于 Chat 与 Responses。当前 Hub 均为 false，使 SDK 发送 auto；未设置的渠道保留默认。覆盖保留框架推断的 context 与加密推理 include，推理档位与预算不变。普通 model_settings.tool_choice 会被框架输出要求覆盖，使用公开 profile 才能正确兼容。工具与严格校验仍启用，纯文本合法 JSON 出价仍走共享一次纠错并在耗尽后失败。

公开能力与渠道 profile 分开保存；本场 `data/run_config.json` 记录生效 profile。历史分析和回放只读本场快照，不依赖今天的配置。

## 磁盘审计

`RunPaths` 是日志、统计和回放的共同布局。format_version、run_type、身份编号合入 run_config，不另建 run.json。旧场次保持原地，新读取器只支持当前格式。玩家 JSONL 存本次新增规范消息、结果与错误；失败审计按 run_id 选取，排除 interrupted 占位响应，避免相邻 request 合并导致漏记。

写入顺序为任务 JSONL、可选捕获、显式私人 Markdown；任一失败向上传播，成功日志完成后才提交内存状态。没有跨文件事务：后续写入失败会留下部分审计，但历史与首日缓存不提交。

请求捕获默认关闭。开启时 RequestCapture 记录真实逻辑边界 messages/settings/parameters/response，包括失败、纠错、回滚后的分叉和总结；不猜测成功历史。RequestWriter 将 schema/settings 按内容哈希存一次，历史用前次相同消息前缀加变化尾部，尾部和响应优先引用任务审计并保留元数据差异。重建严格比较原值和哈希，保留 null、false 与缺失字段；错误从任务审计索引引用。正常模型输入不改，完整展开只按需导出 debug。

回放 SDK 定义通过本场快照构建真实 ModelFactory，假密钥与 MockTransport 拦截所有请求，以模拟 400 结束。只保存 tools/tool_choice/parallel_tool_calls（实际存在的字段）及 SDK 版本，无完整 body、鉴权头或重复摘录。重导出复用已有定义；缺定义明确要求离线补捕获，不静默使用新 SDK 或联网。FunctionModel 逻辑记录没有真实 HTTP 报文，回放明确标注模拟性质。

## 回归入口

`tests/ai/` 覆盖真实 SDK 线协议、共享纠错、重试预算、严格类型、历史与隐私。`tests/app/` 覆盖控制器、启动同步、路径和统计；`tests/integration/` 复用完整假模型赛验证所有产物、首日缓存、回放与资源关闭。公共辅助在 `tests/support/`，夹具留 `tests/conftest.py`，测试文件不互相导入。

Hub Responses 兼容的历史事实：已授权真实对照曾得到 required=400、auto=成功，公开 profile 的生产 Agent 保留 high/context/include 后成功。该结论只覆盖当时探测；后续收费比赛仍按用户确认流程执行。旧验证材料保留在原归档，不在本文重复测试计数或开发步骤流水。
