# AgentWatch：AI 安装协议

本文档供 Codex、Claude Code、Kimi、Grok 等 AI 安装代理读取。人类用户也可以使用完全相同的命令。桌面端没有常驻 GUI；安装完成后由用户在终端中安全登录，后台 watcher 随后自动运行。

## 不可违反的安全边界

- 不要向用户索取 AgentWatch 密码，也不要让用户把密码发到 AI 对话中。
- 不要使用、建议或实现 `--password`；该参数会被 CLI 主动拒绝。
- 不要把密码写入环境变量、`.env`、配置文件、shell 历史、日志或临时文件。
- 不要读取、打印或复制 computer token。登录后 CLI 自动把它写入系统凭据存储。
- 不要配置 topic、订阅 URL 或用户 ID。电脑只调用账号绑定的 `/publish` API，目标账号由服务器根据 computer token 决定。
- 安装、更新和登录都不会自动发送测试通知。不要为了“确认成功”反复发测试。

## AI 自动安装流程

### 1. 获取项目

```bash
git clone https://github.com/taotaoxu7447/bark_notification.git
cd bark_notification
```

已经存在仓库时，先检查当前改动，不要覆盖用户文件；在安全的情况下更新到用户指定的版本。

### 2. 非交互安装

macOS：

```bash
./install_launch_agent.zsh --json --no-login
```

Ubuntu：

```bash
chmod +x install_systemd_user.sh uninstall_systemd_user.sh
./install_systemd_user.sh --json --no-login
```

Windows PowerShell：

```powershell
.\install_task_scheduler.ps1 --json --no-login
```

成功结果包含：

```json
{"ok":true,"installed":true,"authenticated":false,"login_required":true}
```

重复执行安装是幂等操作：只修复同一个后台服务和运行文件，不创建第二个 watcher，不生成通知，也不改变已存在的账号绑定。

### 3. 暂停并交给用户登录

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

### 4. 用户确认登录完成后再验收

macOS / Ubuntu：

```bash
~/.local/bin/agentwatch doctor --json
```

Windows PowerShell：

```powershell
& "$env:USERPROFILE\.local\bin\agentwatch.cmd" doctor --json
```

只有以下关键检查为真，才可以向用户报告安装完成：

- `authenticated`
- `checks.runtime_files`
- `checks.service_installed`
- `checks.service_running`
- `checks.server_reachable`

`doctor` 只做只读诊断，不会发送通知。若检测到旧版 `NTFY_URL` 或 `NTFY_TOKEN`，会报告 `legacy_ntfy_ignored=true`；v0.2 不会向旧共享 topic 双发。

## 人工安装

人类用户可以省略 `--json --no-login`：

```bash
./install_launch_agent.zsh
```

或使用当前系统对应的安装脚本。交互式安装会在复制程序并配置后台服务后，直接提示账号和隐藏密码。登录成功前旧 watcher 会被停止，避免旧共享通道继续投递；登录成功后只启动一个后台服务。

## 统一命令

```text
agentwatch install    安装或幂等修复；人工模式会继续提示登录
agentwatch login      使用账号和隐藏密码绑定当前电脑
agentwatch status     查看安装、登录和后台服务状态
agentwatch doctor     只读诊断，支持 --json
agentwatch update     用当前下载包更新，保留 computer token
agentwatch logout     先在服务器撤销当前 token，再删除本机 token 并停止 watcher
agentwatch uninstall  删除后台服务和运行文件，默认保留凭据与状态
```

`logout` 在服务器成功撤销后才会删除本机 token；服务器返回 401 代表该 token 已经失效，也可以安全删除。网络或服务器失败时会保留本机 token，方便重试。对于已经丢失或无法操作的电脑，用户应在 Android App 的“设备”页面远程撤销。

## 凭据存储

- macOS：Keychain，service 为 `io.github.taotaoxu7447.agentwatch.computer`。
- Windows：当前 Windows 用户绑定的 DPAPI 加密文件。
- Linux：优先 Secret Service；不可用时使用权限严格为 `0600` 的本机私有文件。

稳定的 `computer_id`、电脑名称和平台不是密钥，保存在 `~/.codex-watch-notifier/machine.json`。账号密码、computer token、App token、邀请码均不得提交到 GitHub。
