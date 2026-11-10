# 唯一高价拍卖

这是一个用 Python 编写的大模型拍卖游戏，有个人赛和团队赛，第一名并列时加赛。项目只支持当前规则和当前日志格式，旧日志归档在 `logs/archive_*/`，`analyze` 不读这些归档。

## 编码原则

- 模型输出不可信，严格校验格式和出价规则。校验失败交给模型纠错，不为容错放宽解析。
- 用户手写的配置，只校验会导致误跑、收费或报错难懂的地方，其余交给 YAML、SDK 和 Python 自己报错。
- 程序自己写出的数据（场次快照、局日志、任务日志、能力快照）直接信任，读取时不校验。
- 不为旧格式、旧配置或不存在的调用方保留兼容代码和兜底分支。
- 注释和文档写事实和不明显的原因，不写否定式保证，不复述修改历史。
- 测试验证行为，不复刻实现，不断言旧文案不存在。约定只写原则，不把具体测试或实现细节写成约定。

## 配置和规则

- 生产默认值只在 `configs/game.yaml` 中维护，密钥只放在根目录的 `.env` 中。
- 玩家名单和分组只在 `configs/players.yaml`、`configs/teams.yaml` 中维护，文档和测试使用实际配置或合成名单。
- 字段含义见 `docs/usage.md`。

规则正文以 `RULES.template.md` 为准，团队赛另有 `RULES_TEAM.template.md`；改规则时两份模板和代码一起改。比赛入口在创建日志和请求模型前生成根目录的 `RULES.md`，团队赛是 `RULES_TEAM.md`。

## 入口

`main.py` 只做参数解析和模式分派，比赛由 `auction_game/runners.py` 组装。独立工具在 `auction_game/cli/`，用 `python -m` 运行。

## 改动前先读

- 规则或经济参数：`docs/design/rules.md`。
- 整场流程和各阶段的轮次：`auction_game/flow/AGENTS.md`。
- 模型调用、提示、对话历史和发言：`auction_game/agent/AGENTS.md`。
- 私信和信件格式：`docs/letters.md`。
- 团队赛：`docs/team-mode.md`。
- 人设：`docs/design/personas.md`。
- 日志和统计：`auction_game/reporting/AGENTS.md`，统计口径见 `docs/analyze.md`。
- 用户命令和日志说明：`docs/usage.md`。

以上改动都先看 `docs/design/rejected.md` 的相关条目，测试赛结论见 `docs/design/playtests.md`，每场的数字和依据在 `docs/design/playtests-log.md`，核对依据时再读。

## 暂停、补跑和退出

修改前先读 `auction_game/flow/controller.py` 和 `tests/flow/test_controller.py`。需要回滚的任务通过 `RoundTask.participant` 提供 `checkpoint()` 和 `rollback()`。

- 暂停后不再启动新任务，也不再重发失败的请求，等进行中的请求结束。恢复时只补跑失败或未启动的任务，成功的不重跑。
- 暂停过的轮次即使全部成功，也要用户确认后才提交。退出时马上取消已启动的任务，被取消的任务写"已取消"日志，再把整轮回滚到轮开始时的状态。
- 失败公告发出前就要能接收确认，公告后立即按键有效。每次新的失败都要重新确认。

## 费用边界

用户授权连通排查后，可以执行一次最小对照请求，每次最多调用一次模型，不自动重试。扩大排查范围前，仍须用户确认。

其他收费操作，包括测试赛、正式赛和重跑，都只提供命令，由用户亲自运行。首次真实比赛先由用户运行全员使用测试模型的完整比赛，个人赛和团队赛各算各的，检查确认后再用常规阵容。

## 临时文件

分析某场比赛用的脚本和中间结果放在 `.tmp/<场次目录名>/`，例如 `.tmp/20261107_180624_team/`。结论写进文档并提交后，删掉这个目录。

## 离线验证

```bash
python -m pytest tests/ -q
python -m auction_game.cli.rules --check
python -m pip check
git diff --check
```

完整假模型赛使用 `python -m auction_game.cli.offline_demo`，团队赛加 `--team`，新场次写入 `logs/`。公共测试辅助代码放在 `tests/support/`，测试文件之间不互相导入。
