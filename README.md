# 唯一高价拍卖

让大模型参加拍卖的 Python 游戏，有个人赛和两队各三人的团队赛。玩家每天秘密出价，排除和别人同价、被超级出价命中的正常出价后，最高的一笔获胜。玩家还会公开发言，个人赛互写私信，团队赛在队内信件区交流，并在局末整理只有自己能看的记忆，带进下一局。规则见[个人赛规则](RULES.md)和[团队赛规则](RULES_TEAM.md)。

## 先免费跑一场

```bash
python -m pip install -r requirements.txt
python -m auction_game.cli.offline_demo         # 假模型完整赛，无需密钥
python -m auction_game.cli.offline_demo --team  # 假模型团队赛
```

运行后打开 `logs/<场次目录>/README.md` 查看结果。

## 使用真实模型

按[使用说明](docs/usage.md#准备配置)配置模型和 `.env` 密钥，先跑一场全员使用测试模型的完整比赛，确认结果后再跑常规阵容：

```bash
python main.py --test-model
python main.py
```

团队赛加 `--team`，阵容在 `configs/teams.yaml`；第一次跑团队赛同样先用测试模型。比赛中途关掉后，用 `python main.py --resume` 接着跑，见[中断后续跑](docs/usage.md#中断后续跑)。

**这些命令都会调用真实服务，可能收费。**

## 文档

- [使用说明](docs/usage.md)：配置、运行和控制比赛、阅读场次、分析和排查。
- [统计说明](docs/analyze.md)：分析命令各项指标的口径。
- [信件](docs/letters.md)：私信和队内信件区共用的规则、格式、送达和日志。
- [团队赛](docs/team-mode.md)：团队赛和个人赛不同的赛制、规则、队内信件区和显示。
- [规则设计备忘](docs/design/rules.md)：规则和信件时序的取舍，以及调参时要看的数据。
- [未采用的方向](docs/design/rejected.md)：讨论过但没有采用的方案和原因。
- [测试赛结论](docs/design/playtests.md)：现行配置下仍成立的测试赛结论和待观察项。
- [测试赛记录](docs/design/playtests-log.md)：每场测试赛的数字和依据。
- [开发约定](AGENTS.md)：改代码前要遵守的要求和代码入口。
