# 使用说明

命令都在项目根目录运行。

## 准备配置

- `configs/game.yaml`：局数、金币、奖池和超级出价参数。
- `configs/models.yaml`：模型目录。每个条目写调用的模型 ID、渠道、协议和 models.dev 来源；有 `name` 的条目可以参赛，`name` 就是角色名。
- `configs/players.yaml`：参赛阵容、人设和虚构的预测胜率。
- `configs/config.yaml`：渠道、超时、并发上限、测试模型和提示开关。

密钥写在根目录的 `.env` 里，变量名是渠道的 `api_key_env`，可以照 `.env.example` 写。

### 接入模型

在 `config.yaml` 的 `llm.channels` 里加渠道，再在 `models.yaml` 里用 `provider` 引用它。`protocol` 取 `chat`、`responses` 或 `anthropic`。OpenAI 协议的渠道地址保留 `/v1`；Anthropic 渠道的地址不带 `/v1`，SDK 会自己追加 `/v1/messages`。

每次请求允许的最大输出取自能力快照 `data/model_capabilities.json` 里该模型的最大输出；快照里没有时用 64000，启动时会显示预算提醒。推理档位按以下顺序确定：

1. 模型条目里写了 `reasoning_effort: low | medium | high`（只用于 chat 和 responses），就用它。
2. 否则 chat 和 responses 模型的来源列出了 `medium`，就用 `medium`。
3. 其余情况用渠道默认档位。

改了 `models_dev` 来源或要更新能力数据时，同步快照（免费，联网读取 models.dev）：

```bash
python -m auction_game.cli.sync_models                              # 预览差异
python -m auction_game.cli.sync_models --write                      # 写入快照
python -m auction_game.cli.sync_models --input "path/to/api.json" --write  # 用本地下载的 models.dev 数据
```

## 运行比赛

```bash
python -m auction_game.cli.offline_demo  # 假模型完整赛，无需密钥
python main.py --seed 42                # Bot 比赛，固定种子
python main.py --benchmark 50           # Bot 对照，两组各 50 场，不写日志
```

离线赛用按脚本回复的假模型，默认配置下会演示流局、超级出价命中与落空和加赛。`--test-model` 改用测试模型的配置，`--output 目录` 指定输出位置（目录须不存在或为空）。

benchmark 的第一组全是随机 Bot；第二组是一个策略玩家对五个随机 Bot，策略玩家每天出 `min(出价上限, 当前余额)`，不用超级出价。报告里策略玩家和随机对手的平均分都取自第二组。

**以下命令调用真实服务，可能收费：**

```bash
python main.py --ai --test-model
python main.py --ai
```

`--test-model` 让全员使用 `config.yaml` 里 `test_model` 指定的模型，角色名、人设和各自的对话历史不变。第一次真实比赛先完整跑一场测试模型赛，检查结果没问题后再跑常规阵容。

启动时会生成根目录的 `RULES.md`；生成失败就不启动。改了 `RULES.template.md` 或 `game.yaml` 后也可以手动生成或检查：

```bash
python -m auction_game.game.rules          # 更新 RULES.md
python -m auction_game.game.rules --check  # 过期或模板有错时返回非零
```

## 交互控制

AI 比赛默认单步：回车执行下一轮任务（如一轮出价或发言），`c` 切到连续模式，`s` 切回单步，`p` 暂停，`q` 退出。每天结算后终端显示赢家、超级命中和累计排名，金币列是当天结算后、发次日工资前的余额。

模型回复格式不对或出价违规时，程序会让它改一次；还不对，这个任务就算失败。可重试的网络或服务错误会自动重发一次。

- 暂停或有任务失败时，程序不再启动新任务，等已启动的任务结束后停下等你确认。
- 回车或 `c` 继续，只补跑失败和没启动的任务，成功的不重跑。每次出现新的失败都要重新确认。
- `q` 等已启动的任务结束后撤销本轮结果并退出，已写入的日志保留。

## 阅读场次

每场写在 `logs/<时间>_<ai|ai-test|bot|offline>/`，先打开里面的 `README.md`。

- `RULES.md`：本场规则。
- `speeches.md`：公开发言和观众总结。
- `matches/`：逐局结果。
- `players/`：每名玩家的私人对话。开头是系统提示，之后每节是当次新增的输入和最终结果；局末整理记忆后，对话从系统提示和新记忆重新开始。
- `data/run_config.json`：本场实际使用的模型、输出预算、推理档位和提示开关。
- `data/matches/`：完整结算数据。
- `data/players/<编号>.jsonl`：任务记录，含模型原始回复、thinking 和完整错误。

私人对话和 `data/players/` 里有玩家的私有策略、记忆和 thinking，分享前请检查。比赛进行中用只读方式查看日志，免得编辑器保存时覆盖程序正在追加的内容。

## 分析

```bash
python -m auction_game.reporting.analyze                    # 最新的 ai 或 ai-test 场次
python -m auction_game.reporting.analyze "logs/场次目录"   # 指定场次，Bot 和离线赛要这样指定
```

未完赛的场次只统计已经结束的局。各项指标的口径见[统计说明](analyze.md)。

## 连通检查

**会调用真实服务，可能收费。** 每个模型最多请求一次，不重试，不建场次，收到非空回复就算通过并显示回复内容。

```bash
python -m auction_game.cli.check_llm --test-model         # 只查测试模型
python -m auction_game.cli.check_llm --model 目录键       # 只查指定模型，可重复
python -m auction_game.cli.check_llm                      # 查常规阵容用到的全部模型
```

`--max-tokens` 设探测的输出上限，默认 2048，不超过该模型的比赛输出预算。

## 常用调整

改配置后下次启动生效。

- 提示开关：`config.yaml` 的 `prompts.enable_predictions` 关掉后，模型看不到赛前胜率和专家预测；`prompts.enable_personas` 关掉后，模型看不到人设。两项都关适合做公平对照，角色名仍带模型身份。
- 并发：`llm.max_concurrency`，1 为串行。渠道负载高时调低。
- 超时：`llm.timeout_seconds`，单次请求的秒数。
