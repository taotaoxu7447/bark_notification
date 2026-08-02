# AgentWatch：AI 安装协议

本文档供 Codex、Claude Code、Kimi、Grok 等 AI 安装代理读取。人类用户也可以使用完全相同的命令。桌面端没有常驻 GUI；安装前必须先根据接收设备选择 Bark、AgentWatch 或两者共存。

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

重复执行安装是幂等操作：只修复同一个后台服务和运行文件，不创建第二个 watcher，不生成通知，也不改变已存在的账号绑定。

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

不得跳过这一步直接运行 `doctor`：`doctor` 只读，不会替用户启动或重启服务，也不会发送测试通知。

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

`doctor` 只做只读诊断，不会启动或重启后台服务，也不会发送通知。`bark`/`both` 必须先按上一步运行 `update`。若检测到旧版 `NTFY_URL` 或 `NTFY_TOKEN`，会报告 `legacy_ntfy_ignored=true`；v0.2 不会向旧共享 topic 双发。

Windows 任务计划程序显示 `Ready` 只表示任务已注册并等待触发，不等于 watcher 正在运行。以 `checks.service_running` 和私有运行日志判断实际状态；`doctor` 不会为了让检查通过而启动任务。

## 人工安装

人类用户可以省略 `--json --no-login`：

```bash
./install_launch_agent.zsh --delivery bark
# 或 --delivery agentwatch / --delivery both
```

或使用当前系统对应的安装脚本。用户同样先选择接收设备和投递模式。Bark-only 不应提示 AgentWatch 账号；AgentWatch 模式才使用账号和隐藏密码；`both` 分别配置，且 Bark 不等待 Android 登录。`bark`/`both` 用户私下保存 Bark secret 后必须运行 `agentwatch update`，再运行 `doctor --json`。任何安装路径都不自动发送测试通知。

## 统一命令

```text
agentwatch install    安装或幂等修复；使用 --delivery bark|agentwatch|both
agentwatch login      使用账号和隐藏密码绑定当前电脑
agentwatch status     查看安装、登录和后台服务状态
agentwatch doctor     只读诊断，支持 --json
agentwatch update     更新运行文件并按持久配置重新协调后台，保留 computer token
agentwatch logout     先在服务器撤销当前 token，再删除本机 token；已配置的 Bark 仍保持运行
agentwatch uninstall  删除后台服务和运行文件，默认保留凭据与状态
```

`logout` 在服务器成功撤销后才会删除本机 token；服务器返回 401 代表该 token 已经失效，也可以安全删除。网络或服务器失败时会保留本机 token，方便重试。在 `both` 模式下，退出 AgentWatch 只移除 Android 通道，已配置的 Bark 继续运行。对于已经丢失或无法操作的电脑，用户应在 Android App 的“设备”页面远程撤销。

## 凭据存储

- macOS：Keychain，service 为 `io.github.taotaoxu7447.agentwatch.computer`。
- Windows：当前 Windows 用户绑定的 DPAPI 加密文件。
- Linux：优先 Secret Service；不可用时使用权限严格为 `0600` 的本机私有文件。

稳定的 `computer_id`、电脑名称和平台不是密钥，保存在 `~/.codex-watch-notifier/machine.json`。账号密码、computer token、App token、邀请码均不得提交到 GitHub。

Bark URL/key 是 Bark 通道必须长期保存的个人密钥，应只存在于权限受限的本机配置或未来明确设计的安全存储中。它不能进入聊天或 argv；AgentWatch 密码则不落盘，只能由用户通过隐藏提示临时输入。
