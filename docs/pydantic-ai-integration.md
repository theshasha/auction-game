# Pydantic AI 接入记录

第三步已将个人赛模型任务迁移到 `pydantic-ai-slim[openai,anthropic]==2.56.0` 的 Agent 层。当前支持 OpenAI Chat Completions、OpenAI Responses、Anthropic Messages；真实服务验证仍须单独授权。

## 环境与离线复现

使用现有 `C:\Python313\python.exe`（Python 3.13.7），没有项目虚拟环境。直接依赖版本以 `requirements.txt` 为准。本次环境为 Pydantic AI / pydantic-graph 2.56.0、OpenAI 3.31.0、Anthropic 1.15.0、httpx2 / httpcore2 2.13.1、Pydantic 2.12.5、pytest 9.1.1。

```bash
python -m pytest tests/ -q
python rules.py --check
python -m pip check
git diff --check
```

模型测试只使用 `FunctionModel` 或 `httpx2.MockTransport`，注入假密钥，不读取真实 `.env`。三协议 SDK 的实际流式行为在 `tests/test_pydantic_ai_contract.py`、`tests/test_llm_client.py` 回归；玩家事务及调度在 `tests/test_ai.py`、`tests/test_ai_speech.py`、`tests/test_controller.py` 回归。模拟发生在模型边界，schema 与规则纠错经过生产 Agent。

第一步曾创建临时隔离环境，随后按授权使用 trash 移入回收站；后续验收全部使用现有解释器。第一步全量 384 项通过；第二步全量 436 项通过。第三步测试精简后全量 368 项通过（清理前收集 428 项；测试文件共减少 119 行）。任务链集中验证真实玩家、生产 TaskRunner 及三协议 SDK，检查工具次序及唯一 ID 的调用应答配对；`rules.py --check`、`pip check`、`git diff --check` 均通过，规则正文无需刷新。

## 任务入口与职责

`agent_tasks.TaskRunner(factory)(task, prompt, history, game=..., coins=...)` 接收规范 `list[ModelMessage]`，返回合法 `output`、完整新增 `messages` 及请求错误元数据。同步线程边界每任务执行一次 `asyncio.run()`，客户端在该循环内创建并关闭；工厂只保存配置，不跨循环复用 AsyncClient。

通用边界为 `agent_execution.AgentExecutor(factory)(agent, prompt, history, deps=...)`，接收已定义的 `Agent[DepsT, OutputT]`，返回 `ExecutionResult[OutputT]`。执行器不识别拍卖任务，不导入比赛参数；输出类型与 validator 留在业务层，个人赛通过 `AuctionDeps(game, coins)` 传入比赛参数及余额。`CallFailedError` / `OutputFailedError` 在执行模块定义，调用方直接从这里导入。仍使用公开 `Agent.run()`，没有另写 Agent 循环。

| task | 输出 | 用途 |
| --- | --- | --- |
| `bid` | `BidOutput(strategy, bid, super_bid)` | 日常出价，按实时余额校验 |
| `plan` | `PlanOutput(strategy, bid, super_bid)` | 局前规划及首日缓存提交 |
| `speech` | `SpeechOutput(strategy, speech)` | 开局、局末及最终公开发言 |
| `summary` | 非空白文本 | 观众总结 |

结构化类型使用 `ConfigDict(strict=True, extra='forbid')`。`super_bid` 必填可空，字符串数字及布尔出价均拒绝；私有策略及公开正文拒绝空白。发言长度仍只由提示约束，不硬截断。策略是业务输出，内部 thinking 不作为替代内容。

出价与规划的 `output_validator` 调用 `game_rules.validate_submission`。格式与规则共用 `Agent(retries=1)` 的一次输出纠错，允许重新生成完整结构；只采用最终合法结果。空完整回复同样走输出纠错。错误耗尽为 `OutputFailedError`，其他预期服务失败为 `CallFailedError`，由 controller 收集；程序错误继续抛出。旧 `chat_fn`、裸 JSON 正则、发言定界符及手写纠错模板已移除。

`runners.py` 启动时构造 `TaskRunner`，缺密钥仍在创建比赛日志前报错。`check_llm.py` 定义自己的最小文本 Agent，通过通用执行器探测，仅依赖运行及玩家配置，不导入拍卖任务或加载 `game.yaml`。`ai_speech.py` 规划任务只等待完成，`plan_strategy` 返回 `None`；外部 `decide_bid` 保持二元组，`give_speech` 只返回公开字符串。

## 规范历史及事务

AIPlayer 只持有一份 `list[ModelMessage]`。system 规则及身份在初始化时进入历史；玩家及业务适配器传原历史，执行器在执行边界唯一一次深拷贝。调用方与测试替身遵守不修改输入历史的约定。Agent 成功且日志写入成功后一次追加 `new_messages()`。输出工具调用及对应的工具应答完整保留，下一任务切换 schema 后仍回传原工具记录。现有历史前缀保持不变。

成功规划替换首日缓存，首日消费一次且不再请求模型；规划失败保留调用前历史及缓存。轮级检查点仍同时保存消息长度与缓存，暂停等待在途任务结束，保留已收集结果，只补跑未收集部分。`RoundTask(key, fn, participant=None)` 显式接入 `CheckpointParticipant` 的 `checkpoint()` / `rollback(snapshot)`；AI 任务传玩家，无状态任务不传。控制器只保存并原样交还不透明快照（包括 `None`），不依赖 AIPlayer 类型，原并发与暂停算法保留。

公开面只接收 `speech`；私有 strategy 保留在玩家自身工具消息及日志，不进入对手的发言提示。观众总结读取**玩家完整历史快照及原总结提示**，总结请求和结果只落日志，不写回玩家历史。“独立上下文”指状态隔离，保留原有可见信息。第一步空历史总结原型已改为生产行为回归。

## 完整流与重试预算

使用公开 `Agent.run(event_stream_handler=...)`，handler 消费全部事件，覆盖每个模型节点及输出纠错。模型包装层通过公开 `WrapperModel.request_stream()` 完整缓冲 SDK 流，成功检查后交付 `CompletedStreamedResponse`。不会在工具 JSON 尚未读完流时提前提交，也不接管库的私有节点流程。

- SDK 显式 `max_retries=0`；AIPlayer 没有整任务自动重试。
- 每个模型请求最多两次基础设施尝试：连接故障、读取中断、408、429、5xx 允许一次重发。按 `ModelHTTPError` 状态码及 SDK/HTTP 异常类型判断，库包装的 `ModelAPIError` 追溯其明确异常原因；普通 `ModelAPIError` 不一律重试。
- 400/401/403、配置错误及明确截断立即失败。失败流部分内容留在模型边界内，重发只针对当前请求，已完成的纠错节点不重做。
- 格式及规则共享一次纠错，两次输出请求各有一次基础设施重试，混合故障最多四次 HTTP 尝试。基础设施重试不重置纠错预算。

`request_errors` 每项的 `request` 为任务内请求序号，从 1 开始；`attempt` 为该请求内部尝试序号。首次生成与纠错请求各自的尝试 1 可明确区分；下一任务重新编号。编号由每次 `ModelFactory.open()` 新建的包装器维护，不使用全局计数，不改变预算。

仅接受 `finish_reason` 为 `stop` / `tool_call`。完整 JSON 也可能随 `length` 返回并被原生 Agent 接受，包装层因此必须读完并检查状态。缺结束原因及 `length` 均失败。为隔离失败流，当前缓冲单次完整响应，暂不向界面实时显示 token；底层仍使用 stream，controller 仍只等待任务结果。

三协议未提供统一的原始 terminal-event 标记：Anthropic 已返回 `message_delta.stop_reason` 后，缺最后 `message_stop` 从公开规范响应无法单独识别。当前以明确成功结束原因为边界；未来若要求每个原始终止帧完整，须另行讨论传输层 SSE 检查。

## 渠道与协议

`configuration.py` 校验 `llm.channels` 地址及 `api_key_env`，校验玩家渠道引用、显式协议、正整数预算及超时。地址唯一来自配置，不自动补裁路径。OpenAI 地址包含 `/v1`；Anthropic 地址使用服务根路径，SDK 追加 `/v1/messages`。

`ModelFactory.open()` 按 protocol 创建 `OpenAIChatModel`、`OpenAIResponsesModel` 或 `AnthropicModel`。模型名原样发送，不根据品牌选择协议；中转 Gemini 别名保持支持。Chat profile 显式设置 `openai_chat_streaming_requires_finish_reason=True`、`openai_chat_supports_max_completion_tokens=False`，保留原线上 `max_tokens`。Responses 使用 `max_output_tokens`，Anthropic 使用 `max_tokens`。超时实际进入 HTTP read timeout。

现有六名玩家的实际路由及预算未改变。Kimi 使用 `hub_anthropic`，其余使用 `hub`，共用 `HUB_API_KEY`；Modal 与 Gemini 官方渠道已移除。成功及失败路径均关闭流与 HTTP 客户端；线程间模型上下文与历史隔离。

## 日志与验收边界

玩家 JSONL 每任务保存 `phase/task/context/status`、通过 `ModelMessagesTypeAdapter` 序列化的新增消息、任务结果及 `request_errors`。失败附错误类型与消息；完整已接收回复及纠错消息仅作审计，不提交玩家历史，失败流的半截响应不作为规范消息。捕获失败审计按本次 `run_id` 选择，避免库合并相邻 request 后按旧历史长度切片造成漏记。`agent_log.AgentLog.success()` / `.failure()` 负责序列化与同步写入，玩家只提交任务结果或异常；`render_messages()` 通用投影保留工具 ID 及纠错关联，拍卖结果展示由业务层传入。只支持新日志，无旧格式迁移。

写入顺序仍为 JSONL 后 Markdown，任一失败均向上传播；成功任务须日志成功后才提交历史及首日缓存。已知限制：JSONL 成功而 Markdown 失败会留下不完整的双文件审计，当前没有跨文件事务或 Markdown 降级。

`match_*.json` 比赛日志结构保持原样，`run_output.py`、`analyze.py` 继续使用原结构，无需适配。第二步 `python main.py --seed 42` 已完成五局，冠军 Bravo（8 胜）；`logs/latest` 已按用户授权使用 trash 清理，其他日志已归档到 `logs/archive_20261016`。symlink 创建代码未修改。

第三步覆盖规划→首日缓存→日常出价→公开发言→观众总结的任务串联、余额与严格类型校验、共享纠错、三协议重试及流失败隔离、暂停回滚、隐私与资源关闭。完整 AI 个人赛离线验收仍属第四步；真实中转的工具定义、强制工具选择及参数兼容性仍属第五步，须另行授权，不自动换模型。

第三步补充后的离线回归为 374 项：将旧玩家侧快照测试改为非拍卖 Agent 的类型化 deps、历史隔离及失败审计回归；新增非 AIPlayer 参与者的真实暂停回滚和日志写入失败边界。请求序号断言复用原混合重试及跨任务测试，严格校验与两种暂停竞态沿用现有回归，未扩展业务三协议矩阵。
