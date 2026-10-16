# 使用说明

在项目根目录用现有 Python 安装依赖：`python -m pip install -r requirements.txt`。

## 修改配置

四份手工 YAML 在 `configs/`：`game.yaml` 改比赛参数；`models.yaml` 定义模型、角色名及协议；`players.yaml` 改阵容和人设；`config.yaml` 定义渠道及测试模型。密钥写根 `.env` 中配置指定的环境变量。

`config.yaml` 的 `llm.max_concurrency` 控制同时执行的玩家任务上限，默认 10，设为 1 即串行；必须为正整数。实际并发不超过当前任务数，覆盖出价及各阶段发言。启动时读取并保存到场次快照，修改后下次启动生效。降低并发可缓解负载，额度耗尽或无可用渠道仍需渠道侧处理。

`models.yaml` 的 `provider` 引用渠道，`protocol` 必填 `chat` / `responses` / `anthropic`；渠道名不决定协议。OpenAI URL 保留 `/v1`，Anthropic URL 由 SDK 追加 `/v1/messages`。测试替身可以省略角色名，参赛身份必须有名字。

Chat/Responses 模型可用 `reasoning_effort: low | medium | high` 显式覆盖自动选择的推理档位，仅影响该目录模型；删除该字段恢复能力快照策略。`protocol: chat` 使用 OpenAI Chat Completions。覆盖不改写公共能力快照，实际生效设置保存到场次中，下一次启动生效。

渠道可分别设置 `chat_profile.supports_forced_tool_choice: false`、`responses_profile.supports_forced_tool_choice: false`，使对应协议使用 `tool_choice: auto`；工具定义与严格输出校验保留。

比赛启动自动更新根 `RULES.md`，无需先运行生成命令。改规则正文请编辑 `RULES.template.md` 并同步检查代码。根规则显示配置身份，本场规则显示实际参赛名单。

能力快照在 `data/model_capabilities.json`，与模型目录位置分开；启动只读，不联网。免费公共能力同步命令：

```bash
python -m auction_game.cli.sync_models
python -m auction_game.cli.sync_models --write
python -m auction_game.cli.sync_models --input path/to/api.json --write
```

默认仅预览；`--write` 原子写入，数据相同不更新。按 models.dev 精确来源匹配，不猜模型别名。比赛输出采用有效最大输出，未知回退 **64000**；独立 input 与 context 只留作元数据，不缩减输出或裁历史。明确列出 **high** 时，Chat/Responses 使用公开 `openai_reasoning_effort=high`；缺档位、toggle 或预算不会猜算成 high，未知状态在启动中报告。渠道拒绝或截断直接报错，不降低预算重发。

## 运行比赛

```bash
python main.py --seed 42
python main.py --benchmark 50
python -m auction_game.cli.offline_demo
python -m auction_game.cli.offline_demo --test-model
```

Bot 固定种子便于复现。benchmark 两组各 N 场，共 2N 场，不写日志；策略组每天出 `min(出价上限, 当前余额)`，不使用超级出价，对比均值取第二组。offline 使用生产完整比赛流程，仅模型边界换成确定性假模型，零真实服务调用；假模型按实时余额提交合法出价，无交互任务失败时直接报错退出，不等待回车。`--player 角色名` 选择捕获与回放玩家，默认阵容第一位。`--output 全新目录` 仅用于显式指定输出，不覆盖已有目录。

以下命令调用真实服务并可能收费，由用户亲自运行：

```bash
python -m auction_game.cli.check_llm --test-model
python main.py --ai --test-model
python main.py --ai
```

先完成测试模型整场比赛，经检查确认后使用常规阵容。`--test-model` 使用 `configs/config.yaml` 的 `test_model` 目录键；要用现有 GLM Flash，手动设为 `test_model: glm_flash`，无需改阵容或新增命令参数。启动提示显示所选目录键、实际模型 ID 与协议。测试模式整体替换执行模型、能力、预算与推理设置，保留身份、人设及独立历史；原测试模型的 `reasoning_effort: low` 不会带入新选择。

连通检查的 `--model 目录键` 可重复，默认按阵容实际执行配置去重；单目标至多一次流式工具请求，无重试。默认探测预算 2048，可用 `--max-tokens` 调整且不超过比赛预算；探测不依赖 `game.yaml`，不改比赛预算。智能体执行真实探测的授权边界见根 `AGENTS.md`。

交互控制：回车推进一次出价或发言；`c` 连续；`s` 单步；`p` 暂停；`q` 中止。暂停停止派发新任务，等待在途任务结束，保留校验成功的结果及玩家历史；恢复仅执行失败或未启动项。整轮全部成功时也会先等待恢复确认，再提交结果。等待期间在途任务原有的有限重试及输出纠错可能继续，HTTP 请求不会被强杀。`q` 停止新派发并等待在途收尾。`main.py` 成功或主动中止返回 0，已处理启动错误返回 1，参数错误返回 2。

## 阅读场次

Typora 输入折叠采用连续的纯 HTML 块，换行使用 `br`，特殊字符先转义；不混用代码围栏或独立空锚点。缩进制表符按四列展开。补跑通过唯一的中文章节标题跳转，重复阶段追加记录序号。此格式仅作用于新生成日志。

所有新场次在 `logs/<YYYYMMDD_HHMMSS>_<ai|ai-test|bot|offline>/`；同秒加序号防覆盖。`ai-test` 同样是真实收费比赛。类型同时保存在快照，offline 的常规/替身差异保存在 `mode`。

先打开 `README.md` 查看结果与链接。`RULES.md` 是本场规则；`speeches.md` 为公开发言；`matches/` 为逐局结果；`players/` 是含私有策略的私人对话，分享前注意隐私。文件用本场补零编号，标题显示角色名。

私人对话按中文比赛阶段展示，输入原文折叠，私有策略及公开发言各保留一次原文。提交出价与实际结算分开；正文不显示任务编号、常规成功状态、网络尝试次数或逐条审计入口，纠错及失败仅显示简短说明。同一任务补跑提示恢复结果并链接首次相同输入与上次记录。完整工具消息、错误及补跑关联保存在 `data/players/` 的机器审计中，历史场次不自动重写。

机器 JSON/JSONL 全在 `data/`：`run_config.json` 保存版本、类型、玩家编号及实际执行快照，`matches/` 保存结算，`players/` 保存增量任务审计。仅启用请求捕获时才有 `data/replay/`。

```bash
python -m auction_game.reporting.analyze
python -m auction_game.reporting.analyze logs/<场次目录>
```

默认只选最新有效的常规真实 `ai`，不会混入测试赛、Bot 或模拟；没有时明确提示。其他类型显式传目录。旧格式目录报错，历史数据原地保留。统计口径见 [统计说明](analyze.md)。

## 回放与调试

offline 自动生成 `replay.md`。真实比赛的完整请求捕获默认关闭；程序调用 `run_ai(record_player=角色名)` 可仅为指定玩家开启。捕获来自实际模型边界，包含失败与纠错；原内存输入不改，磁盘采用前次相同前缀加变化尾部，引用任务响应并校验重建。

回放的模拟标识只读取本场快照，无需也不接受 `--simulated` 开关。

```bash
python -m auction_game.reporting.replay logs/<场次目录> 角色名
python -m auction_game.reporting.replay logs/<场次目录> 角色名 --debug
```

回放只展开开头、第一局首日缓存与第二天、第一局末到第二局规划、实际终局；其他日局跳过。提示全文、首次工具 JSON、原始回复与程序处理清楚分开，重复定义引用。`--debug` 才生成 `debug/<玩家编号>.md` 完整展开，不默认保存重复完整请求副本。

SDK 工具定义通过真实适配器的离线 MockTransport 捕获，保存定义与来源版本，不保存完整 HTTP body。重新导出复用原定义；缺失会报错，可明确使用 `--capture-definitions` 离线补捕获。不会联网或静默更新 SDK 证据。运行记录含私有内容；捕获不保存密钥或鉴权头。

## 手动检查规则

```bash
python -m auction_game.game.rules
python -m auction_game.game.rules --check
```

`--check` 只读，过期或模板错误返回非零。默认路径由项目位置确定；从其他目录运行根 `main.py` 可用绝对路径，`-m` 工具需要项目已在 Python 模块搜索路径（例如设置 `PYTHONPATH=C:\project\auction-game`）。程序不修改 `sys.path`。
