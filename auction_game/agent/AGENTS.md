# 玩家和模型调用

## 按任务查代码

- `auction_game/agent/`：提示文字在 `prompts.py`，文本格式与解析在 `text_output.py`，出价校验和 Agent 组装在 `tasks.py`，玩家对话历史在 `player.py`，任务日志在 `log.py`。
- `auction_game/llm/`：任务执行在 `execution.py`，协议和网络请求在 `client.py`。
- 发言、出价、记忆和规划的轮次以及私信送达在 `auction_game/flow/`，改之前读 `auction_game/flow/AGENTS.md`。
- 私信的规则、格式、送达和日志见 `docs/letters.md`，改私信前先读。

## 原则

- 模型直接回复文本，请求不带工具或 JSON schema。格式错误和出价违规共用一次纠错机会，仍失败就让任务失败，由控制器等用户确认补跑。
- 请求失败（HTTP 错误、网络错误、输出截断或流没有正常结束）后等 10 秒重发，最多重发 3 次；暂停后不再重发。程序错误原样抛出，不重发。
- 用户退出时，取消信号中断进行中的请求和重发等待，任务写"已取消"日志。
- 客户端在任务自己的事件循环中创建和关闭。
- 每个任务先写 JSONL，再写可读 Markdown，都写成功后才更新玩家状态；写入异常向上传递。
- `analyze` 读任务日志 JSONL 的 `task`、`context`、`messages`、`request_errors`，改这几个键时同步改 `auction_game/reporting/analyze.py`。
- `strategy` 和记忆只对玩家本人可见，私信只对收件人可见，对手只看到公开发言。观众总结不写回玩家历史。已完成任务的 thinking 只留在任务日志里，后续请求不带 thinking。
- 新记忆替换旧记忆和之前的对话，所以提示要让模型保留有效经验、改掉被推翻的判断，只写结论，不写例子和出价记录；事实由程序在下一局规划请求里补回之前各局的完整出价日志。
- 发言 300 字、私信 200 字左右、记忆 1000 字只写在提示里，程序不检查：检查会占用唯一一次纠错机会，失败还会让整场暂停。最终感言有意不设上限。
- 提示词直接影响比赛效果，改动后要由用户跑一场测试模型赛确认。

模型侧测试在 `tests/llm/` 和 `tests/agent/`，使用 FunctionModel 或 MockTransport 和假密钥。
