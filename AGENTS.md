# 拍卖个人赛

Python 大模型拍卖游戏；第一名并列加赛。只支持当前规则与新场次格式，历史日志归档保留在 `logs/archive_20261016/`。

## 用户修改入口

- `configs/game.yaml`：局数、经济与奖池，生产默认值只维护在此。
- `configs/models.yaml`：模型目录、角色名、显式协议与 models.dev 精确来源。
- `configs/players.yaml`：阵容目录引用、人设与虚构预测胜率；名单只在配置维护，文档和测试使用实际配置或合成名单。
- `configs/config.yaml`：测试替身、超时、渠道 URL 与密钥环境变量名；密钥只放根 `.env`。
- `RULES.template.md`：规则正文源；规则文字与代码共同维护。

AI、Bot、benchmark、offline 启动在创建日志或请求模型前自动同步根 `RULES.md`；根文档使用配置身份，场次使用实际名单。内容一致不重写，配置、模板或写入失败停止启动。Bot 无需密钥，能力快照只离线读取。手动预览：`python -m auction_game.game.rules`；`--check` 只读。连通检查独立于游戏参数，统计不修改历史。

## 按任务查代码

- 启动与路径：根 `main.py` 保留参数解析及模式分派；`auction_game/paths.py` 集中默认路径，配置在 `configuration.py`，组装在 `runners.py`。
- 游戏规则、结算、Bot、比赛流程：`auction_game/game/`。
- 自然语言提示：`auction_game/ai/prompts.py`；工具映射、description、schema 与共享纠错：`agent_tasks.py`；历史事务：`ai_player.py`。已完成任务仅回传提示与有效业务结果，原始 thinking 留审计。需继续的局末全员发言公布后独立 `save_memory`，日志成功才累计替换记忆并重置游戏上下文；下一局成绩由程序提供。观众总结单独读全场业务回顾。当场实际工具定义从场次 README 进入 `tools.md`，原始调用及错误查玩家 JSONL。
- 修改模型调用、历史、重试或协议兼容前，先读 `docs/pydantic-ai-integration.md`。通用执行器、模型工厂与发言编排在 `auction_game/ai/`。
- 修改暂停与补跑前查 `auction_game/controller.py`，保留两种暂停竞态回归。`RoundTask.participant` 显式接入检查点，已完成结果保留，仅补跑未完成部分。
- 日志布局、捕获与回放：`auction_game/reporting/`；统计口径先读 `docs/analyze.md`。
- 用户命令、能力预算与产物阅读：`docs/usage.md`。独立工具在 `auction_game/cli/`，使用 `python -m`，无旧脚本转发壳。

## 费用边界

用户授权连通排查后，可执行单次最小对照请求，每次至多一次调用且无自动重试；扩大批量范围须确认。其他收费操作，包括测试赛、正式赛与重跑，均给出命令由用户亲自运行。首次真实比赛由用户运行全测试模型完整赛，检查并确认后再用常规阵容。

比赛最大输出使用能力快照，未知回退 64000；自动推理仅在明确列出 medium 且协议适用时设置 medium，模型目录显式配置优先，详见使用说明。Hub Responses 的兼容 profile 保留，严格 schema 和一次共享纠错不变。

## 离线验证

```bash
python -m pytest tests/ -q
python -m auction_game.game.rules --check
python -m pip check
git diff --check
```

完整假模型赛：`python -m auction_game.cli.offline_demo`，所有新场次统一进入 `logs/`，benchmark 不写场次。优先复用现有完整赛和合成回归，保持严格校验、请求预算、隐私隔离与暂停行为。
