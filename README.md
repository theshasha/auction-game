# 拍卖个人赛

让大模型参加唯一高价拍卖：秘密出价、公开发言，通过局末私有记忆调整下一局策略。总胜场决定排名，第一名并列时加赛，具体见 [比赛规则](RULES.md)。

## 先免费跑一场

在项目根目录使用 Python：

```bash
python -m pip install -r requirements.txt
python -m auction_game.cli.offline_demo
```

离线赛使用确定性假模型，走完整比赛流程，无需密钥，不调用真实服务。运行后打开新生成的 `logs/<场次目录>/README.md` 查看结果。

也可以运行 Bot 比赛：`python main.py --seed 42`。

## 使用真实模型

先按 [使用说明](docs/usage.md#准备配置) 配置模型与根 `.env`，再由用户亲自运行：

```bash
python main.py --ai --test-model
```

**这会调用真实服务并可能收费。** `--test-model` 让全员使用配置指定的测试模型，仍保留各自身份与独立历史。完整测试赛经检查确认后，再用 `python main.py --ai` 运行常规阵容。

## 文档入口

- [使用说明](docs/usage.md)：配置、运行、日志阅读与排查。
- [统计口径](docs/analyze.md)：分析场次与解释指标。
- [比赛规则](RULES.md)：由配置及 `RULES.template.md` 生成；修改规则正文请改模板并同步代码。
- [开发约定](AGENTS.md)：修改代码前的约束与查找入口。
