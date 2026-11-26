# 使用说明

命令都在项目根目录运行。

## 准备配置

- `configs/game.yaml`：局数、金币、奖池、出价上限和超级出价参数。
- `configs/models.yaml`：模型目录。每个条目写调用的模型 ID、渠道、协议和 models.dev 来源；`name` 是参赛时默认的角色名。`price_multiplier` 是渠道实际收的美元 ÷ 官方美元价，不写为 1。渠道按人民币或"额度"收费时先换算成美元，例如官方 1 美元、渠道收 0.5 元、汇率 6.66，倍率就是 0.5 ÷ 6.66 ≈ 0.075。
- `configs/players.yaml`：个人赛阵容、人设和虚构的预测胜率。玩家写 `model`、`persona_title`、`persona_text`，开启预测时还要写 `predicted_win_rate`。`name` 不写时用模型目录里的角色名；同一个模型放多名玩家时，给每人写 `name`。
- `configs/teams.yaml`：团队赛阵容，正好 2 个阵营、每个 3 人。每个阵营写 `name`（阵营名，不能为空或重复）和 `players`；玩家的字段同 `players.yaml`，但没有预测胜率。
- `configs/config.yaml`：渠道、超时、并发上限、测试模型和提示开关。

玩家名字（包括取自模型目录的角色名）不能含空白和 `<`、`>`、`/`，不能是 `strategy`、`decision`、`speech`、`队内信件区`，同一场不能重名。

密钥写在根目录的 `.env` 里，变量名是渠道的 `api_key_env`，可以照 `.env.example` 写。渠道地址含账号标识时（如 Vertex 的项目号），渠道里用 `base_url_env` 代替 `base_url`，地址也写在 `.env` 里。

### 接入模型

在 `config.yaml` 的 `llm.channels` 里加渠道，再在 `models.yaml` 里用 `provider` 引用它。`protocol` 取 `chat`、`responses`、`anthropic` 或 `vertex`。OpenAI 协议的渠道地址保留 `/v1`；Anthropic 渠道的地址不带 `/v1`。

`vertex` 协议只支持 Vertex 快速模式的 API 密钥（`AQ.` 开头），渠道地址要写到区域为止，例如 `https://aiplatform.googleapis.com/v1/projects/项目号/locations/global`；不写项目和区域时，请求 Gemini 4 会返回 404。

推理档位：模型条目写了 `reasoning_effort: low | medium | high` 就用它；没写时，模型来源列出了 `high` 就用 `high`，否则用渠道默认档位。

新增模型、改了 `models_dev` 来源或要更新能力数据和官方单价时，从 models.dev 同步快照：

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

离线赛用按脚本回复的假模型，会演示流局、超级出价命中与落空、加赛和信件。`--test-model` 改用测试赛参数，`--output 目录` 指定输出位置。

**以下命令调用真实服务，可能收费：**

```bash
python main.py --test-model
python main.py
python main.py --team --test-model
python main.py --team
```

`--team` 跑团队赛，阵容读 `teams.yaml`。`--test-model` 让全员改用 `config.yaml` 里 `test_model` 指定的模型，角色名和人设不变；比赛参数改用 `game.yaml` 里 `test_model_overrides` 覆盖后的值，用来缩短局数和天数。

开赛时先打印阵容和预估花费，然后停在第一轮等回车。回车或按 `c` 放行后才建场次目录，放行前按 `q` 退出不留文件。不开赛也能查预估，不写任何文件：

```bash
python -m auction_game.cli.estimate [--team] [--test-model]
```

预估按 `logs/` 下以往场次里同一模型的实际用量算，没打过的模型给一个区间。同一渠道几场的实际花费差约 ±15%，渠道悄悄降级时可能差 2 倍以上，所以只看量级，不能当预算上限。

启动时会按 `game.yaml` 的生产参数更新 `rules/` 下的规则，个人赛是 `RULES.md`，团队赛是 `RULES_TEAM.md`；本场实际规则在场次目录的 `RULES.md`。改了规则模板、`game.yaml` 或阵容后，也可以手动生成或检查：

```bash
python -m auction_game.cli.rules          # 更新两份规则
python -m auction_game.cli.rules --check  # 过期或模板、配置有错时返回非零
```

## 交互控制

比赛默认单步：回车执行下一轮任务（如一轮出价或发言），`c` 切到连续模式，`s` 切回单步，`p` 暂停，`q` 退出。终端排名表的金币列是当天结算后、发次日工资前的余额。

模型回复格式不对或出价违规时，程序让它改一次，还不对就算这个任务失败。请求失败（服务报错、网络中断、输出被截断）时，程序等 10 秒重发，最多 3 次。

- 暂停或有任务失败时，进行中的请求跑完后停下等你确认。
- 回车或 `c` 继续，只补跑失败和没启动的任务。回车回到停下前的模式，`c` 切到连续；连续跑时想之后一步一步看，先按 `s` 再回车。
- `q` 马上取消已启动的任务并退出，本轮已成功的结果在续跑时沿用。被取消的请求可能已经收费。Ctrl+C、Ctrl+Break 和关闭输入都按 `q` 处理。
- 程序出错时，等进行中的请求结束后退出，打印报错和续跑命令。如果提示场次文件没写完，删掉那个场次目录重新开一场。
- 按键收到后会显示提示。按了没有提示，说明终端没把这次按键送到程序。

## 中断后续跑

比赛中途按 `q`、关掉窗口或程序崩溃后，可以接着跑完。**续跑同样调用真实服务，可能收费：**

```bash
python main.py --resume              # 最新一场没打完的真实比赛
python main.py --resume logs/<场次>  # 指定场次
```

已经成功的任务直接取用，不再请求模型。终端先显示从哪一轮恢复，回车后才继续，回车前按 `q` 退出不改任何文件。

- 比赛参数、阵容、模型和人设都沿用这一场开赛时的，不看当前配置；`--resume` 不能和 `--team`、`--test-model` 一起写，也不能换模型。
- 渠道地址、密钥、超时和并发取当前配置，渠道出问题时可以关掉比赛、换地址再续跑。花费仍按开赛时的单价和倍率算。
- 输出明显变短、纠错变多时，先查渠道，渠道可能悄悄降级了。
- 中途改过提示词或流程代码，这场就续不上，终端会说明是哪名玩家的哪一轮。
- 离线赛和已经打完的场次不能续跑。

## 阅读场次

每场写在 `logs/<时间>_<类型>/`，类型是个人赛的 `ai`、`ai-test`、`offline`，或团队赛的 `team`、`team-test`、`team-offline`。先打开里面的 `README.md`。

- `RULES.md`：本场规则。
- `speeches.md`：公开发言和观众总结。
- `letters.md`：私信，团队赛是队内信件区，按写信的环节分节。
- `matches.md`：逐局出价表和累计排名。
- `players/`：每名玩家的私人对话，每节是当次的输入和结果。局末整理记忆后，对话从系统提示和新记忆重新开始。补跑时模型看不到失败的那次。
- `data/run_config.json`：本场实际使用的模型、输出预算、推理档位、单价、价格倍率和提示开关。
- `data/matches/`：完整结算数据。
- `data/players/<编号>.jsonl`：任务记录，含模型原始回复、thinking 和完整错误。

### 对局回放

```bash
python -m auction_game.cli.replay                    # 最新的打完了的真实场次
python -m auction_game.cli.replay "logs/场次目录"   # 指定场次，离线赛要这样指定
```

在场次目录里生成 `replay.html`，双击用浏览器打开，可以按人、按轮次看策略、信、发言、记忆和每天的出价结果。只支持打完的场次。页面里没有 thinking、提示词原文和渠道名，可以发给别人看。

## 分析

```bash
python -m auction_game.cli.analyze                    # 最新的、至少打完一局的真实场次，含团队赛
python -m auction_game.cli.analyze "logs/场次目录"   # 指定场次，离线赛要这样指定
python -m auction_game.cli.team_report                                      # 团队赛两队对比，最新一场
python -m auction_game.cli.team_report "logs/场次目录" ["logs/场次目录" ...]   # 一场或多场
```

各项指标的口径见[统计说明](analyze.md)。

## 连通检查

**会调用真实服务，可能收费。** 每个模型最多请求 4 次加 `--concurrency` 次，都不重试。

```bash
python -m auction_game.cli.check_llm --test-model         # 只查测试模型
python -m auction_game.cli.check_llm --model 目录键       # 只查指定模型，可重复
python -m auction_game.cli.check_llm                      # 查 players.yaml 阵容用到的全部模型
```

每个模型一行，前一项失败时后面显示 `-`：

- 连通：`正常` 才能用。`提示丢失` 说明渠道丢掉了系统提示，模型看不到规则；`配置错误` 是 `.env` 缺密钥或地址。
- 耗时：第一次请求的耗时。
- 速度：让模型数到 200，每秒输出多少 token，接近比赛里实际等待的快慢。
- 并发：同时发 `--concurrency` 个请求（默认 3，设成阵容里同一模型的最多席位数）。`被限流` 或 `失败` 时比赛会频繁失败；`排队` 时比赛不会失败但会变慢，偶尔误报，可以再查一次。按分钟计的用量限额这里查不出来。
- 缓存：`没有` 时输入费用更高。Gemini 偶尔没命中，可以再查一次。

任何一列显示 `程序错误`，是本程序的问题，不是渠道的问题。表格下面的"要处理的"列出失败的模型和原因，并给出只重查这些模型的命令；"值得留意"列出不算失败的提醒。加 `--verbose` 显示渠道、预算、推理档位、回复和报错原文。`--max-tokens` 设探测的输出上限，默认 2048。

## 常用调整

- 预测：`config.yaml` 的 `prompts.enable_predictions` 关掉后，模型看不到赛前胜率和专家预测。团队赛没有预测。
- 人设：`prompts.enable_personas` 关掉后，模型看不到人设。和预测都关时适合做公平对照；目录里的角色名带模型身份，要隐去时，在阵容里另写名字。
- 写作要求：`prompts.enable_writing_style` 开启时，`configs/writing_style.md` 的全文接在每位玩家系统提示的最后。
- 并发：`llm.max_concurrency`，1 为串行。
- 超时：`llm.timeout_seconds`，单次请求的秒数。
