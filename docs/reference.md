# 配置与排查参考

日常运行见[使用说明](usage.md)。以下命令中的场次目录和角色名请替换为实际值。

## 模型接入

`configs/models.yaml` 的 `provider` 引用渠道，`protocol` 必填 `chat` / `responses` / `anthropic`。OpenAI URL 保留 `/v1`，Anthropic URL 由 SDK 追加 `/v1/messages`。参赛身份必须有角色名，测试替身可省略。

Chat/Responses 默认使用 `tool_choice: auto`；强制工具提交按模型目录中的设置启用，测试替身采用自身配置。

## 能力快照导入

已有 models.dev 数据文件时可离线更新：

```bash
python -m auction_game.cli.sync_models --input "path/to/api.json" --write
```

同步按 `configs/models.yaml` 的 models.dev 精确来源匹配。省略 `--write` 仅预览；写入时数据相同则保留原文件。预算与推理策略见[常用调整](usage.md#常用调整)。

## 连通检查参数

```bash
python -m auction_game.cli.check_llm --model "目录键" --max-tokens 2048
```

`--model` 可重复，和 `--test-model` 互斥；两者均省略时，探测阵容实际执行配置去重后的全部目标。每个目标至多一次流式工具请求，无重试，不创建场次。

默认预算 2048，`--max-tokens` 不得超过比赛预算；检查不依赖 `game.yaml`，也不修改比赛预算。真实探测可能收费，智能体执行的授权边界见根 [AGENTS.md](../AGENTS.md#费用边界)。

## 完整请求捕获与回放

offline 自动生成 `replay.md`。真实比赛默认关闭完整捕获，可通过 Python 调用 `auction_game.runners.run_ai(record_player=角色名)` 为指定玩家开启，CLI 暂无开关。捕获包含失败与纠错，写入 `data/replay/`；未提前开启的场次无法补出完整请求。

已捕获的真实场次首次导出：

```bash
python -m auction_game.reporting.replay "logs/场次目录" "角色名" --capture-definitions
```

这一步使用场次快照离线补充 SDK 工具定义，不调用真实服务；已有定义会复用。后续导出：

```bash
python -m auction_game.reporting.replay "logs/场次目录" "角色名"
python -m auction_game.reporting.replay "logs/场次目录" "角色名" --debug
```

默认展示开局、第一局第二天、首次跨局和终局等关键节点，并从实际日志选择流局滚存、超级出价命中或落空、加赛等事件。来源与审计链接在页尾；`--debug` 额外导出完整记录到 `debug/<玩家编号>.md`。

只查看工具定义无需开启完整捕获：场次 `tools.md` 对应 `data/tool_requests.jsonl` 的请求引用及 `data/tool_definitions.json` 的定义。

捕获不保存密钥或鉴权头，但包含私有内容，分享前请检查。

## 离线赛与运行环境

`offline_demo` 使用确定性假模型，失败先保存审计与所选玩家的完整捕获，再退出；失败场次可用上述命令离线导出回放。可选参数：

- `--test-model`：使用测试替身配置，仍完全离线。
- `--player "角色名"`：选择捕获与回放玩家，默认阵容第一位。
- `--output "全新目录"`：指定输出，已有目录不覆盖。

默认配置演示首日流局与超级出价落空、次日滚存及多人超级命中，正赛并列后通过一次加赛决出第一。报价按任务的实际局日生成，首日报价仍由规划缓存提供。至少四人、两天、两局正赛且允许加赛时启用这套脚本；经济参数还需足够支付报价与超级成本。其他配置回退为首位玩家按余额低价参与、其余零价的合法策略。所有策略与回复均为假模型生成，不产生真实服务请求。

`main.py` 成功或主动中止返回 0，已处理启动错误返回 1，参数错误返回 2。

从其他目录运行 `main.py` 可使用绝对路径；`python -m` 工具需项目位于模块搜索路径，例如在 Git Bash 中设置 `export PYTHONPATH='C:\project\auction-game'`。
