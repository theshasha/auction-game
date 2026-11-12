# 使用说明

命令都在项目根目录运行。

## 准备配置

- `configs/game.yaml`：局数、金币、奖池、出价上限和超级出价参数。
- `configs/models.yaml`：模型目录。每个条目写调用的模型 ID、渠道、协议和 models.dev 来源；`name` 是参赛时默认的角色名。
- `configs/players.yaml`：个人赛阵容、人设和虚构的预测胜率。玩家写 `model`、`persona_title`、`persona_text` 和 `predicted_win_rate`，`name` 可以不写，不写时用模型目录里的角色名；同一个模型放多名玩家时，给每人写 `name`。名字的要求见[信件](letters.md#名字)。
- `configs/teams.yaml`：团队赛阵容，正好 2 个阵营、每个 3 人。每个阵营写 `name`（阵营名，不能为空或重复）和 `players`；玩家的字段和名字要求同 `players.yaml`，但没有预测胜率。
- `configs/config.yaml`：渠道、超时、并发上限、测试模型和提示开关。

密钥写在根目录的 `.env` 里，变量名是渠道的 `api_key_env`，可以照 `.env.example` 写。

### 接入模型

在 `config.yaml` 的 `llm.channels` 里加渠道，再在 `models.yaml` 里用 `provider` 引用它。`protocol` 取 `chat`、`responses` 或 `anthropic`。OpenAI 协议的渠道地址保留 `/v1`；Anthropic 渠道的地址不带 `/v1`，SDK 会自己追加 `/v1/messages`。

每次请求允许的最大输出取自能力快照 `data/model_capabilities.json` 里该模型的最大输出；快照里没有时用 64000。推理档位按以下顺序确定：

1. 模型条目里写了 `reasoning_effort: low | medium | high`，就用它。
2. 否则模型的来源列出了 `high`，就用 `high`。
3. 其余情况用渠道默认档位。

`anthropic` 协议的请求都申请 1 小时提示缓存，实际时长由渠道决定：`hub_anthropic` 实测按 5 分钟写入。其他协议由渠道自行缓存。

新增模型、改了 `models_dev` 来源或要更新能力数据时，从 models.dev 同步快照：

```bash
python -m auction_game.cli.sync_models                              # 预览差异
python -m auction_game.cli.sync_models --write                      # 写入快照
python -m auction_game.cli.sync_models --input "path/to/api.json" --write  # 用本地下载的 models.dev 数据
```

## 运行比赛

```bash
python -m auction_game.cli.offline_demo         # 假模型完整赛，无需密钥
python -m auction_game.cli.offline_demo --team  # 假模型团队赛
```

离线赛用按脚本回复的假模型，默认配置下会演示流局、超级出价命中与落空、加赛和私信；团队赛没有私信，还会演示开局讨论、队内信件区、只狙到队友价位的落空和两队同分加赛。`--test-model` 改用测试模型的配置和测试赛参数，`--output 目录` 指定输出位置。

**以下命令调用真实服务，可能收费：**

```bash
python main.py --test-model
python main.py
python main.py --team --test-model
python main.py --team
```

`--team` 跑团队赛，阵容读 `teams.yaml`，赛制和规则见[团队赛](team-mode.md)。`--test-model` 让全员使用 `config.yaml` 里 `test_model` 指定的模型，角色名、人设和各自的对话历史不变。比赛参数改用 `game.yaml` 里 `test_model_overrides` 覆盖后的值，用来缩短局数和天数。

启动时会按 `game.yaml` 的生产参数生成根目录的规则，个人赛是 `RULES.md`，团队赛是 `RULES_TEAM.md`，测试模式也一样；本场实际规则在场次目录的 `RULES.md`。改了 `RULES.template.md`、`RULES_TEAM.template.md`、`game.yaml` 或阵容后，也可以手动生成或检查两份：

```bash
python -m auction_game.cli.rules          # 更新 RULES.md 和 RULES_TEAM.md
python -m auction_game.cli.rules --check  # 过期或模板有错时返回非零
```

## 交互控制

比赛默认单步：回车执行下一轮任务（如一轮出价或发言），`c` 切到连续模式，`s` 切回单步，`p` 暂停，`q` 退出。终端排名表的金币列是当天结算后、发次日工资前的余额；同分的玩家名次相同，金币多的列在前面。

模型回复格式不对或出价违规时，程序会让它改一次；还不对，这个任务就算失败。请求失败（服务报错、网络中断、输出被截断）时，程序等 10 秒重发，最多重发 3 次。

- 暂停后，程序不再启动新任务，也不再重发失败的请求，等进行中的请求结束后停下等你确认。
- 有任务失败时，本轮其余任务照常跑完，然后停下等你确认。
- 回车或 `c` 继续，只补跑失败和没启动的任务，成功的不重跑。每次出现新的失败都要重新确认。
- `q` 马上取消已启动的任务并退出，本轮已成功的结果留在日志里，[续跑](#中断后续跑)时沿用。被取消的请求可能已经收费。
- `c`、`s` 和第一次按的 `p`、`q` 收到后会显示提示；认不出的输入会原样显示，包括终端混入的控制字符。按了没有提示，说明终端没把这次按键送到程序。

## 中断后续跑

比赛中途按 `q`、关掉窗口或程序崩溃后，可以接着跑完。**续跑同样调用真实服务，可能收费：**

```bash
python main.py --resume              # 最新一场没打完的真实比赛
python main.py --resume logs/<场次>  # 指定场次
```

任务日志里已经成功的任务直接取用，不再请求模型，被中断那一轮里已成功的也算。终端显示从哪一轮恢复，回车后继续；日志里已经有全部轮次、只差公布时，直接补上并打印最终结果。

- 比赛参数、阵容、每个席位调用的模型和系统提示都取自这一场，不看当前的 `game.yaml`、阵容、人设和写作要求。`--resume` 不能和 `--team`、`--test-model` 一起写，也不能换模型。
- 渠道地址、密钥、超时和并发取当前的 `config.yaml` 和 `.env`，渠道出问题时可以关掉比赛、换地址再续跑。渠道可能悄悄降级：`logs/20261105_191747_ai` 的 Grok 席位模型名、协议和推理档都没变，输出中位数却从 9018 降到 2157 token，思考消失、纠错变多，最终感言时换了渠道才恢复。比赛里输出明显变短、纠错变多时，先查渠道。
- 续跑会核对重新生成的每条请求和日志里当时发出的是否相同。中途改过提示词或流程代码，这场就续不上，终端说明是哪名玩家的哪一轮，场次文件不动。
- 离线赛、已经打完的场次、刚建好目录就关掉的场次不能续跑。

## 阅读场次

每场写在 `logs/<时间>_<类型>/`，类型是个人赛的 `ai`、`ai-test`、`offline`，或团队赛的 `team`、`team-test`、`team-offline`。先打开里面的 `README.md`，团队赛的私人对话按阵营分组列出。

- `RULES.md`：本场规则。
- `speeches.md`：公开发言和观众总结。
- `letters.md`：私信，团队赛是队内信件区，按写信的环节分节，有人写信的环节结束后追加。
- `matches.md`：逐局出价表和累计排名，每局结束时追加。
- `players/`：每名玩家的私人对话。开头是系统提示，之后每节是当次新增的输入和最终结果，收到的信在输入里，写出的信在结果的"私信"一节，团队赛是"队内信件区"一节；局末整理记忆后，对话从系统提示和新记忆重新开始。补跑用同一份输入重新请求，模型看不到失败的那次；按 `q` 时进行中的任务记为"已取消"。
- `data/run_config.json`：本场实际使用的模型、输出预算、推理档位和提示开关。
- `data/matches/`：完整结算数据。
- `data/players/<编号>.jsonl`：任务记录，含模型原始回复、thinking 和完整错误。

## 分析

```bash
python -m auction_game.cli.analyze                    # 最新的真实 AI 场次，含团队赛
python -m auction_game.cli.analyze "logs/场次目录"   # 指定场次，离线赛要这样指定
```

团队赛两队对比的指标，包括贴上限、按余额狙击、去掉超级出价重算和队内撞车重算，用另一个命令，可以一次传多场：

```bash
python -m auction_game.cli.team_report                                      # 最新的真实团队赛
python -m auction_game.cli.team_report "logs/场次目录" ["logs/场次目录" ...]   # 指定一场或多场，离线赛要这样指定
```

各项指标的口径见[统计说明](analyze.md)。

## 连通检查

**会调用真实服务，可能收费。** 每个模型最多请求一次，不重试。请求把随机核对码放进系统提示，回复里有这个码才算通过；有回复但没有核对码时显示 `[系统提示未生效]`，说明渠道丢弃或替换了系统提示，模型看不到规则。结果显示回复开头和输入 token 数。这次请求只有几十个字，各模型分词不同，差两三倍属于正常；上千 token 说明渠道额外加了内容。

```bash
python -m auction_game.cli.check_llm --test-model         # 只查测试模型
python -m auction_game.cli.check_llm --model 目录键       # 只查指定模型，可重复
python -m auction_game.cli.check_llm                      # 查常规阵容用到的全部模型
```

`--max-tokens` 设探测的输出上限，默认 2048，不超过该模型的比赛输出预算。不带参数时查的是 `players.yaml` 的阵容；团队赛阵容用到别的模型时，用 `--model` 指定。

## 常用调整

- 预测：`config.yaml` 的 `prompts.enable_predictions` 关掉后，模型看不到赛前胜率和专家预测。团队赛没有预测，这个开关不起作用。
- 人设：`prompts.enable_personas` 关掉后，模型看不到人设。和预测都关时适合做公平对照；目录里的角色名带模型身份，要隐去时，在阵容里另写名字。
- 写作要求：`prompts.enable_writing_style` 开启时，`configs/writing_style.md` 的全文接在每位玩家系统提示的最后，约束所有输出的文风。
- 并发：`llm.max_concurrency`，1 为串行。
- 超时：`llm.timeout_seconds`，单次请求的秒数。
