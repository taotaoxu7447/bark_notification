# Codex 主会话提醒过滤设计

## 背景

GPT 5.6 可以在一个主任务中创建大量子智能体。每个子智能体都会写入独立的 Codex rollout 文件，并产生自己的 `task_complete` 或 `turn_aborted` 事件。当前 watcher 扫描所有 rollout 文件，因此会把子任务结束误当成用户需要关注的任务结束，造成连续推送。

实际会话记录提供了稳定的层级元数据：

- 主会话的 `session_meta.payload.thread_source` 为 `user`。
- 子智能体的 `session_meta.payload.thread_source` 为 `subagent`。
- 子智能体同时包含 `parent_thread_id`，嵌套子智能体仍保持 `subagent` 标记。

## 目标

默认只发送主会话中对用户有意义的提醒：最终完成、等待用户操作、失败或中止。子智能体的完成、失败和中止事件默认全部静默。

保留显式开关 `CODEX_WATCH_NOTIFY_SUBAGENTS=1`，供开发和排障时恢复子智能体提醒。

## 事件判定

watcher 读取每个 rollout 文件开头的第一条 `session_meta`，得到 `thread_source`、`parent_thread_id` 和会话标识。

处理 Codex 事件时采用以下规则：

1. `thread_source == "subagent"` 且未启用开关：跳过通知。
2. `thread_source == "subagent"` 且开关已启用：沿用现有通知逻辑。
3. `thread_source == "user"`：沿用现有 `task_complete`、`turn_aborted` 和额外事件处理逻辑。
4. 元数据缺失或来自旧格式：视为非子智能体，继续通知，避免漏掉主任务。

过滤发生在 Bark、ntfy、webhook、命令和本地通知发送之前，因此所有发送渠道以及 macOS、Ubuntu、Windows 的行为一致。ZCode 使用独立日志格式，不受本次变更影响。

## 状态与日志

被过滤事件仍然推进文件读取 offset，防止每轮扫描重复处理。它们不会写入 `sent` 去重集合，因为没有实际发送。

日志只在首次识别一个子智能体 rollout 时记录静默原因，避免日志被大量完成事件淹没。`--doctor` 显示当前子智能体提醒策略。

## 配置与兼容性

`CODEX_WATCH_NOTIFY_SUBAGENTS` 默认值为 `0`。只有 `1`、`true`、`yes`、`on` 等既有布尔配置支持的真值才启用子智能体提醒。

该设置加入 `env.example` 和中文 README。现有用户不修改配置即可获得安静模式；需要旧行为的用户可以显式开启。

## 测试

自动化测试至少覆盖：

- 主会话 `task_complete` 仍生成通知事件。
- 子智能体 `task_complete` 默认不生成通知事件。
- 子智能体 `turn_aborted` 默认不生成通知事件。
- 开启 `CODEX_WATCH_NOTIFY_SUBAGENTS` 后，子智能体事件恢复通知。
- 元数据缺失时继续通知。
- rollout 中后来追加其他 `session_meta` 时，以文件开头第一条元数据判定文件身份。

最后执行语法检查、完整测试、dry-run 和安装脚本验证，并重载本机 LaunchAgent。验证过程不发送真实 Bark 或 ntfy 测试消息，避免产生额外打扰。
