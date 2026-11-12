# 唯一高价拍卖

这是一个用 Python 编写的大模型拍卖游戏，有个人赛和团队赛，第一名并列时加赛。项目只支持当前规则和当前日志格式，旧日志归档在 `logs/archive_*/`，`analyze` 不读这些归档。

## 编码原则

- 模型输出不可信，严格校验格式和出价规则。校验失败交给模型纠错，不为容错放宽解析。
- 用户手写的配置，只校验会导致误跑、收费或报错难懂的地方，其余交给 YAML、SDK 和 Python 自己报错。
- 程序自己写出的数据（场次快照、局日志、任务日志、能力快照）直接信任，读取时不校验。续跑截掉任务日志末尾的半行、比对请求文本，针对的是强关和两次运行之间的代码变动。
- 不为旧格式、旧配置或不存在的调用方保留兼容代码和兜底分支。
- 注释和文档写事实和不明显的原因，不写否定式保证，不复述修改历史。
- 测试验证行为，不复刻实现，不断言旧文案不存在。

## 配置和规则

- 生产默认值只在 `configs/game.yaml` 中维护，密钥只放在根目录的 `.env` 中。
- 玩家名单和分组只在 `configs/players.yaml`、`configs/teams.yaml` 中维护。
- 字段含义见 `docs/usage.md`。

规则正文以 `RULES.template.md` 为准，团队赛另有 `RULES_TEAM.template.md`；改规则时两份模板和代码一起改。根目录的 `RULES.md`、`RULES_TEAM.md` 由程序生成，不手改。

## 入口

`main.py` 只做参数解析和模式分派，比赛由 `auction_game/runners.py` 组装。独立工具在 `auction_game/cli/`，用 `python -m` 运行。

## 改动前先读

- 规则或经济参数：`docs/design/rules.md`。
- 整场流程、各阶段的轮次，暂停、补跑、退出和续跑：`auction_game/flow/AGENTS.md`。
- 模型调用、提示、对话历史和发言：`auction_game/agent/AGENTS.md`。
- 私信和信件格式：`docs/letters.md`。
- 团队赛：`docs/team-mode.md`。
- 人设：`docs/design/personas.md`。
- 日志和统计：`auction_game/reporting/AGENTS.md`，统计口径见 `docs/analyze.md`。
- 用户命令和日志说明：`docs/usage.md`。

以上改动都先看 `docs/design/rejected.md` 的相关条目。

## 费用边界

请求真实模型要收费。测试赛、正式赛、重跑和连通排查都只提供命令，由用户亲自运行；用户授权了的，在授权的范围内执行。

## 临时文件

分析某场比赛用的脚本和中间结果放在 `.tmp/<场次目录名>/`，例如 `.tmp/20261107_180624_team/`。结论只在回复里说，会改变某个决定的才写到那个决定所在的文档。分析结束后删掉这个目录。

## 离线验证

```bash
python -m pytest tests/ -q
python -m auction_game.cli.rules --check
python -m pip check
git diff --check
```

完整假模型赛使用 `python -m auction_game.cli.offline_demo`，团队赛加 `--team`，新场次写入 `logs/`。公共测试辅助代码放在 `tests/support/`，测试文件之间不互相导入。
