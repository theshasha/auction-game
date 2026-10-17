# 拍卖个人赛

Python 大模型拍卖游戏；第一名并列加赛。只支持当前规则与新场次格式，历史日志归档保留在 `logs/archive_20261016/`。

## 用户修改入口

- `configs/game.yaml`：局数、经济与奖池，生产默认值只维护在此。
- `configs/models.yaml`：模型目录、角色名、显式协议与 models.dev 精确来源。
- `configs/players.yaml`：阵容目录引用、人设与虚构预测胜率；名单只在配置维护，文档和测试使用实际配置或合成名单。
- `configs/config.yaml`：测试替身、超时、渠道 URL 与密钥环境变量名；密钥只放根 `.env`。
- `RULES.template.md`：规则正文源；规则文字与代码共同维护。

各比赛入口在创建日志或请求模型前同步根 `RULES.md`；失败即停止启动。根规则使用配置身份，场次使用实际名单。能力快照只离线读取，Bot 无需密钥；连通检查独立于比赛参数。

## 按任务查代码

- 启动与路径：根 `main.py` 保留参数解析及模式分派；`auction_game/paths.py` 集中默认路径，配置在 `configuration.py`，组装在 `runners.py`。
- 游戏规则、结算、Bot、比赛流程：`auction_game/game/`。
- 模型调用、提示、历史与发言：`auction_game/ai/`，修改前读该目录 `AGENTS.md`。
- 日志、捕获、回放与统计：`auction_game/reporting/`，修改前读该目录 `AGENTS.md`；统计口径见 `docs/analyze.md`。
- 用户命令、能力预算与产物阅读：`docs/usage.md`。独立工具在 `auction_game/cli/`，使用 `python -m`。

修改暂停或补跑先查 `auction_game/controller.py` 与 `tests/app/test_controller.py`。`RoundTask.participant` 显式接入检查点；暂停先排空在途任务，保留成功结果及状态，恢复只补跑失败或未启动项，整轮成功仍等确认；退出回滚整轮。保留“首日缓存已消费但结果未收集”和“已收集成功与在途成功共存”两种竞态回归。失败等待闸门在公告前建立，保证公告后立即确认有效，旧回车不放行新失败。

## 费用边界

用户授权连通排查后，可执行单次最小对照请求，每次至多一次调用且无自动重试；扩大批量范围须确认。其他收费操作，包括测试赛、正式赛与重跑，均给出命令由用户亲自运行。首次真实比赛由用户运行全测试模型完整赛，检查并确认后再用常规阵容。

## 离线验证

```bash
python -m pytest tests/ -q
python -m auction_game.game.rules --check
python -m pip check
git diff --check
```

完整假模型赛：`python -m auction_game.cli.offline_demo`，新场次统一进入 `logs/`，benchmark 不写场次。优先复用现有离线回归；公共测试辅助放 `tests/support/`，测试文件不互相导入。
