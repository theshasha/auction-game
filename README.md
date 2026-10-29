# 拍卖个人赛

让大模型参加拍卖的 Python 游戏。玩家每天秘密出价，排除撞价和被超级出价命中的报价后，最高的正常出价获胜。玩家还会公开发言，并在局末整理只有自己能看的记忆，带进下一局。规则见[比赛规则](RULES.md)。

## 先免费跑一场

```bash
python -m pip install -r requirements.txt
python -m auction_game.cli.offline_demo  # 假模型完整赛，无需密钥
```

运行后打开 `logs/<场次目录>/README.md` 查看结果。

## 使用真实模型

按[使用说明](docs/usage.md#准备配置)配置模型和 `.env` 密钥，先跑一场全员使用测试模型的完整比赛，确认结果后再跑常规阵容：

```bash
python main.py --test-model
python main.py
```

**这两条命令都会调用真实服务，可能收费。**

## 文档

- [使用说明](docs/usage.md)：配置、运行和控制比赛、阅读场次、分析和排查。
- [统计说明](docs/analyze.md)：分析命令各项指标的口径。
- [规则讨论备忘](docs/rule-discussion.md)：规则设计的取舍和调参时要看的数据。
- [开发约定](AGENTS.md)：改代码前要遵守的要求和代码入口。
