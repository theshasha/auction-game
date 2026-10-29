# 使用说明

命令都在项目根目录运行。

## 准备配置

- `configs/game.yaml`：局数、金币、奖池、出价上限和超级出价参数。
- `configs/models.yaml`：模型目录。每个条目写调用的模型 ID、渠道、协议和 models.dev 来源；`name` 是参赛时默认的角色名。
- `configs/players.yaml`：参赛阵容、人设和虚构的预测胜率。玩家的 `name` 可以不写，不写时用模型目录里的角色名；同一个模型放多名玩家时，给每人写 `name`。名字不能含空白和 `<`、`>`、`/`，不能是 `strategy`、`decision`、`speech`，同一场不能重名。
- `configs/config.yaml`：渠道、超时、并发上限、测试模型和提示开关。

密钥写在根目录的 `.env` 里，变量名是渠道的 `api_key_env`，可以照 `.env.example` 写。

### 接入模型

在 `config.yaml` 的 `llm.channels` 里加渠道，再在 `models.yaml` 里用 `provider` 引用它。`protocol` 取 `chat`、`responses` 或 `anthropic`。OpenAI 协议的渠道地址保留 `/v1`；Anthropic 渠道的地址不带 `/v1`，SDK 会自己追加 `/v1/messages`。

每次请求允许的最大输出取自能力快照 `data/model_capabilities.json` 里该模型的最大输出；快照里没有时用 64000。推理档位按以下顺序确定：

1. 模型条目里写了 `reasoning_effort: low | medium | high`，就用它。
2. 否则模型的来源列出了 `medium`，就用 `medium`。
3. 其余情况用渠道默认档位。

新增模型、改了 `models_dev` 来源或要更新能力数据时，从 models.dev 同步快照：

```bash
python -m auction_game.cli.sync_models                              # 预览差异
python -m auction_game.cli.sync_models --write                      # 写入快照
python -m auction_game.cli.sync_models --input "path/to/api.json" --write  # 用本地下载的 models.dev 数据
```

## 运行比赛

```bash
python -m auction_game.cli.offline_demo  # 假模型完整赛，无需密钥
```

离线赛用按脚本回复的假模型，默认配置下会演示流局、超级出价命中与落空、加赛和私信；假玩家每次能写信时给配置里的下一位写一封。`--test-model` 改用测试模型的配置和测试赛参数，`--output 目录` 指定输出位置。

**以下命令调用真实服务，可能收费：**

```bash
python main.py --test-model
python main.py
```

`--test-model` 让全员使用 `config.yaml` 里 `test_model` 指定的模型，角色名、人设和各自的对话历史不变。比赛参数改用 `game.yaml` 里 `test_model_overrides` 覆盖后的值，用来缩短局数和天数；离线赛的 `--test-model` 同样生效。第一次真实比赛先完整跑一场测试模型赛，检查结果没问题后再跑常规阵容。

启动时会按 `game.yaml` 的生产参数生成根目录的 `RULES.md`，测试模式也一样；本场实际规则在场次目录的 `RULES.md`。改了 `RULES.template.md`、`game.yaml` 或阵容后也可以手动生成或检查：

```bash
python -m auction_game.game.rules          # 更新 RULES.md
python -m auction_game.game.rules --check  # 过期或模板有错时返回非零
```

## 交互控制

AI 比赛默认单步：回车执行下一轮任务（如一轮出价或发言），`c` 切到连续模式，`s` 切回单步，`p` 暂停，`q` 退出。每天结算后终端显示赢家、超级命中和累计排名，金币列是当天结算后、发次日工资前的余额。

模型回复格式不对或出价违规时，程序会让它改一次；还不对，这个任务就算失败。请求失败（服务报错、网络中断、输出被截断）时，程序等 10 秒重发，最多重发 3 次。

- 暂停后，程序不再启动新任务，也不再重发失败的请求，等进行中的请求结束后停下等你确认。
- 有任务失败时，本轮其余任务照常跑完，然后停下等你确认。
- 回车或 `c` 继续，只补跑失败和没启动的任务，成功的不重跑。每次出现新的失败都要重新确认。
- `q` 马上取消已启动的任务，撤销本轮结果并退出，已写入的日志保留。被取消的请求可能已经收费。
- `c`、`s` 和第一次按的 `p`、`q` 收到后会显示提示；认不出的输入会原样显示，包括终端混入的控制字符。按了没有提示，说明终端没把这次按键送到程序。

## 阅读场次

每场写在 `logs/<时间>_<ai|ai-test|offline>/`，先打开里面的 `README.md`。

- `RULES.md`：本场规则。
- `speeches.md`：公开发言和观众总结。
- `letters.md`：私信，按写信的环节分节，有人写信的环节结束后追加。
- `matches.md`：逐局出价表和累计排名，每局结束时追加。
- `players/`：每名玩家的私人对话。开头是系统提示，之后每节是当次新增的输入和最终结果，收到的私信在输入里，写出的私信在结果的"私信"一节；局末整理记忆后，对话从系统提示和新记忆重新开始。补跑用同一份输入重新请求，看不到失败的那次；按 `q` 时进行中的任务记为"已取消"。
- `data/run_config.json`：本场实际使用的模型、输出预算、推理档位和提示开关。
- `data/matches/`：完整结算数据。
- `data/players/<编号>.jsonl`：任务记录，含模型原始回复、thinking 和完整错误。

## 分析

```bash
python -m auction_game.reporting.analyze                    # 最新的 ai 或 ai-test 场次
python -m auction_game.reporting.analyze "logs/场次目录"   # 指定场次，离线赛要这样指定
```

各项指标的口径见[统计说明](analyze.md)。

## 连通检查

**会调用真实服务，可能收费。** 每个模型最多请求一次，不重试，收到非空回复就算通过并显示回复开头。

```bash
python -m auction_game.cli.check_llm --test-model         # 只查测试模型
python -m auction_game.cli.check_llm --model 目录键       # 只查指定模型，可重复
python -m auction_game.cli.check_llm                      # 查常规阵容用到的全部模型
```

`--max-tokens` 设探测的输出上限，默认 2048，不超过该模型的比赛输出预算。

## 常用调整

- 提示开关：`config.yaml` 的 `prompts.enable_predictions` 关掉后，模型看不到赛前胜率和专家预测；`prompts.enable_personas` 关掉后，模型看不到人设。两项都关适合做公平对照。目录里的角色名带模型身份；要隐去时，在阵容里另写名字。`prompts.enable_writing_style` 开启时，`configs/writing_style.md` 的全文接在每位玩家系统提示的最后，约束所有输出的文风；文件缺失时启动报配置错误。
- 并发：`llm.max_concurrency`，1 为串行。
- 超时：`llm.timeout_seconds`，单次请求的秒数。
