# Pydantic AI 接入记录

第二步已将生产入口接到 `pydantic-ai-slim[openai,anthropic]==2.56.0` 的模型层。AIPlayer 暂保留文本任务接口，第三步再迁移结构化 Agent。以下第一步记录保留为技术验证依据，当前实现见末节。

## 环境与复现

项目使用 Python 3.13.7，解释器为 `C:\Python313\python.exe`，没有项目虚拟环境。依赖直接安装到该解释器；第二步已在 `requirements.txt` 固定 Pydantic AI 及直接使用的 SDK 版本。

```bash
python -m pip install 'pydantic-ai-slim[openai,anthropic]==2.56.0'
python -m pytest tests/test_pydantic_ai_contract.py -q
python -m pytest tests/ -q
python rules.py --check
python -m pip check
```

本次实际版本：Pydantic AI / pydantic-graph 2.56.0，OpenAI 3.31.0，Anthropic 1.15.0，httpx2 / httpcore2 2.13.1，Pydantic 2.12.5，pytest 9.1.1。安装升级 OpenAI 2.24.0 → 3.31.0、jiter 0.13.0 → 0.17.0、idna 3.11 → 3.20；新增上述候选库及 genai-prices 0.1.13、griffelib 2.3.2、opentelemetry-api 1.45.1、logfire-api 5.2.0、truststore 0.10.5。原 httpx 0.28.1 与 Pydantic 保持原版本，`pip check` 通过。

授权更新前曾建立 `C:\Users\liu\AppData\Local\Temp\opencode\auction-pydantic-ai-step1`，已按用户授权使用 trash 移入回收站；验收结果取项目现有解释器。所有模型 HTTP 均由 `httpx2.MockTransport` 接管，仅用假密钥；未读取 `.env`，未调用真实服务。第二步起这些依赖为必需，契约测试不再跳过缺失依赖。

## 最小接入契约

以任务调用替换 `chat_fn(messages) -> str`：输入为任务类型、本次提示、规范历史、`GameSettings` 及当前余额；返回严格业务结果和 `new_messages()`。出价保留私有 `strategy`，规划在同一结果中返回策略及首日出价，发言分开返回私有策略与公开正文。简单复盘允许文本输出，终局观众总结使用独立上下文。

AIPlayer 持有唯一 `list[ModelMessage]`。传给 Agent 的历史使用独立快照；成功后一次性追加新消息并提交首日缓存，失败不改原状态。规范消息通过 `ModelMessagesTypeAdapter` 转日志，展示从同一数据投影，保留工具调用 ID 及其应答；不维护第二套 dict 对话状态。规划 → 发言切换输出工具名称和 schema 后，三协议仍能回传旧工具记录，system 与当前 user 各出现一次。

`ConfigDict(strict=True, extra='forbid')` 负责类型，`output_validator` 调用现有 `validate_submission`，非法业务结果抛 `ModelRetry`。`Agent(retries=1)` 让格式与规则共用一次纠错。策略是业务正文，不从内部 thinking 提取；空白公开正文需要拒绝。

## 流式入口与重试归属

同步线程边界采用每任务 `asyncio.run()`，在该循环内创建 SDK 客户端，并在 `finally`/异步上下文退出时关闭。现有 controller 的线程池及暂停等待机制可以保留；禁止跨这些循环复用 AsyncClient。已验证四线程并发、同玩家连续两次新循环及独立历史。现有 AIPlayer/controller 回归继续覆盖检查点、缓存回滚、暂停后补跑；候选链路验证成功提交新消息及失败无状态变动。

采用 `Agent.run(event_stream_handler=...)` 驱动每个模型节点的流式请求及输出纠错，handler 消费全部事件。`run_sync()` 不用作流式保证；`run_stream()` 的最终输出捷径也不作为本次接入口。

第一步候选验证通过公开 `WrapperModel.request_stream()` 实现每请求重试：完整消费内部 SDK 流，得到 `stream.get()`，检查结束状态后返回公开 `CompletedStreamedResponse`。失败流的部分输出始终留在模型边界内。候选每个模型请求最多两次网络尝试，SDK 显式 `max_retries=0`；第三步接入时再移除 AIPlayer 整任务重试。模拟验证覆盖 1 次正常请求、2 次纠错请求、网络重试叠加后 3 次请求，以及初次/纠错各发生网络故障的 4 次请求。HTTP 400 不走候选网络重试。第二步生产层只提取完整流检查，候选重试暂留测试中。

第一步候选网络预算范围为连接失败及流读取中断。第三步重试分类约定见末节。完整 JSON 但输出上限截断按任务失败处理，不静默提交或从部分输出恢复。

## 协议行为与限制

- 协议由显式模型类选择：`OpenAIChatModel`、`OpenAIResponsesModel`、`AnthropicModel`；自定义 Gemini 中转别名保持原样，不据模型品牌切换协议。OpenAI base_url 包含 `/v1`；Anthropic SDK 本身追加 `/v1/messages`，其 base_url 使用服务根地址，配置接入时需要分别规范化。
- `timeout=3` 实际进入 HTTP read timeout；`max_tokens=123` 分别发送为 Chat 的 `max_completion_tokens`、Responses 的 `max_output_tokens`、Anthropic 的 `max_tokens`。未知别名使用 provider 默认 profile；Chat 显式设置公开 `openai_chat_streaming_requires_finish_reason=True`，否则库会为缺结束状态的工具输出推断成功。
- 三协议均存在“工具 JSON 完整，即使 finish reason 是 length 也能被 Agent 接受”的行为，测试已固定该事实。因此模型边界必须读完流再检查 `finish_reason`，仅允许 `stop/tool_call`。缺少结束状态或 `length` 一律失败；连接/ReadError 的重试丢弃部分调用。库未提供三个协议统一的原始 terminal-event 标记：Anthropic 的 `message_delta.stop_reason` 已给出结果时，缺最后 `message_stop` 无法从公开规范响应单独识别。本步按明确成功 finish reason 验收；若未来要求每一个原始终止帧完整，需另行增加传输层 SSE 检查。
- 为保证失败流隔离，模型边界缓冲单次完整响应，暂不对界面实时输出 token；底层请求仍真实使用 stream。当前 controller 只等任务结果，该策略不改变它的行为。流与客户端在成功和异常路径均关闭。

本步未验证真实中转对工具定义、强制工具选择、参数名及任意模型的支持，也未实施正式 AIPlayer 迁移或完整候选 AI 个人赛；这些仍属于后续步骤。业务必需的网络语义通过公开接口已实现，无需当前用户改变约束。

## 验收及第二步入口

基线 325 项测试通过，规则检查通过。新增契约测试 59 项通过；候选加现有 AIPlayer/controller 回归共 99 项通过。最终全量 384 项通过，`rules.py --check`、`pip check` 及 `git diff --check` 均通过。测试集中在 `tests/test_pydantic_ai_contract.py`，没有新比赛控制器，也没有生产 Agent 框架。

第二步已完成配置与生产模型层接入。第三步再修改 `ai_player.py`、`prompts.py`、`ai_speech.py`，将结构化任务、消息历史及缓存提交边界落地；须单独授权。

## 第二步：当前生产接口

`configuration.py` 校验 `llm.channels` 中的 HTTP(S) 地址及 `api_key_env`，同时校验玩家的渠道引用、显式协议、正整数 `max_tokens` 与超时。没有旧配置兼容层。`load_players()` 仍可独立供规则生成使用；`check_llm.py` 继续只加载运行与玩家配置。缺密钥在创建比赛日志之前报错。

渠道地址由配置唯一决定，不自动补裁路径：OpenAI 渠道使用含 `/v1` 的地址；Anthropic 渠道使用当前 `/anthropic` 根地址，SDK 追加 `/v1/messages`。原六名玩家的模型名、预算及实际协议保持原值，Kimi 引用 `hub_anthropic`，其余引用 `hub`；两个渠道共用 `HUB_API_KEY`。Modal 与 Gemini 官方接入已移除，中转 Gemini 保留。

`ModelFactory.open()` 按协议选择 `OpenAIChatModel`、`OpenAIResponsesModel` 或 `AnthropicModel`，返回生产 `BufferedRequest`。不根据品牌选协议。工厂仅保存配置，客户端在每次调用的循环内创建并关闭；成功或异常均关闭流与 HTTP 客户端。`settings` 提供超时及预算，供后续 Agent 复用。

Chat 显式 profile 为 `openai_chat_streaming_requires_finish_reason=True`、`openai_chat_supports_max_completion_tokens=False`：保留原线上 `max_tokens` 字段。Responses 使用 `max_output_tokens`，Anthropic 使用 `max_tokens`。第一步默认 profile 映射 `max_completion_tokens` 的记录属于候选结果，第二步已通过真实 SDK 模拟请求验证上述兼容配置。

`make_chat_fn()` 经同一工厂创建薄文本适配器，将现有 dict 历史逐条转换，使用公开 `Model.request_stream()`，只取 `TextPart` 正文并沿用 think 清理。它不持有历史、不创建文本 Agent，也不追加重复提示。同步边界每调用执行 `asyncio.run()`；AIPlayer 的首日缓存、解析纠错及 controller 暂停回滚保持原实现。

### 过渡期重试与后续交接

第二步生产模型层每次只发一个请求，SDK `max_retries=0`。AIPlayer 继续捕获全部异常并最多重试一次，因此 HTTP 400 也可能共请求两次；这是保留的过渡行为。格式与规则纠错也仍归 AIPlayer。`check_llm` 无额外重试。

第三步须一次性交接：移除 AIPlayer 整任务失败重试；模型包装层只重试当前请求，基础设施故障至多重试一次；Agent 独占一次输出纠错。重试候选为连接/读取故障、408、429 与 5xx；400/401/403、配置错误、结构化结果截断直接失败。实现时核对实际 SDK 异常及状态码，避免将全部 `ModelAPIError` 都当作网络故障。测试中的 `CandidateRetry` 仅保留第一步可行性契约，尚未启用该生产策略。

### 验证边界

协议契约测试已改用生产工厂与完整流检查；未来结构化 Agent 的契约继续保留。新增文本 SSE 测试验证三协议正文、历史不重复、预算字段、不同调用的循环隔离、成功/失败关闭及单次请求数；实际 AIPlayer 路径验证连接故障、读取中断与 HTTP 400 只有其自身两次尝试。

所有模型请求仍为假密钥及 `MockTransport`；未读取 `.env`，未执行真实 `check_llm.py`，真实中转兼容性留待第五步单独授权。Anthropic 缺原始 `message_stop` 的公开可观测限制保持第一步结论。

第二步最终全量测试 436 项通过；`rules.py --check`、`pip check`、`git diff --check` 通过，规则正文无需重新生成。`main.py --seed 42` 正常完成五局，冠军 Bravo（8 胜），日志保留在 `logs/20261016_114622_bot/`。更新既有 `logs/latest` 时出现 Windows 访问拒绝警告，独立场次日志均已写入；本步保留该既有路径，未删除或替换它。
