# 智能体回声

![智能体回声封面](assets/cover-agent-watch.png)

![通知回路副封面](assets/cover-notification-loop.png)

夜深以后，终端仍亮着微弱的光。智能体在日志的缝隙里跋涉，把一个个未竟的任务推向完成；人可以暂时离席，去喝水、散步、睡一小会儿，不必守在光标旁等待最后一行回声。

于是这枚小小的信标被放进工作流：它不替你写代码，也不替你判断世界，只在该回来的时候轻轻敲一下随身的屏幕。电脑、手机、手表之间，形成一条安静的回路；任务落定，回声抵达。

```text
终端夜雨响微声
智能孤灯守远程
一缕回波穿腕上
醒来代码已新晴
```

这是一个面向 AI 编程助手的本地任务提醒器。它会在本机持续监听已支持工具的日志或会话文件，当 AI 任务完成、停止、需要人工处理或异常中断时，通过 Bark 或自研 Android 客户端 AgentWatch 推送到你的随身设备。

项目当前优先服务内部同事使用：先让大家在自己的 macOS、Ubuntu、Windows 工作环境里稳定收到提醒；如果某个同事使用的工具还没适配，可以按本文档添加 watcher 后直接提交到主分支。

## 已支持能力

- **通知通道**：iPhone / Apple Watch 使用 Bark；Android 使用项目自研 AgentWatch，通过自建 ntfy 的鉴权 WebSocket 实时接收。
- **Android 一次登录**：服务器地址、topic 和连接方式已内置，用户只需用邀请代码注册一次，后续自动连接。
- **Android 来源分组**：Codex、ZCode、Kimi Code、Grok Build 使用独立通知频道和定制图标，手机系统可分别管理。
- **已适配工具**：Codex App / Codex CLI、ZCode、Kimi Code、Grok Build。
- **主任务优先**：Codex、Kimi Code 和 Grok Build 默认过滤子智能体或子会话事件，只提醒主任务。
- **分组和标签**：Bark 使用 `group` 和 `icon` 区分工具；ntfy 使用 topic 和 tags 区分来源。
- **三端安装**：macOS LaunchAgent、Ubuntu systemd user service、Windows Task Scheduler。
- **诊断命令**：`--doctor` 检查配置、日志目录、状态文件、后台服务和隐私设置。
- **测试命令**：`--test`、`--test-zcode`、`--test-kimi`、`--test-grok` 分别测试四种工具的通知。
- **隐私开关**：可以关闭工作目录和消息摘要，避免把敏感内容推送到手机。
- **历史抑制**：首次启动会建立基线，默认不会把旧日志里的历史任务重新推送一遍。

代码里仍保留了通用 webhook、企业微信 webhook、命令行回调等预留入口；当前推荐路径是 iOS 使用 Bark、Android 使用 AgentWatch。飞书、个人微信、企业定制 IM 需要确认公司环境后再决定是否启用。

## 适用场景

这个工具适合下面这种工作流：

1. 在电脑上让 Codex、ZCode、Kimi Code、Grok Build 或其他 AI 编程助手跑长任务。
2. 人离开电脑，或切换去做别的事。
3. AI 任务完成、卡住或需要确认时，手机、手表或其他通知客户端设备收到提醒。
4. 回到对应电脑继续处理。

任务监听和判断都发生在你自己的电脑上，不需要上传代码。通知正文只经过你选择的 Bark 通道或项目自建的 ntfy 中继；自建 Android 路径不会把代码交给第三方任务处理服务。

## 随身设备准备

### iPhone / Apple Watch：Bark

1. 在 iPhone 安装 Bark。
2. 打开 Bark，允许通知权限。
3. 复制 Bark 首页显示的推送地址或 key。
4. 如果使用 Apple Watch，保持 iPhone 和 Apple Watch 的系统通知同步设置正常。Bark 通知会按 iOS / watchOS 的规则转发到手表。

配置时可以二选一：

```bash
BARK_URL=https://api.day.app/<your-key>
# 或
BARK_KEY=<your-key>
```

### Android / 手环：AgentWatch

1. 安装 GitHub Release 中的 `AgentWatch-android-<version>.apk`，打开后允许通知。
2. 新用户输入管理员私下提供的邀请代码、账号和至少 12 位密码，点“注册并连接”；已有账号直接登录。服务器地址和 WebSocket topic 已内置，不需要手工添加订阅。
3. 确认首页显示“已连接”，再按页面按钮把自启动和耗电管理设为“完全允许后台行为”。这些 Android 系统确认必须由用户亲自完成。
4. 如果需要手环或手表震动，在手机的穿戴设备 App 中允许转发 AgentWatch 通知；手环本身不需要安装 AgentWatch。
5. “端到端测试”只投递到发起测试的当前设备，不会让同 topic 的其他手机一起响；服务器只记录短期送达事件 ID，不保存通知正文。

如果手机以前还订阅了同一 topic 的官方 ntfy 客户端，请先停用旧订阅，否则两个不同 App 会各显示一条通知；单个 App 内部的去重无法替另一个 App 消除通知。

当前 `0.1.x` 使用一个受 ACL 保护的共享广播 topic：所有获邀账号都能收到该 topic 的完整通知正文。因此它只适合彼此完全互信、原本就应该共享这些任务提醒的团队。若不同用户必须隔离正文，应先升级为 per-user topic/ACL，再发放邀请代码。

电脑端仍需保留正式发送地址，并填入管理员私下提供的只写 publisher token：

```bash
NTFY_URL=https://64.90.8.184:9444/agent-watch
NTFY_TOKEN=<publisher-token>
NTFY_PRIORITY=default
NTFY_TAGS=computer,robot
```

正式 URL 和 topic 是项目公开元数据，因此会直接保存在本仓库；它们本身不是授权凭据。服务端默认拒绝匿名访问，发送账号只有该 topic 的写权限，订阅账号只有读权限。`NTFY_TOKEN`、账号密码和服务端认证数据库仍然是密钥，绝不能提交到 GitHub。

AgentWatch 的 Android 源码、构建和签名说明见 [`android/README.md`](android/README.md)，注册服务部署说明见 [`deploy/agentwatch-registration/README.md`](deploy/agentwatch-registration/README.md)。旧电脑安装不会被安装脚本覆盖私有 env，需要手动更新 `~/.codex-watch-notifier/env` 中的 `NTFY_URL` 和 `NTFY_TOKEN`。

不要把真实的 Bark URL、Bark key、ntfy token、账号密码或 webhook 地址提交到 GitHub。它们属于个人密钥。

## 新电脑安装

从 GitHub Release 下载与你系统对应的包：

- macOS：`codex-watch-notifier-macos-<version>.zip`
- Ubuntu：`codex-watch-notifier-ubuntu-<version>.tar.gz`
- Windows：`codex-watch-notifier-windows-<version>.zip`

也可以直接 clone 仓库后在仓库根目录执行下面的命令。

### macOS

```bash
./install_launch_agent.zsh
$EDITOR ~/.codex-watch-notifier/env
./install_launch_agent.zsh
./codex-watch-notifier.zsh --doctor
./codex-watch-notifier.zsh --test
./codex-watch-notifier.zsh --test-zcode
./codex-watch-notifier.zsh --test-kimi
./codex-watch-notifier.zsh --test-grok
```

第一次执行安装脚本会复制程序和生成配置文件。编辑 `~/.codex-watch-notifier/env` 填入 Bark 或 ntfy 配置后，再执行一次安装脚本来重载后台服务。

常用排查：

```bash
tail -f ~/.codex-watch-notifier/codex-watch-notifier.log
launchctl print gui/$(id -u)/com.xutao.codex-watch-notifier
```

### Ubuntu

```bash
chmod +x install_systemd_user.sh uninstall_systemd_user.sh
./install_systemd_user.sh
$EDITOR ~/.codex-watch-notifier/env
./install_systemd_user.sh
python3 ~/.codex-watch-notifier/bin/codex_watch_notifier.py --doctor
python3 ~/.codex-watch-notifier/bin/codex_watch_notifier.py --test
python3 ~/.codex-watch-notifier/bin/codex_watch_notifier.py --test-zcode
python3 ~/.codex-watch-notifier/bin/codex_watch_notifier.py --test-kimi
python3 ~/.codex-watch-notifier/bin/codex_watch_notifier.py --test-grok
```

常用排查：

```bash
systemctl --user status codex-watch-notifier
journalctl --user -u codex-watch-notifier -f
```

如果机器需要在没有桌面登录会话时继续运行，可以让管理员或本人启用 lingering：

```bash
loginctl enable-linger "$USER"
```

### Windows

在解压后的包目录里打开 PowerShell：

```powershell
.\install_task_scheduler.ps1
notepad $env:USERPROFILE\.codex-watch-notifier\env
.\install_task_scheduler.ps1
py -3 $env:USERPROFILE\.codex-watch-notifier\bin\codex_watch_notifier.py --doctor
py -3 $env:USERPROFILE\.codex-watch-notifier\bin\codex_watch_notifier.py --test
py -3 $env:USERPROFILE\.codex-watch-notifier\bin\codex_watch_notifier.py --test-zcode
py -3 $env:USERPROFILE\.codex-watch-notifier\bin\codex_watch_notifier.py --test-kimi
py -3 $env:USERPROFILE\.codex-watch-notifier\bin\codex_watch_notifier.py --test-grok
```

常用排查：

```powershell
Get-ScheduledTask -TaskName CodexWatchNotifier
Get-Content $env:USERPROFILE\.codex-watch-notifier\codex-watch-notifier.log -Wait
```

Windows 路径可以写在 env 文件里，例如：

```text
CODEX_WATCH_STATE=C:\Users\<name>\.codex-watch-notifier\state.json
```

## 配置说明

配置文件默认位于：

```text
~/.codex-watch-notifier/env
```

常用配置：

| 变量 | 说明 |
| --- | --- |
| `BARK_URL` | Bark 完整推送地址，例如 `https://api.day.app/<key>` |
| `BARK_KEY` | Bark key；和 `BARK_URL` 二选一 |
| `BARK_LEVEL` | Bark 通知级别，默认 `timeSensitive` |
| `CODEX_BARK_GROUP` | Codex 通知分组，默认 `Codex` |
| `CODEX_BARK_ICON` | Codex 通知图标 URL |
| `ZCODE_BARK_GROUP` | ZCode 通知分组，默认 `ZCode` |
| `ZCODE_BARK_ICON` | ZCode 通知图标 URL |
| `KIMI_BARK_GROUP` | Kimi Code 通知分组，默认 `Kimi Code` |
| `KIMI_BARK_ICON` | Kimi Code 通知图标 URL；默认使用仓库内的 Kimi 官方 App 图案适配版 |
| `GROK_BARK_GROUP` | Grok Build 通知分组，默认 `Grok Build` |
| `GROK_BARK_ICON` | Grok Build 通知图标 URL；默认使用仓库内的 Grok 官方 App 图案适配版 |
| `NTFY_URL` | ntfy 推送地址；项目默认是 `https://64.90.8.184:9444/agent-watch` |
| `NTFY_TOKEN` | ntfy 写入 token；由管理员私下发放，绝不能提交 |
| `NTFY_PRIORITY` | ntfy 优先级，默认 `default` |
| `NTFY_TAGS` | ntfy 标签，例如 `computer,robot` |
| `AGENT_WATCH_PUBLISHER_ID` | 可选的发送电脑稳定标识；留空时首次启动会在本机生成，不是密钥 |
| `CODEX_NTFY_URL` | Codex 专用 ntfy URL；留空则使用 `NTFY_URL` |
| `CODEX_NTFY_TAGS` | Codex 专用 ntfy 标签 |
| `ZCODE_NTFY_URL` | ZCode 专用 ntfy URL；留空则使用 `NTFY_URL` |
| `ZCODE_NTFY_TAGS` | ZCode 专用 ntfy 标签 |
| `KIMI_NTFY_URL` | Kimi Code 专用 ntfy URL；留空则使用 `NTFY_URL` |
| `KIMI_NTFY_TAGS` | Kimi Code 专用 ntfy 标签 |
| `GROK_NTFY_URL` | Grok Build 专用 ntfy URL；留空则使用 `NTFY_URL` |
| `GROK_NTFY_TAGS` | Grok Build 专用 ntfy 标签 |
| `CODEX_WATCH_POLL_INTERVAL` | 轮询间隔，默认 2 秒 |
| `NOTIFY_DELIVERY_MAX_ATTEMPTS` | 同一事件的自动投递总次数，默认且硬上限为 `2`；设为 `1` 可关闭自动补投 |
| `NOTIFY_DELIVERY_RETRY_DELAY_SECONDS` | 第二次投递前的等待时间，默认 `60` 秒、最低 `30` 秒 |
| `CODEX_WATCH_NOTIFY_SUBAGENTS` | 是否提醒 Codex 子智能体事件，默认 `0`，只提醒主会话 |
| `ZCODE_WATCH_ENABLED` | 是否启用 ZCode，默认 `1` |
| `ZCODE_WATCH_LOG_ROOT` | ZCode 日志目录，默认 `~/.zcode/cli/log` |
| `KIMI_WATCH_ENABLED` | 是否启用 Kimi Code，默认 `1` |
| `KIMI_WATCH_SESSIONS_ROOT` | Kimi Code 会话目录，默认 `~/.kimi-code/sessions` |
| `KIMI_WATCH_NOTIFY_SUBAGENTS` | 是否提醒 Kimi 子智能体，默认 `0` |
| `GROK_WATCH_ENABLED` | 是否启用 Grok Build，默认 `1` |
| `GROK_WATCH_SESSIONS_ROOT` | Grok Build 会话目录，默认 `~/.grok/sessions` |
| `GROK_WATCH_NOTIFY_SUBAGENTS` | 是否提醒 Grok 子会话，默认 `0` |
| `NOTIFY_INCLUDE_WORKSPACE` | 是否在通知里显示工作目录，默认 `1` |
| `NOTIFY_INCLUDE_MESSAGE` | 是否在通知里显示消息摘要，默认 `1` |
| `NOTIFY_BODY_MAX_CHARS` | 通知正文最大长度，默认 `1100` |

隐私更严格时可以这样设置：

```bash
NOTIFY_INCLUDE_WORKSPACE=0
NOTIFY_INCLUDE_MESSAGE=0
NOTIFY_BODY_MAX_CHARS=0
```

## 工作原理

程序主体是 `codex_watch_notifier.py`。

- Codex watcher 监听 `~/.codex/sessions` 下的 `rollout-*.jsonl`。
- ZCode watcher 监听 `~/.zcode/cli/log` 下的 `zcode-*.jsonl`。
- Kimi Code watcher 监听 `~/.kimi-code/sessions` 下主智能体的 `agents/main/wire.jsonl`，只在 `step.end` 且 `finishReason=end_turn` 时提醒，不把工具调用步骤当成完成。
- Grok Build watcher 监听 `~/.grok/sessions` 下的 `events.jsonl`，识别 `turn_ended` 的 `completed`、`error` 和 `cancelled` 结果。
- watcher 会记录每个文件已经处理到的位置，状态存在 `~/.codex-watch-notifier/state.json`。
- watcher 会对状态文件持有系统级单实例锁；旧服务未退出时，新实例会直接退出且不发送，避免两个进程各自重复投递。
- 第一次启动默认只建立基线，不回放旧历史。
- 每个事件最多自动投递两次；第一次完全失败后至少等待 30 秒再补投一次，计数会先写入状态文件，服务重启也不会重置。两次都失败时停止自动发送并在 `--doctor` 中留下记录，避免无限重试造成通知轰炸。
- ntfy 投递会携带稳定 `X-Sequence-ID` 和 `source_*` 标签。Android 即使收到发送端的第二次补投，也只会更新同一系统通知，不会再次响铃；断线续传重放的同秒副本也由持久事件键吸收。
- Bark 通知使用事件的稳定 ID；补投时复用同一 Bark `id`，让支持该能力的 Bark 客户端和服务端折叠或更新同一条通知。它是第二层防重复保护，不替代本地状态去重。
- Codex 5.6 创建的子智能体 rollout 默认静默，只提醒主会话最终完成、等待人工或中止；排障时可设置 `CODEX_WATCH_NOTIFY_SUBAGENTS=1` 恢复全部提醒。
- Kimi Code 的非 `main` agent 和带 `parent_session_id` 的 Grok 子会话也默认静默，可分别通过对应的 `*_NOTIFY_SUBAGENTS=1` 临时开启。
- 检测到完成、停止、等待人工或异常事件后，会组装统一通知，再交给 Bark、ntfy 或其他发送层。

如果你要让 AI 帮你维护这个项目，可以直接把本节和下一节给它看。核心约束是：不要提交个人密钥，不要默认回放旧历史，不要绕过现有 Bark / ntfy 发送层。

## 添加新的 AI 工具支持

当同事使用 Claude Code CLI、Codex CLI 的新日志格式、Trae、Cursor、VS Code Claude Code 插件或其他工具时，优先按 watcher 的方式接入。

### 先确认工具能不能被监听

先找这个工具在本机留下的稳定痕迹：

- 日志目录：例如 `~/.xxx/log`、`~/.config/<tool>`、工作区内 `.tool/`。
- 会话文件：JSONL、JSON、SQLite、纯文本日志都可以。
- 事件语义：任务完成、等待输入、失败、中断、取消。
- 工作区信息：能否拿到项目路径或会话标题。
- 稳定 ID：能否构造一个不会重复推送的事件 ID。

如果工具没有本地日志，可以考虑命令行 wrapper、shell hook、插件事件、扩展 API，但先不要引入复杂后台服务。

### 代码修改建议

优先只改这些位置：

- `codex_watch_notifier.py`：新增 watcher、解析函数、通知事件构造。
- `env.example`：新增该工具的开关、日志路径、Bark 分组、ntfy 标签和图标配置。
- `README.md`：补充用户安装和测试说明。
- `assets/`：新增该工具的图标，使用 raw GitHub URL 填入 env。
- `build_packages.zsh`：如果新增文件需要进入发布包，把它加入 `COMMON`。

推荐实现步骤：

1. 为新工具增加默认日志目录和 env 变量，例如 `CLAUDE_WATCH_ENABLED`、`CLAUDE_WATCH_LOG_ROOT`。
2. 写一个只负责发现文件的函数，不在里面解析业务事件。
3. 写一个解析单行或单条记录的函数，把工具私有格式转换成统一事件。
4. 复用现有 state 机制，只处理文件新增内容。
5. 为通知设置独立 `bark_group`、`bark_icon`、`ntfy_tags`；必要时设置独立 `ntfy_url`。
6. 给 CLI 增加测试参数，例如 `--test-claude`。
7. 扩展 `--doctor`，让它能检查新工具的日志目录和开关状态。
8. 更新 README，让同事知道如何启用、测试和关闭。

### 需要保持的行为

- 首次运行不推送旧历史。
- 只推送明确的完成、失败、等待人工或中断事件。
- 不把 API key、Bark key、ntfy token、账号密码或公司内部 token 写进仓库；项目正式 ntfy URL/topic 可以公开。
- 通知正文必须受 `NOTIFY_INCLUDE_WORKSPACE`、`NOTIFY_INCLUDE_MESSAGE`、`NOTIFY_BODY_MAX_CHARS` 控制。
- 新工具默认不要破坏 Codex、ZCode、Kimi Code 和 Grok Build 已有行为。
- Windows、Ubuntu、macOS 至少要能优雅地跳过不存在的日志目录。

### 提交前检查

```bash
python3 -m py_compile codex_watch_notifier.py
python3 codex_watch_notifier.py --doctor
python3 codex_watch_notifier.py --test
python3 codex_watch_notifier.py --test-zcode
python3 codex_watch_notifier.py --test-kimi
python3 codex_watch_notifier.py --test-grok
./build_packages.zsh internal-test
```

如果你新增了某个工具的测试命令，也要一并运行。

## 协作约定

内部使用阶段可以直接提交到主分支，但提交前请遵守：

- 小步提交，一次只适配一个工具或修一个明确问题。
- 提交信息说明用户可见变化，例如 `Add Claude Code CLI watcher`。
- 不提交 `~/.codex-watch-notifier/env`、个人 token、真实 Bark URL、ntfy 账号密码、认证数据库或公司内部 webhook。
- 修改三端安装脚本时，至少说明自己在哪个系统上验证过。
- 如果不确定日志格式是否稳定，在 README 里把该 watcher 标成实验支持。

适合 AI 继续开发的任务描述模板：

```text
请在这个仓库里为 <工具名> 添加 Bark / ntfy 任务提醒支持。
已知日志位置是 <路径>，完成事件长这样：<样例>。
要求复用 codex_watch_notifier.py 的状态和 Bark / ntfy 发送层，不提交密钥。
请更新 env.example、README.md，并运行 py_compile 和 build_packages.zsh。
```

## 构建发布包

在 macOS 仓库根目录执行：

```bash
./build_packages.zsh v0.1.0-internal
```

产物会输出到 `dist/`：

- `codex-watch-notifier-macos-v0.1.0-internal.zip`
- `codex-watch-notifier-ubuntu-v0.1.0-internal.tar.gz`
- `codex-watch-notifier-windows-v0.1.0-internal.zip`

Android 正式 APK 在 `android/` 内使用长期发布密钥单独构建：

```bash
cd android
./build_release.zsh
```

产物是 `android/app/build/outputs/apk/release/app-release.apk`。发布密钥和密码绝不能进入 Git；丢失密钥将导致以后无法覆盖升级已安装的 APK。

每次发布建议从同一个 git commit 构建三种电脑端安装包和 Android APK。

## 卸载

macOS：

```bash
./uninstall_launch_agent.zsh
```

Ubuntu：

```bash
./uninstall_systemd_user.sh
```

Windows：

```powershell
.\uninstall_task_scheduler.ps1
```

卸载脚本只处理后台服务和安装文件。个人 env、日志和 state 是否删除，请根据实际情况手动确认。

## 当前边界

- iPhone / Apple Watch 推荐 Bark；Android / 手环通知转发推荐 AgentWatch，底层使用项目自建 ntfy。
- 项目自建 ntfy 的正式 URL/topic 公开，但访问必须认证；publisher 与 subscriber 凭据严格分离。
- 当前正式 topic 是共享通知流；接入更多同事前，如果不应互相看到任务提醒，应为每位用户分配独立 topic 和最小权限 ACL。
- 飞书、个人微信、企业微信等还没有作为正式主线启用。
- Claude Code CLI、Trae、Cursor、VS Code Claude Code 插件等还需要同事提供本机日志样例后再适配。
- 这个工具只做本机监听和推送，不负责启动、控制或接管 AI 编程助手。
