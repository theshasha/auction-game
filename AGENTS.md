# 拍卖个人赛

这是一个用 Python 编写的大模型拍卖游戏，第一名并列时加赛。项目只支持当前规则和当前日志格式，旧日志归档在 `logs/archive_*/`，`analyze` 不读这些归档。

## 编码原则

- 模型输出不可信，严格校验格式和出价规则。校验失败交给模型纠错，不为容错放宽解析。
- 用户手写的配置，只校验会导致误跑、收费或报错难懂的地方，其余交给 YAML、SDK 和 Python 自己报错。
- 程序自己写出的数据（场次快照、局日志、任务日志、能力快照）直接信任，读取时不校验。
- 不为旧格式、旧配置或不存在的调用方保留兼容代码和兜底分支。
- 注释和文档写事实和不明显的原因，不写否定式保证，不复述修改历史。
- 测试验证行为，不复刻实现，不断言旧文案不存在。约定只写原则，不把具体测试或实现细节写成约定。

## 配置和规则

- `configs/game.yaml`：局数、金币、奖池、出价上限和超级出价参数，以及 `--test-model` 时的覆盖参数。生产默认值只在这里维护。
- `configs/models.yaml`：模型目录、角色名、协议，以及模型在 models.dev 中的精确来源。
- `configs/players.yaml`：阵容、人设和虚构预测胜率。玩家名单只在配置中维护，文档和测试使用实际配置或合成名单。
- `configs/config.yaml`：渠道、超时、并发上限、测试模型和提示开关。密钥只放在根目录的 `.env` 中。
- `configs/writing_style.md`：全模型通用的写作要求，开关在 `config.yaml` 的 `prompts.enable_writing_style`。

规则正文以 `RULES.template.md` 为准，改规则时同步改代码。比赛入口在创建日志和请求模型前生成根 `RULES.md`。改规则或经济参数前，先读 `docs/rule-discussion.md`。

## 按任务查代码

- 启动：`main.py` 只做参数解析和模式分派。默认路径在 `auction_game/paths.py`，配置由 `auction_game/configuration.py` 解析，输出预算和推理档位由 `auction_game/model_capabilities.py` 从能力快照得出，比赛由 `auction_game/runners.py` 组装。
- 游戏规则、结算和赛程判断：`auction_game/game/`。
- 整场流程和各阶段的轮次：修改前读 `auction_game/flow/AGENTS.md`。
- 模型调用、提示、对话历史和发言：修改前读 `auction_game/agent/AGENTS.md`。
- 私信：修改前读 `docs/letters.md`。
- 日志和统计：修改前读 `auction_game/reporting/AGENTS.md`。统计口径见 `docs/analyze.md`。
- 用户命令和日志说明：`docs/usage.md`。独立工具在 `auction_game/cli/`，用 `python -m` 运行。

## 暂停、补跑和退出

修改前先读 `auction_game/flow/controller.py` 和 `tests/flow/test_controller.py`。需要回滚的任务通过 `RoundTask.participant` 提供 `checkpoint()` 和 `rollback()`。

- 暂停后不再启动新任务，也不再重发失败的请求，等进行中的请求结束。恢复时只补跑失败或未启动的任务，成功的不重跑。
- 暂停过的轮次即使全部成功，也要用户确认后才提交。退出时马上取消已启动的任务，被取消的任务写"已取消"日志，再把整轮回滚到轮开始时的状态。
- 失败公告发出前就要能接收确认，公告后立即按键有效。每次新的失败都要重新确认。

## 费用边界

用户授权连通排查后，可以执行一次最小对照请求，每次最多调用一次模型，不自动重试。扩大排查范围前，仍须用户确认。

其他收费操作，包括测试赛、正式赛和重跑，都只提供命令，由用户亲自运行。首次真实比赛先由用户运行全员使用测试模型的完整比赛，检查确认后再用常规阵容。

## 离线验证

```bash
python -m pytest tests/ -q
python -m auction_game.cli.rules --check
python -m pip check
git diff --check
```

完整假模型赛使用 `python -m auction_game.cli.offline_demo`，新场次写入 `logs/`。公共测试辅助代码放在 `tests/support/`，测试文件之间不互相导入。
