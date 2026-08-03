# AgentWatch：AI 安装协议

本文档供 Codex、Claude Code、Kimi、Grok、Pi Agent、OpenCode 等 AI 安装代理读取。人类用户也可以使用完全相同的命令。桌面端没有常驻 GUI；安装前必须先根据接收设备选择 Bark、AgentWatch 或两者共存。

## 第一步必须确定接收设备

AI 必须先询问用户，或从用户已经明确提供的信息中判断接收设备，然后映射为投递模式：

- 只有 iPhone / Apple Watch：`bark`。手机只安装 Bark，不安装、不注册、不登录 AgentWatch。
- 只有 Android / Android 穿戴设备通知转发：`agentwatch`。Android 安装自研 AgentWatch App 并注册或登录账号。
- 两类设备都要接收：`both`。两条通道独立共存；Android 登录尚未完成或暂时失效时，已经配置好的 Bark 仍须照常运行。

CLI 使用以下契约：

```text
agentwatch install --delivery bark
agentwatch install --delivery agentwatch
agentwatch install --delivery both
```

不存在 `configure-bark` 命令，不得编造。应使用当前包提供的安装入口，并保持上述模式语义。

## 不可违反的安全边界

- 不要向用户索取 AgentWatch 密码，也不要让用户把密码发到 AI 对话中。
- 不要使用、建议或实现 `--password`；该参数会被 CLI 主动拒绝。
- 不要把密码写入环境变量、`.env`、配置文件、shell 历史、日志或临时文件。
- Bark 首页个人推送地址包含 key，本身就是密钥。不要向用户索取，不要让它进入 AI 对话、argv、日志、Git 或 AI 可见的命令输出。
- Bark 模式仍需要把该个人推送地址或 key 安全配置到电脑；这只是 Bark 投递配置，不是 AgentWatch 账号配对。AI 可以准备权限受限的空白配置，但必须暂停，让用户本人把真实值写入持久的 `~/.codex-watch-notifier/env`，且不得随后读取或回显。当前 shell 的临时 `export` 不算后台配置，后台只以持久私有 `env` 为准。
- 不要读取、打印或复制 computer token。登录后 CLI 自动把它写入系统凭据存储。
- 不要配置 topic、订阅 URL 或用户 ID。电脑只调用账号绑定的 `/publish` API，目标账号由服务器根据 computer token 决定。
- 安装、更新、登录和 `doctor` 都不会自动发送测试通知。不要为了“确认成功”反复发测试。
- 不要自行覆盖或重写 `~/.claude/settings.json`。安装器会结构化、幂等地合并 AgentWatch 自己的 Claude `Stop` 和 `StopFailure` hooks，同时保留所有现有设置与 hooks；绝不能添加 `SubagentStop`。
- Claude hook 只能写入本机私有队列，不得在 Claude Code 进程里发 HTTP、Bark 或 WebSocket 请求。网络投递由已经安装的后台 watcher 统一完成。
- `CLAUDE_WATCH_EVENTS_FILE` 留空时使用 AgentWatch 的默认私有队列。自定义路径必须是 AgentWatch 注册的专用文件，文件和父目录均须由当前用户持有并保持私有；不得复用已有日志、配置、状态或任何其他应用的数据文件，也不得使用符号链接、junction 或目录。
- Claude Code 必须为 `2.1.196` 或更高版本。虽然官方 exec-form `args` 从 `2.1.139` 已可用，但本项目还依赖 `2.1.145` 加入的 `background_tasks` / `session_crons`，以及 `2.1.196` 加入、用于同一 prompt 主去重的 `prompt_id`。不要擅自替用户升级；若 `doctor` 报告版本不兼容，应说明最低版本并按用户授权处理。
- Pi Agent 必须为 `0.80.4` 或更高版本，以使用官方 `agent_settled`。OpenCode 必须为 `1.15.11` 或更高版本，以使用官方全局插件的 `session.idle` 和可等待的 `dispose` 生命周期。两种集成都只能原子写入本机私有事件队列，不能在 AI 工具进程中联网。
- 安装器只管理带 AgentWatch 标记的 Pi 扩展和 OpenCode 插件。发现同名外来文件、符号链接、非当前用户文件或损坏的注册记录时必须停止，不得覆盖；卸载也只删除已登记的 AgentWatch 文件。

## AI 自动安装流程

### 1. 获取项目

```bash
git clone https://github.com/taotaoxu7447/bark_notification.git
cd bark_notification
```

已经存在仓库时，先检查当前改动，不要覆盖用户文件；在安全的情况下更新到用户指定的版本。

### 2. 选择投递模式并非交互安装

先记录从接收设备得出的 `bark`、`agentwatch` 或 `both` 选择。统一入口是 `agentwatch install --delivery <mode>`；平台安装器使用相同参数，以不接触密钥的非交互方式完成文件和后台服务安装：

下面以 `both` 为例；实际执行时必须替换为第一步确定的模式。

macOS：

```bash
./install_launch_agent.zsh --delivery both --json --no-login
```

Ubuntu：

```bash
chmod +x install_systemd_user.sh uninstall_systemd_user.sh
./install_systemd_user.sh --delivery both --json --no-login
```

Windows PowerShell：

```powershell
.\install_task_scheduler.ps1 --delivery both --json --no-login
```

不要依赖一份固定 JSON 判断三种模式；关键差异如下。兼容字段 `authenticated` 仅表示 AgentWatch 是否已登录，不表示 Bark 是否可用。

| 状态 | 关键结果 |
| --- | --- |
| `bark` 的持久 Bark 配置已由 install/update 协调 | `authenticated=false`、`login_required=false`、`bark_configuration_required=false`、`operational=true`、`fully_configured=true`，后台服务运行 |
| `bark` 但 Bark 未配置 | `bark_configuration_required=true`、`operational=false`，后台服务停止并等待用户私下配置 |
| `agentwatch` 但无 computer token | `login_required=true`、`operational=false`，后台服务停止并等待用户隐藏登录 |
| `both` 仅 Bark 已配置并经 update 协调 | `authenticated=false`、`login_required=true`、`operational=true`、`degraded=true`，后台服务继续运行 Bark |

只有与所选模式对应的必需条件满足后，才把该通道报告为就绪；`both` 可以是 Bark 已运行、Android 待登录的降级状态。

重复执行安装是幂等操作：只修复同一个后台服务和运行文件，不创建第二个 watcher，不生成通知，也不改变已存在的账号绑定。每次安装或更新都会协调 Claude Code、Pi Agent 和 OpenCode 的官方事件集成：Claude 条目缺少时加入 AgentWatch 自己的 `Stop`、`StopFailure`，Pi/OpenCode 只写入各自全局扩展目录中的 AgentWatch 管理文件；已有内容不重复写，并保留所有外来配置。不会配置 `SubagentStop`。

### 3. 暂停并交给用户完成对应密钥步骤

#### `bark`

用户在 iPhone Bark 首页找到个人推送地址或 key，并亲自将 `BARK_URL` 或 `BARK_KEY` 写入电脑上权限受限的持久 `~/.codex-watch-notifier/env`。AI 不得让用户把真实值粘贴到聊天或命令参数，不得代填、读取或打印该值。只在当前 shell 临时 `export` 不会配置后台 watcher。Bark-only 用户不运行 `agentwatch login`。

#### `agentwatch`

当 `login_required` 为 `true` 时，AI 必须暂停自动化，让用户亲自在本机终端执行：

macOS / Ubuntu：

```bash
~/.local/bin/agentwatch login
```

Windows PowerShell：

```powershell
& "$env:USERPROFILE\.local\bin\agentwatch.cmd" login
```

CLI 会先读取账号，再用隐藏输入读取密码。密码只用于本次 HTTPS 登录，成功后立即丢弃；电脑只保留服务器签发的、只能向当前账号发布通知的 computer token。

#### `both`

分别执行上述 Bark 私密配置和 AgentWatch 隐藏登录，两者不能互相作为成功前提。用户暂时不完成 AgentWatch 登录时，应明确告知 Android 通道尚未启用，但不要停掉或判定 Bark 失败。

### 4. Bark 配置完成后由 AI 重新协调后台

`bark` 和 `both` 模式下，用户只需告诉 AI“已经保存”，绝不能把 secret 值发回对话。AI 随后运行 `update`，让 CLI 重新读取持久私有 `env`，并启动或重启后台服务：

macOS / Ubuntu：

```bash
~/.local/bin/agentwatch update
```

Windows PowerShell：

```powershell
& "$env:USERPROFILE\.local\bin\agentwatch.cmd" update
```

不得跳过这一步直接运行 `doctor`：`doctor` 只读，不会替用户启动或重启服务，也不会发送测试通知。`update` 还会重新执行幂等的 Claude、Pi 和 OpenCode 集成协调，但不会把协调过程当成一次任务事件。

### 5. 用户确认相关私密步骤完成后再验收

macOS / Ubuntu：

```bash
~/.local/bin/agentwatch doctor --json
```

Windows PowerShell：

```powershell
& "$env:USERPROFILE\.local\bin\agentwatch.cmd" doctor --json
```

通用检查应为真：

- `checks.runtime_files`
- `checks.service_installed`
- `checks.service_running`

模式相关检查：

- `bark`：检查 Bark 已配置，但不要读取或显示 URL/key；不要求 `authenticated`，也不要求 Android App。
- `agentwatch`：要求 `authenticated` 和 `checks.server_reachable`。
- `both`：分别报告两条通道；缺少 AgentWatch 登录时不得把已就绪的 Bark 报告为不可用。

`doctor` 只做只读诊断，不会启动或重启后台服务，也不会发送通知。`bark`/`both` 必须先按上一步运行 `update`。若检测到旧版 `NTFY_URL` 或 `NTFY_TOKEN`，会报告 `legacy_ntfy_ignored=true`；v0.4.0 不会向旧共享 topic 双发。

Windows 任务计划程序显示 `Ready` 只表示任务已注册并等待触发，不等于 watcher 正在运行。以 `checks.service_running` 和私有运行日志判断实际状态；`doctor` 不会为了让检查通过而启动任务。

## 人工安装

人类用户可以省略 `--json --no-login`：

```bash
./install_launch_agent.zsh --delivery bark
# 或 --delivery agentwatch / --delivery both
```

或使用当前系统对应的安装脚本。用户同样先选择接收设备和投递模式。Bark-only 不应提示 AgentWatch 账号；AgentWatch 模式才使用账号和隐藏密码；`both` 分别配置，且 Bark 不等待 Android 登录。`bark`/`both` 用户私下保存 Bark secret 后必须运行 `agentwatch update`，再运行 `doctor --json`。任何安装路径都会幂等协调 Claude、Pi 和 OpenCode 集成，但不会自动发送测试通知。

## 统一命令

```text
agentwatch install    安装或幂等修复；使用 --delivery bark|agentwatch|both；协调自身 AI 工具集成
agentwatch login      使用账号和隐藏密码绑定当前电脑
agentwatch status     查看安装、登录和后台服务状态
agentwatch doctor     只读诊断，支持 --json
agentwatch update     更新运行文件、幂等协调自身 AI 工具集成，并按持久配置重新协调后台，保留 computer token
agentwatch logout     先在服务器撤销当前 token，再删除本机 token；已配置的 Bark 仍保持运行
agentwatch uninstall  删除后台服务和运行文件，只移除自身已登记集成，默认保留凭据与状态
```

`logout` 在服务器成功撤销后才会删除本机 token；服务器返回 401 代表该 token 已经失效，也可以安全删除。网络或服务器失败时会保留本机 token，方便重试。在 `both` 模式下，退出 AgentWatch 只移除 Android 通道，已配置的 Bark 继续运行。对于已经丢失或无法操作的电脑，用户应在 Android App 的“设备”页面远程撤销。

卸载时只能从 Claude 的 `Stop`、`StopFailure` 数组中移除由 AgentWatch 管理的精确条目。不得删除整个事件数组、其他工具的 hooks、用户自定义 hooks 或 Claude settings 中的任何其他设置。若 settings 无法解析，必须先移除后台服务但保留 Hook 指向的运行时，报告 `claude_hook_cleanup_failed` 部分失败，修复 settings 后再重试；绝不能留下指向已删除程序的 Hook。

Pi/OpenCode 卸载同样只能删除注册记录指向、且带正确 AgentWatch 标记的管理文件。路径变化或文件被用户替换时必须报告 `tool_hook_cleanup_failed` 并保留运行时，不能猜测或扩大删除范围。

## Claude Code、Pi Agent、OpenCode 事件与显式测试

Claude Code 官方 hook 将 `Stop` 或 `StopFailure` 的 stdin JSON 交给本机处理器。处理器只校验并追加到权限受限的 `~/.codex-watch-notifier/claude-hook-events.jsonl`，随后立即成功退出；后台 watcher 才读取队列并访问外部通知通道。项目不使用 `SubagentStop`，因此 Claude 子智能体停止不会制造额外提醒。

Claude 官方会并行运行所有匹配的 hooks；一个 `stop_hook_active=false` 事件写入时，其他 project/plugin/session/managed Stop Hook 仍可能尚未返回阻止决定。后台 watcher 必须保留当前 offset，并默认等待 `CLAUDE_WATCH_STOP_SETTLE_SECONDS=10` 秒：等待期不访问网络、不建立或增加 delivery attempt，也不把私密消息复制进 state。每轮只读 lookahead 后续完整 JSONL；仅当找到同一 `session_id`、`prompt_id` 和 transcript 的有效 `stop_hook_active=true`（或同一 prompt 的有效 `StopFailure`）时，才抛弃 false 并继续处理终态。独立 `StopFailure` 和有效 true Stop 不等待。

不得把 transcript 文件增长单独当作“被其他 Hook 阻止”的证据。Claude 官方说明 transcript 异步落盘，普通最终 Stop 后也可能增长；增长只能作为匹配 true 记录存在时的旁证，当前通知文本使用 `last_assistant_message`。10 秒默认值优先保证提醒及时性，并不覆盖官方 prompt Hook 的 30 秒默认超时；配置会被限制在 5–600 秒。command/http/MCP Hook 官方默认可运行 600 秒，project/plugin/session 或自定义 Hook 也可能超过当前窗口，因此本机制是有边界的抑制而不是对 Claude 最终合并决定的绝对证明。需要降低延迟后误提醒或二次提醒的概率可设为 35 秒或更长，最严格可设为 600 秒，但会同样延迟普通通知；超出窗口的 blocker 仍可能导致暂定提醒先到。

安装器会用私有注册文件记住实际 Claude user-settings 路径。`CLAUDE_CONFIG_DIR` 改变后，必须执行 `agentwatch update` 完成旧 Hook 清理和新路径迁移；`status` / `doctor` 的 `needs_reconcile` 不得被忽略。静态诊断会检查 user settings、系统文件型 managed settings 及 `managed-settings.d`，但无法完整枚举 project、local、plugin、skill、agent、session、远程 managed policy 等运行时 scope。最终必须让用户在 Claude Code 内同时运行 `/status` 查看 `Setting sources`、运行 `/hooks` 查看实际 Hook；CLI 结果不能替代这两项运行时验证。

默认 Claude 队列位于 AgentWatch 的私有配置目录。若设置 `CLAUDE_WATCH_EVENTS_FILE`，安装器只能接受 AgentWatch 注册的专用私有路径；不得接管现有的任意数据文件，也不得允许链接、junction、目录、非当前用户所有或父目录权限开放的路径。该所有权记录应跨卸载保留，以便安全重装，而不能把“路径相同”泛化为可接管其他文件。

后台 watcher 第一次接管现有 Claude 队列时会基线到当前文件末尾，不回放旧记录。真实事件进入统一投递机制后，自动投递总计最多两轮；第一轮已经成功的通道会持久记录，第二轮只补失败通道，绝不能因为补投让同一台成功设备再次响铃。

Claude live 队列中的已消费数据采用双重低频留存边界：默认达到 4 MiB（`CLAUDE_WATCH_SPOOL_MAX_BYTES=4194304`）或 24 小时（`CLAUDE_WATCH_SPOOL_MAX_AGE_SECONDS=86400`）即触发安全轮转，配置最低值分别为 65536 字节和 3600 秒。容量是软上限；只要存在未读记录、active retry 或尚未排空的 drain，就必须继续保留，绝不能使用 read/check 后 truncate。watcher 通过 rename 保留旧 inode，继续读取并等待 30 秒写入安全窗后才清理，避免与 Claude Hook 并发 append 时丢事件。

Pi 使用 `agent_settled`，只接收持久、非 JSON 模式的稳定终态；默认不提醒带 `parentSession` 的 fork/clone。OpenCode 使用 `session.idle`，并通过 `Session.parentID` 排除子会话。二者的单事件文件写完整后才原子发布到私有队列；首次接管从 EOF 建立基线，失败事件在有界重试完成前阻止后续事件越序。

只有用户明确要求测试时，才可执行对应的一条 `--test-claude`、`--test-pi` 或 `--test-opencode`（或发布包 wrapper 的同名参数）。每个都是一次显式、单次测试，不得由 `install`、`update`、`login`、`doctor` 或安装验收流程自动调用，也不得为了“确认”循环执行。

## 凭据存储

- macOS：Keychain，service 为 `io.github.taotaoxu7447.agentwatch.computer`。
- Windows：当前 Windows 用户绑定的 DPAPI 加密文件。
- Linux：优先 Secret Service；不可用时使用权限严格为 `0600` 的本机私有文件。

稳定的 `computer_id`、电脑名称和平台不是密钥，保存在 `~/.codex-watch-notifier/machine.json`。账号密码、computer token、App token、邀请码均不得提交到 GitHub。

Bark URL/key 是 Bark 通道必须长期保存的个人密钥，应只存在于权限受限的本机配置或未来明确设计的安全存储中。它不能进入聊天或 argv；AgentWatch 密码则不落盘，只能由用户通过隐藏提示临时输入。
