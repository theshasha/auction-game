# 模型执行与审计约定

修改模型边界、任务、历史或协议兼容时阅读本文。用户命令与配置政策见 [使用说明](usage.md)，费用授权见根 `AGENTS.md`。依赖版本以 `requirements.txt` 为准；使用现有 Python，测试只用 FunctionModel 或 MockTransport 与假密钥。

## 执行边界

`auction_game/ai/agent_execution.py` 的 `AgentExecutor(factory)(agent, prompt, history, deps=...)` 使用公开 `Agent.run(event_stream_handler=...)` 消费所有模型节点，包括纠错。同步线程每任务一次 `asyncio.run()`；客户端在该事件循环内创建并关闭，工厂不持有跨循环 HTTP 客户端。执行器唯一一次深拷贝输入历史，不识别拍卖任务。

`agent_tasks.TaskRunner` 在业务层定义严格输出与 `AuctionDeps(game, coins)`：bid/plan 共用 `bid` 工具，返回 strategy、bid、必填可空 super_bid；speech 工具返回私有 strategy 与公开 speech；memory 使用 `save_memory`，仅返回非空 memory；summary 返回非空文本。业务 plan 阶段及首日缓存不变。工具映射、显式 description 与字段 schema 集中在 `agent_tasks.py`；自然语言集中在 `prompts.py`，TaskRunner 从映射取得当前工具名，统一追加提交要求，进入消息日志。规则模板在根目录。

`strict=True, extra='forbid'`，拒绝字符串数字、布尔出价与空白策略。super_bid 的公开 before 字段校验仅将精确字符串 `"null"` 转为 None，真实 JSON null 保持不变；schema 保持必填 integer/null，推荐模型提交实际 JSON null。`"None"`、空白包裹或大小写变体、字符串数字与布尔值仍触发类型纠错。原始工具参数保留在审计中，业务结果保存归一化后的 None；正文 JSON 仍须经过工具提交。发言只由提示限长，不硬截断，内部 thinking 不代替业务策略。

格式与规则校验使用 `Agent(retries=1)`，TaskRunner 同时设置公开 `UsageLimits(request_limit=2)`，使文本、未知工具、结构及业务错误混合时也共用一次纠错。SDK 对文本与工具错误存在分别计数的路径，逻辑请求上限防止第三次请求，耗尽同样映射为 `OutputFailedError`。`game_rules.validate_submission` 使用实时余额；只采用最终合法输出。预期模型/传输故障抛 `CallFailedError` 交控制器处理；程序错误原样抛出。连通工具使用独立最小 Agent 与通用执行器，不依赖比赛参数。

## 历史、隐私与暂停

AIPlayer 的游戏上下文 `messages` 与全场回顾 `review_messages` 初始化各含一次 system。执行成功且日志全部写入成功后，把本次实际 user 提示与最终合法业务结果投影为一条普通 user 消息，明确标识为程序提供的已完成任务资料，保留显式 strategy。原提示完整保留在历史区，说明其中的提交指令已完成、无需再次执行；不生成 assistant 正文成功 JSON 示范。不重放原始 response/tool，因此 thinking、加密推理、签名及 provider response ID 均不跨独立任务回传。原始 `new_messages()` 完整留在磁盘审计，不原地改写。当前任务内部纠错仍由 SDK 原样管理，保留协议要求的 thinking 与工具配对。三协议 MockTransport 测试检查真实序列化。成功规划更新首日缓存；首日消费一次且不请求模型。失败不提交历史或缓存。

需继续的局末按 speech 全员完成并公布 → memory 全员完成 → 下一局 plan 执行，每玩家新增一次正常记忆请求，仍共用一次纠错预算。记忆整合旧笔记、本局经历与所有局末公开发言（含本人），整体替换旧记忆。一页笔记是软目标，只有结构与非空校验，无长度纠错或截断。记忆日志成功后，游戏上下文重置为 system 与最新记忆；下一局规划由程序传入最新累计成绩和新局首日状态，不再回塞上一局原始发言。最后一局跳过记忆，加赛按 will_continue 使用同一流程。

检查点保存当前上下文列表、全场回顾列表、累计记忆与首日缓存；消息仅追加或整体替换，快照无需深拷贝消息对象。控制器只原样保存和交还不透明快照，支持 `None`。`RoundTask.participant` 显式接入检查点参与者。暂停停止新派发，等待在途任务结束并读取所有 future，业务校验成功的结果、历史与首日缓存均保留；恢复仅补跑失败或未启动项。全部成功也要在整轮提交前等待确认。快速回车/c 记录恢复意图，先收集再继续；失败重试另等确认。q 排空在途后回滚整轮并退出；程序异常原样传播。两种暂停竞态测试必须保留。

`llm.max_concurrency` 统一解析为正整数，默认 10，拒绝布尔值；1 为串行。生产 AI 与完整离线赛的所有轮级任务共用控制器上限，一次启动读取一次，启动显示并写入本场快照。派发与暂停使用同一同步边界，最多提交上限个任务，每轮输入仍由调用方统一固定。暂停等待期间，任务内部已有的有限网络重试及一次输出纠错可能继续，不增加预算。

失败后的等待闸门必须在公布可操作提示前建立：执行期间旧回车不预放行，失败公告后收到的确认不能再次清空。连续失败每次重新等待确认，离线无人交互入口遇到任务失败则直接退出。

对手只看到 speech，strategy 与 memory 留在自身上下文及私人日志。观众总结读取全场去 thinking 的业务回顾与原总结提示，回复只给观众和日志，不回写玩家历史；这个终局请求仍可能较长。最后一局直接最终感言，跳过普通局末发言及记忆请求。局末出价表直接使用引擎的获胜者、撞车价位和超级命中字段标注款项，不重复结算算法。

## 完整流与重试预算

模型包装器通过公开 `WrapperModel.request_stream()` 缓冲 SDK 完整流，确认成功状态后交付 `CompletedStreamedResponse`。SDK `max_retries=0`；每个请求只允许一次基础设施重发：网络/超时/读取故障、408、429、5xx；400/401/403、配置错误与截断立即失败。按异常类型和明确状态码判断，普通 ModelAPIError 不一概重发。

输出纠错最多两次请求，每次最多两次 HTTP 尝试，混合故障最多四次。失败流的部分响应不进入规范历史；网络重发只重发当前请求，不重做已经完成的纠错节点。request/attempt 按任务内编号，客户端按任务新建。

成功 finish_reason 仅接受 stop/tool_call，length 或缺原因直接失败，即使已有合法工具 JSON。Anthropic 的公开规范响应在已有 message_delta.stop_reason 后无法单独识别缺失的原始 message_stop；目前以明确成功原因为边界。单次响应缓冲期间不向 UI 实时展示 token，底层仍为流式。若要求原始终止帧完整，需另议 SSE 传输检查。

## 协议兼容

ModelFactory 按显式 protocol 选择 Chat、Responses 或 Anthropic，不猜品牌。Chat profile 保持 `openai_chat_streaming_requires_finish_reason=True` 与 `openai_chat_supports_max_completion_tokens=False`，实际使用 max_tokens；Responses 使用 max_output_tokens；Anthropic 使用 max_tokens。超时进入 HTTP read timeout。输出预算、64000 回退和默认 medium 策略见使用文档；显式 reasoning_effort 优先，原始能力列表保持不变。

渠道 `chat_profile.supports_forced_tool_choice` 与 `responses_profile.supports_forced_tool_choice` 只接受布尔，分别作用于 Chat 与 Responses。当前 Hub 均为 false，使 SDK 发送 auto；未设置的渠道保留默认。覆盖保留框架推断的 context 与加密推理 include，推理档位与预算不变。普通 model_settings.tool_choice 会被框架输出要求覆盖，使用公开 profile 才能正确兼容。工具与严格校验仍启用，纯文本合法 JSON 出价仍走共享一次纠错并在耗尽后失败。

公开能力与渠道 profile 分开保存；本场 `data/run_config.json` 记录生效 profile。历史分析和回放只读本场快照，不依赖今天的配置。

## 磁盘审计

`RunPaths` 是日志、统计和回放的共同布局。format_version、run_type、身份编号合入 run_config，不另建 run.json。旧场次保持原地，新读取器只支持当前格式。玩家 JSONL 存本次新增规范消息、结果与错误；失败审计按 run_id 选取，排除 interrupted 占位响应，避免相邻 request 合并导致漏记。

写入顺序为任务 JSONL、可选捕获、显式私人 Markdown；任一失败向上传播，成功日志完成后才提交内存状态。没有跨文件事务：后续写入失败会留下部分审计，但上下文、记忆与首日缓存不提交。记忆任务记录 `memory_chars`（Python 字符长度，非 token），私人 Markdown 展示完整笔记及长度，仅用于观察。

默认轻量工具审计先于 HTTP 发送：`llm_client.py` 的公开 HTTP request hook 调用 `tool_records.protocol_tools()`，只提取实际存在的 tools/tool_choice/parallel_tool_calls。AgentLog 在任务开始时分配既有 task_id，通过 ContextVar 关联玩家、业务任务、BufferedRequest 的 request/attempt。每场共享 ToolWriter 在锁内按协议及字段内容哈希去重，定义写 `data/tool_definitions.json`，来源与请求引用写 `data/tool_requests.jsonl`，随后更新 `tools.md`。磁盘异常即使被 SDK 包装成连接错误也原样传播，不消耗网络重发；此时不提交历史或缓存。失败请求的发送前记录保留，无跨文件事务。README 与私人 Markdown 直接链接工具页，任务 JSONL 引用定义 ID。

真实捕获标记 `http_pre_send`（发送前实际请求定义），FunctionModel 用生产 SDK + MockTransport 序列化并标记 `offline_sdk_generated`（离线 SDK 生成）。仅有场次上下文时启用；无请求显示尚无记录，总结记录无输出工具。不保存鉴权头、密钥、完整 body 或历史副本。独立连通检查不创建场次；旧场次不补写。默认轻量审计与下述完整捕获互相独立。

请求捕获默认关闭。开启时 RequestCapture 记录真实逻辑边界 messages/settings/parameters/response，包括失败、纠错、回滚后的分叉和总结；不猜测成功历史。RequestWriter 将 schema/settings 按内容哈希存一次，历史用前次相同消息前缀加变化尾部，尾部和响应优先引用任务审计并保留元数据差异。重建严格比较原值和哈希，保留 null、false 与缺失字段；错误从任务审计索引引用。正常模型输入不改，完整展开只按需导出 debug。

回放 SDK 定义通过本场快照构建真实 ModelFactory，假密钥与 MockTransport 拦截所有请求，以模拟 400 结束。只保存 tools/tool_choice/parallel_tool_calls（实际存在的字段）及 SDK 版本，无完整 body、鉴权头或重复摘录。重导出复用已有定义；缺定义明确要求离线补捕获，不静默使用新 SDK 或联网。FunctionModel 逻辑记录没有真实 HTTP 报文，回放明确标注模拟性质。

## 回归入口

`tests/ai/` 覆盖真实 SDK 线协议、共享纠错、重试预算、严格类型、历史与隐私。`tests/app/` 覆盖控制器、启动同步、路径和统计；`tests/integration/` 复用完整假模型赛验证所有产物、首日缓存、回放与资源关闭。公共辅助在 `tests/support/`，夹具留 `tests/conftest.py`，测试文件不互相导入。

Hub Responses 兼容的历史事实：已授权真实对照曾得到 required=400、auto=成功，公开 profile 的生产 Agent 保留 high/context/include 后成功。该结论只覆盖当时探测；后续收费比赛仍按用户确认流程执行。旧验证材料保留在原归档，不在本文重复测试计数或开发步骤流水。
