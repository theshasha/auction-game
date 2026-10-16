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

拍卖业务提示统一由 `prompts.py` 按 system、开局、规划、出价、局末、终局生成；规则正文仍来自 `RULES.template.md`。调用方只传状态，字段说明及 schema 继续由 `agent_tasks.py` 定义；业务纠错留在任务 validator，动态出价错误来自 `validate_submission`。通用执行器与连通检查独立于拍卖提示。

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

`match_*.json` 比赛日志结构保持原样，`run_output.py`、`analyze.py` 继续使用原结构。场次各自保存规则及 game 参数快照；统计默认扫描最新时间戳 AI 场次，也可显式传入演示目录。已有归档保持原状。

第三步覆盖规划→首日缓存→日常出价→公开发言→观众总结的任务串联、余额与严格类型校验、共享纠错、三协议重试及流失败隔离、暂停回滚、隐私与资源关闭。第四步使用同一生产 `run_ai` 入口执行当前五局完整赛，只在模型边界注入 `FunctionModel`，控制器通过既有连续命令推进。真实中转的工具定义、强制工具选择及参数兼容性仍属第五步，须另行授权，不自动换模型。

第三步补充后的离线回归为 374 项：将旧玩家侧快照测试改为非拍卖 Agent 的类型化 deps、历史隔离及失败审计回归；新增非 AIPlayer 参与者的真实暂停回滚和日志写入失败边界。请求序号断言复用原混合重试及跨任务测试，严格校验与两种暂停竞态沿用现有回归，未扩展业务三协议矩阵。

### 离线完整赛与关键节点回放

```bash
python offline_demo.py
python offline_demo.py --player 鲲鲲 --output demos/another_offline
python analyze.py demos/<生成目录>
```

默认回放 `players.yaml` 第一名玩家；输出目录必须是新目录，防止追加旧审计。生成物位于 Git 忽略的 `demos/`，与真实比赛日志分开，包含五局 JSON/Markdown、规则、全部玩家任务审计。所选玩家另有 `replay_<玩家>.md` 阅读正文、`debug_<玩家>.md` 完整调试记录、`requests_<玩家>.jsonl` 逻辑请求及 `protocol_<玩家>.json` 离线 SDK 捕获。测试直接调用同一 `generate_demo()`，没有另写比赛流程。模拟策略固定第一名玩家每天出 3，其余出 0，不使用超级出价；当前配置下完成 30 天，第一名获得 30 胜，无加赛。示例用于解释程序行为，不衡量模型实力。

回放只展开开局、首局规划、首局第二天、第一局到第二局衔接、实际终局感言及观众总结；系统全文只出现一次。正文按中文环节展示实际新增提示、协议工具定义、原始业务回复及程序处理；相同协议定义用中文锚点引用，内部编号及设置留在链接到的调试记录，真实纠错请求与首次回复都保留。首日缓存消费不发请求。最后一局跳过普通局末发言，最终感言直接包含最后一局日志；总结看完整历史，但只面向观众且不回写历史。

`run_ai(record_player=名称)` 为指定玩家启用可选逻辑请求审计，普通比赛默认关闭。`RequestCapture` 通过公开模型包装边界捕获完整规范 messages、输出工具/schema、模型 settings、任务内请求编号及对应回复，Agent 纠错请求同样记录；它不更改流结束检查或重试预算。快照不读取 SDK 鉴权配置，不保存密钥或鉴权头；消息本身含私有策略，分享前按私有日志处理。这些是模型边界逻辑记录，FunctionModel 没有实际 HTTP 请求。

`python replay.py <场次目录> <玩家名> --simulated` 可从已有模拟审计重新导出；未来获授权的真实运行记录省略 `--simulated`。导出器只读运行记录中的回复，不生成示例答案。正文工具 JSON 来自当前玩家配置的生产 `ModelFactory`：通过公开 `request_stream()` 将规范输入及参数交给真实 provider/SDK，由 `MockTransport` 捕获序列化 JSON，再以模拟 400 结束，无须生成模型回复。所有 HTTP 请求均拦截，使用假密钥，不读取 `.env`，不保存鉴权头。按定义/设置组合复用捕获，重新导出复用已有协议文件，不重跑比赛。保留 SDK 实际发出的 `tools`、`tool_choice`、`parallel_tool_calls`（若存在），其中的 null/false 与可空 schema 原样保留；框架内部 `ToolDefinition` 字段只留在逻辑调试记录。来源标注为“SDK 在离线模拟传输中生成的请求定义，未发送到真实中转”，真实中转处理仍须授权验证。完整逻辑请求导出与玩家审计共享同一运行捕获数据；双文件写入失败策略仍按上文限制处理。

第四步全量回归 375 项通过，保留两种暂停竞态、重试与私有字段隔离检查；新增唯一完整 AI 比赛测试同时验证回放、首日无重复请求、资源关闭及实际统计入口读取本场快照。`rules.py --check`、`pip check`、`git diff --check` 通过。Bot CLI 固定种子 42 完成五局，Bravo 8 胜。离线演示完成五局无加赛，统计读取 30 天 / 180 笔正常出价，非法出价 0；当前胜场矩阵为珂珂每局 6 胜，其余每局 0。真实服务验收尚未执行。

回放修正复用上述测试：三协议捕获与原 SDK 请求的工具 JSON 逐项相等，实际 null/false 保留；完整赛检查定义去重、锚点定位及回复原文；已有纠错失败测试同时检查首次回复与纠错输入均出现在回放。未新增整场三协议矩阵，全量仍为 375 项。
