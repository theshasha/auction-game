# 拍卖个人赛

这是一个用 Python 编写的大模型拍卖游戏。第一名并列时需要加赛。项目只支持当前规则和当前场次格式，历史日志归档保留在 `logs/archive_20261016/`。

## 用户修改入口

- `configs/game.yaml` 设置局数、金币和奖池。生产默认值只在这里维护。
- `configs/models.yaml` 定义模型目录和角色名，明确指定协议，并记录模型在 models.dev 中的精确来源。
- `configs/players.yaml` 通过模型目录引用配置阵容，也保存人设和虚构预测胜率。玩家名单只在配置中维护；文档和测试使用实际配置或合成名单。
- `configs/config.yaml` 设置测试时替代实际阵容的模型、超时、渠道 URL 和密钥环境变量名。密钥只放在根目录的 `.env` 中。
- `RULES.template.md` 是规则正文的来源。修改规则时，同时维护规则文字和代码。

各比赛入口必须在创建日志或请求模型前同步根 `RULES.md`，同步失败就停止启动。根规则使用配置中的角色身份，本场规则使用实际参赛名单。手动生成规则使用 `python -m auction_game.game.rules`。

比赛启动时只离线读取模型能力快照。Bot 比赛无需密钥，连通检查也不依赖比赛参数。

## 按任务查代码

- 启动：根 `main.py` 只负责参数解析和模式分派。默认路径集中在 `auction_game/paths.py`，配置由 `auction_game/configuration.py` 解析，比赛由 `auction_game/runners.py` 组装。
- 游戏规则、结算、Bot 和比赛流程：查 `auction_game/game/`。
- 模型调用、提示、对话历史或发言：修改前读 `auction_game/ai/AGENTS.md`。
- 日志、请求捕获、回放或统计：修改前读 `auction_game/reporting/AGENTS.md`。统计口径见 `docs/analyze.md`。
- 用户命令、模型输出长度设置和比赛日志说明：查 `docs/usage.md`。独立工具位于 `auction_game/cli/`，用 `python -m` 运行。

## 暂停、补跑和退出

修改这些行为前，先读 `auction_game/controller.py` 和 `tests/app/test_controller.py`。需要保存和恢复状态的任务，必须通过 `RoundTask.participant` 传入提供 `checkpoint()` 和 `rollback()` 的对象。

- 暂停后停止启动新任务，先等已经启动的任务结束，并收集结果。保留成功结果及其状态，恢复时只补跑失败或尚未启动的任务。暂停期间，任务内部的重试仍可能继续。
- 暂停后即使整轮任务都已成功，也要等用户确认再提交整轮结果。用户退出时，先等已启动任务结束，再撤销整轮并恢复轮开始时的状态。
- 程序要先准备好接收失败后的确认，再公布失败消息，保证公告后立即按键也有效。每次出现新的失败，都需要用户重新确认，之前按过的回车或继续命令无效。

保留两种并发时序的回归测试：首日出价缓存已消费，但控制器尚未收集结果；控制器已收集部分成功结果，同时其他已启动任务也执行成功。两种情况都要验证恢复和退出时的状态处理。

## 费用边界

用户授权连通排查后，可以执行一次最小对照请求，每次最多调用一次模型，不自动重试。扩大排查范围前，仍须用户确认。

其他收费操作，包括测试赛、正式赛和重跑，都只提供命令，由用户亲自运行。`ai-test` 也会真实调用模型并可能收费。首次真实比赛先由用户运行全员使用测试模型的完整比赛，检查并确认后再用常规阵容。

## 离线验证

```bash
python -m pytest tests/ -q
python -m auction_game.game.rules --check
python -m pip check
git diff --check
```

完整假模型赛使用 `python -m auction_game.cli.offline_demo`。新场次统一写入 `logs/`，benchmark 不创建场次日志。

优先复用现有离线回归测试。公共测试辅助代码放在 `tests/support/`，测试文件之间不互相导入。
