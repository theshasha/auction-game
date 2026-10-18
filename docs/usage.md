# 使用说明

在项目根目录用现有 Python 安装依赖：`python -m pip install -r requirements.txt`。

## 准备配置

四份手工 YAML 在 `configs/`：`game.yaml` 改比赛参数；`models.yaml` 定义模型、角色名及协议；`players.yaml` 改阵容和人设；`config.yaml` 定义渠道及测试模型。密钥写根 `.env` 中配置指定的环境变量。

比赛启动自动更新根 `RULES.md`。改规则正文请编辑 `RULES.template.md` 并同步检查代码。

## 运行比赛

```bash
python -m auction_game.cli.offline_demo  # 免费离线完整赛，无需密钥或键盘推进
python main.py --seed 42                # Bot 比赛，固定种子
python main.py --benchmark 50           # 两组各 50 场，不写日志
```

benchmark 的策略组每天出 `min(出价上限, 当前余额)`，不使用超级出价，对比均值取第二组。离线赛参数见[参考](reference.md#离线赛与运行环境)。

以下命令调用真实服务并可能收费，由用户亲自运行：

```bash
python main.py --ai --test-model
python main.py --ai
```

先完成测试模型整场比赛，经检查确认后使用常规阵容。修改 `configs/config.yaml` 的 `test_model` 目录键即可切换测试模型；全员使用其执行配置，保留各自身份、人设及独立历史。实际调用设置见场次 `data/run_config.json`。

## 交互控制

回车推进一次出价或发言；`c` 连续；`s` 单步；`p` 暂停；`q` 中止。

- 暂停会等待在途任务结束，期间原有重试与纠错可能继续。成功结果保留，恢复只补失败或未启动项；整轮成功也需确认才提交。
- 失败后需重新按回车或 `c` 确认。回车恢复为单步，`c` 恢复为连续；提前按键无效。
- `q` 等待在途任务结束，再撤销本轮玩家状态并退出。

## 阅读场次

场次位于 `logs/<时间>_<ai|ai-test|bot|offline>/`，先打开其中的 `README.md` 查看结果与链接。`ai-test` 是真实收费比赛。

- `RULES.md`：本场规则；`speeches.md`：公开发言。
- `matches/`：逐局结果；`players/`：私人对话。
- `data/run_config.json`：实际执行配置；`data/matches/`：完整结算；`data/players/`：原始工具消息与错误。

比赛期间用只读预览查看日志，避免覆盖正在追加的内容。私人对话及原始数据含私有策略，分享前请检查。

玩家在局内保留对话，局间通过累计私有记忆延续经验：局末全部发言公布后整理笔记，最后一局跳过。对手只看到公开发言。私人日志的“输入原文”仅展示当次新增输入，完整请求需提前[开启捕获](reference.md#完整请求捕获与回放)。

## 分析与排查

```bash
python -m auction_game.reporting.analyze
python -m auction_game.reporting.analyze "logs/场次目录"
```

默认选最新有效 `ai` 或 `ai-test` 场次；Bot 和 offline 需显式传目录。命令中的场次目录请替换为实际路径，仅支持当前日志格式。统计口径见[统计说明](analyze.md)。

检查测试模型连通性（真实调用，可能收费，由用户亲自运行）：

```bash
python -m auction_game.cli.check_llm --test-model
```

每个目标请求一次，无自动重试；指定模型与探测预算见[连通检查参数](reference.md#连通检查参数)。

查工具定义打开场次 `tools.md`；查调用错误看 `data/players/<编号>.jsonl`，按 `task_id` 对照。需要完整消息历史时，见[捕获与回放](reference.md#完整请求捕获与回放)。

## 常用调整

配置修改均在下次启动生效。

- 并发：`configs/config.yaml` 的 `llm.max_concurrency`，正整数，设为 1 即串行。降低并发可缓解负载。
- 协议与渠道 URL：见[模型接入](reference.md#模型接入)。
- 推理档位：在模型目录设置 `reasoning_effort: low | medium | high`，适用于 Chat/Responses；删除后恢复能力快照策略。

能力快照 `data/model_capabilities.json` 在比赛启动时离线读取。免费同步公共能力数据：

```bash
python -m auction_game.cli.sync_models          # 联网预览
python -m auction_game.cli.sync_models --write  # 联网更新
```

输出预算采用有效最大输出，未知回退 **64000**；input/context 只作元数据，不裁历史或缩减输出，渠道拒绝或截断后直接报错。推理档位优先用显式配置，否则 Chat/Responses 在能力明确支持 medium 时自动采用，其余沿用渠道默认。实际值见场次 `data/run_config.json`；离线导入见[参考](reference.md#能力快照导入)。

改规则后可手动生成或检查：

```bash
python -m auction_game.game.rules          # 更新 RULES.md
python -m auction_game.game.rules --check  # 只读检查，过期或模板错误返回非零
```
