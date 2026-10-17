# 使用说明

在项目根目录用现有 Python 安装依赖：`python -m pip install -r requirements.txt`。

## 准备配置

四份手工 YAML 在 `configs/`：`game.yaml` 改比赛参数；`models.yaml` 定义模型、角色名及协议；`players.yaml` 改阵容和人设；`config.yaml` 定义渠道及测试模型。密钥写根 `.env` 中配置指定的环境变量。

比赛启动自动更新根 `RULES.md`，无需先运行生成命令。改规则正文请编辑 `RULES.template.md` 并同步检查代码。根规则显示配置身份，本场规则显示实际参赛名单。

## 运行比赛

```bash
python main.py --seed 42
python main.py --benchmark 50
python -m auction_game.cli.offline_demo
python -m auction_game.cli.offline_demo --test-model
```

- Bot：固定种子便于复现。
- benchmark：两组各 N 场，共 2N 场，不写日志。策略组每天出 `min(出价上限, 当前余额)`，不使用超级出价，对比均值取第二组。每日均值口径在开头说明一次，两组逐日只列平均出价与平均奖池。
- offline：完整比赛流程使用确定性假模型，不调用真实服务，不收费，无需键盘推进；任务失败直接退出。`--player 角色名` 选择捕获与回放玩家，默认阵容第一位；`--output 全新目录` 可指定输出，不覆盖已有目录。

以下命令调用真实服务并可能收费，由用户亲自运行：

```bash
python -m auction_game.cli.check_llm --test-model
python main.py --ai --test-model
python main.py --ai
```

先完成测试模型整场比赛，经检查确认后使用常规阵容。`--test-model` 使用 `configs/config.yaml` 的 `test_model` 目录键；切换时只需修改该键，无需改阵容。启动时只显示一次测试模型，提醒真实调用可能收费；常规阵容逐行显示角色与模型。两种模式都显示并发上限与日志目录，详细调用设置保存在场次 `data/run_config.json`。测试模式整体替换执行模型、能力、预算与推理设置，保留身份、人设及独立历史。

连通检查的 `--model 目录键` 可重复，默认按阵容实际执行配置去重；单目标至多一次流式工具请求，无重试。默认探测预算 2048，可用 `--max-tokens` 调整且不超过比赛预算；探测不依赖 `game.yaml`，不改比赛预算。智能体执行真实探测的授权边界见根 `AGENTS.md`。

### 交互控制

回车推进一次出价或发言；`c` 连续；`s` 单步；`p` 暂停；`q` 中止。

暂停后不再启动新任务，等待已启动的任务结束，保留成功结果及玩家历史；按回车或 `c` 继续时，只执行失败或未启动的任务。整轮全部成功时也要确认继续，才提交结果。已启动任务原有的有限重试及输出纠错可能继续，不会强行取消 HTTP 请求。`q` 同样等待已启动任务结束，再撤销本轮全部玩家状态并中止比赛。

任务失败后必须重新按回车或 `c` 确认重试，失败前按过的继续键不算确认；执行期间误按的回车也不会提前放行下一轮。`c` 恢复后保持连续模式，回车恢复后保持单步模式。

`main.py` 成功或主动中止返回 0，已处理启动错误返回 1，参数错误返回 2。

## 阅读场次

输入原文直接展开为 Markdown，保留换行与表格，不使用 HTML 折叠。补跑通过唯一的中文章节标题跳转，重复阶段追加记录序号。比赛期间请用只读预览查看日志，避免编辑器把旧内容保存回正在追加的文件。此格式仅作用于新生成日志。

所有新场次在 `logs/<YYYYMMDD_HHMMSS>_<ai|ai-test|bot|offline>/`；同秒加序号防覆盖。`ai-test` 同样是真实收费比赛。类型同时保存在快照，offline 的常规/替身差异保存在 `mode`。

先打开 `README.md` 查看结果与链接。`tools.md` 展示本场工具定义；`RULES.md` 是本场规则；`speeches.md` 为公开发言；`matches/` 为逐局结果；`players/` 是含私有策略的私人对话，分享前注意隐私。文件用本场补零编号，标题显示角色名。

逐局表格与模型局末提示共用精简格式：保留报价、奖池、撞车价位及结算结果，固定扣款、超级成本与悬赏不逐格重复，普通流局只写“流局”，末日注明奖池作废。逐局报告可直接跳到本场规则；每日个人结算仍明确说明扣款。完整结算字段保存在 `data/matches/`，不随 Markdown 精简。

私人对话按中文比赛阶段展示，区分提交出价与实际结算。私有策略、公开发言及累计记忆保留原文，记忆长度按字符计（非 token）。纠错及失败显示简短说明，补跑记录链接此前输入。完整工具消息与错误保存在 `data/players/`；历史场次不自动重写。

机器 JSON/JSONL 全在 `data/`：`run_config.json` 保存版本、类型、玩家编号及实际执行快照，`matches/` 保存结算，`players/` 保存增量任务审计。仅启用请求捕获时才有 `data/replay/`。

### 上下文与私有记忆

每日出价提示列出日期、奖池、余额与胜场，第 2 天起同时提供昨日报价和全员余额。余额已含当天工资；使用超级时的费用约束单独说明，不额外计算出价组合。最后一天提醒本局结束后金币清零。

游戏请求保留本局原生对话：user 提示、assistant 正文与工具调用、工具回执及纠错往返。每次成功写日志后，只追加去掉 thinking 的副本，不再包装成 user JSON 资料。跨任务清除旧响应引用，保留工具配对及 assistant phase；任务内部纠错由 SDK 保留所需推理状态。旧 thinking 留在原始审计。对手只看到公开发言，私有策略与记忆仅供本人使用。私人 Markdown 的“输入原文”是本次新增输入，先前历史见前序任务；完整请求可用现有请求捕获功能查看。

每个需继续的局末先公布全部发言，再为每位玩家新增一次 `save_memory` 请求，整合旧笔记、本局经历及局末发言。一页笔记是软目标，无硬字数限制或截断。日志成功后清除旧游戏对话；下一局带上规则身份、最新记忆、程序提供的成绩及新局信息，再规划并缓存首日出价。最后一局跳过记忆，加赛同样按是否继续处理。

观众总结单独读取全场去 thinking 回顾，不回喂玩家，终局请求仍可能较长。实际记忆质量及费用需结合真实测试赛的 usage 观察。

2026-10-18 独立连通验证：Luna Responses 共 5 次成功请求，覆盖连续工具调用、切换记忆工具及记忆后重置；Luna Chat 两次探测均返回渠道 503，随后停止。改用 Sol Responses 验证“出价→记忆→新局出价”，3 次请求全部成功。成功响应均未返回 thinking 或签名；签名清理及 phase 保留已由三协议离线 SDK 回归覆盖，真实带签名响应的清理仍待验证。探测未改变生产协议配置。

## 分析统计

```bash
python -m auction_game.reporting.analyze
python -m auction_game.reporting.analyze logs/<场次目录>
```

默认从 `ai` 与 `ai-test` 中选择最新有效场次，两类比赛同等参与选择；Bot 或 offline 需显式传目录。没有有效场次时明确提示。旧格式目录报错，历史数据原地保留。统计口径见 [统计说明](analyze.md)。

## 回放与调试

### 排查入口

- 当场工具定义：场次 `README.md` → `tools.md`，无需开启完整请求捕获。真实比赛标明“发送前实际请求定义”，离线赛标明“离线 SDK 生成”。
- 原始工具调用与校验错误：`data/players/<编号>.jsonl`，按工具页的 task_id 对照。请求引用见 `data/tool_requests.jsonl`，去重定义见 `data/tool_definitions.json`。
- 修改自然语言提示：`auction_game/ai/prompts.py`；工具名、description 与字段 schema：`auction_game/ai/agent_tasks.py`。输出提交要求由任务组装时统一追加，进入实际消息日志。

轻量工具记录不保存完整请求历史或鉴权头；需要查看消息历史与请求参数时，再开启完整捕获。连通检查不创建场次。

### 完整请求捕获与回放

offline 自动生成 `replay.md`。真实比赛的完整请求捕获默认关闭；程序调用 `auction_game.runners.run_ai(record_player=角色名)` 可仅为指定玩家开启，CLI 暂无此开关。捕获包含失败与纠错，未开启捕获的历史场次无法事后补出完整请求。

回放的模拟标识只读取本场快照，无需也不接受 `--simulated` 开关。

已开启捕获的真实场次首次导出时，需离线补捕获 SDK 工具定义：

```bash
python -m auction_game.reporting.replay logs/<场次目录> 角色名 --capture-definitions
```

此操作使用本场快照，通过 MockTransport 生成定义并记录当前 SDK 版本，不调用真实服务。已有定义会复用。之后重新导出或查看完整展开：

```bash
python -m auction_game.reporting.replay logs/<场次目录> 角色名
python -m auction_game.reporting.replay logs/<场次目录> 角色名 --debug
```

回放只展开开头、第一局首日缓存与第二天、第一局末发言到独立记忆及第二局规划、实际终局；其他日局跳过。提示全文、首次工具 JSON、原始回复与程序处理清楚分开，重复定义引用。`--debug` 才生成 `debug/<玩家编号>.md` 完整展开，不默认保存重复完整请求副本。

运行记录含私有内容，分享前请检查；捕获不保存密钥或鉴权头。

## 进阶配置

### 并发与协议

`config.yaml` 的 `llm.max_concurrency` 控制同时执行的玩家任务上限，默认 10，设为 1 即串行；必须为正整数。实际并发不超过当前任务数，覆盖出价及各阶段发言。修改后下次启动生效，并写入场次快照。降低并发可缓解负载，额度耗尽仍需渠道侧处理。

`models.yaml` 的 `provider` 引用渠道，`protocol` 必填 `chat` / `responses` / `anthropic`；渠道名不决定协议。`chat` 使用 OpenAI Chat Completions。OpenAI URL 保留 `/v1`，Anthropic URL 由 SDK 追加 `/v1/messages`。测试替身可以省略角色名，参赛身份必须有名字。

渠道 Chat 与 Responses 默认使用 `tool_choice: auto`，仅 Kimi 在模型目录中覆盖开启强制工具提交；按实际执行模型选择，测试替身不会继承角色身份的设置。工具定义与严格输出校验始终保留。配置仅在启动时读取。

2026-10-18 对六个参赛模型各执行一次并发探测：Kimi、两个 Gemini 通过；两个 GPT Responses 返回 `Missing required parameter: 'tool_choice.type'.`，DeepSeek 返回 thinking 模式不支持该 tool_choice。最终仅对 Kimi 启用，其他模型沿用 auto。

### 能力快照与推理档位

能力快照在 `data/model_capabilities.json`，启动只读，不联网。免费公共能力同步命令：

```bash
python -m auction_game.cli.sync_models
python -m auction_game.cli.sync_models --write
python -m auction_game.cli.sync_models --input path/to/api.json --write
```

默认仅预览；`--write` 原子写入，数据相同不更新。按 models.dev 精确来源匹配，不猜模型别名。

- 输出预算采用有效最大输出，未知回退 **64000**。input 与 context 只留作元数据，不缩减输出或裁历史。渠道拒绝或截断直接报错，不降低预算重发。
- 能力明确列出 **medium** 时，Chat/Responses 自动使用 medium。仅 high、toggle、预算或缺档位，以及未适配协议，均沿用渠道默认。
- Chat/Responses 模型可在目录中设置 `reasoning_effort: low | medium | high`，优先于自动选择；删除该字段恢复能力快照策略。只影响该目录模型，不改写公共能力快照。

设置下一次启动生效，实际预算与推理设置保存在本场 `data/run_config.json`，也可用 `sync_models` 查看。正常启动不逐人展示技术详情；输出预算需要回退时，按实际模型去重提醒，并给出同步命令与来源配置入口。离线假模型赛不显示真实调用的预算提醒。

## 手动检查规则

```bash
python -m auction_game.game.rules
python -m auction_game.game.rules --check
```

`--check` 只读，过期或模板错误返回非零。默认路径由项目位置确定；从其他目录运行根 `main.py` 可用绝对路径，`-m` 工具需要项目已在 Python 模块搜索路径（例如设置 `PYTHONPATH=C:\project\auction-game`）。程序不修改 `sys.path`。
