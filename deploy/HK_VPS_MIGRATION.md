# AgentWatch VPS 迁移运行手册

## SH VPS 迁移（v0.4.3）

AgentWatch 的 ntfy 和账号绑定服务已从 HK VPS 迁到 SH VPS：

```text
旧地址：https://191.222.219.94:9444
新地址：https://aw.taotaoxu.net
SH 地址：124.223.213.72
```

Android v0.4.3 会保留现有 username、topic 和全部 token，只在本地把旧
HTTPS/WSS 会话地址改写到新域名。桌面端执行 `agentwatch update` 时只改写
精确匹配项目旧默认值的 `AGENTWATCH_API_BASE`，自定义服务器地址保持不变。

服务端公开检查：

```bash
curl --fail --silent --show-error https://aw.taotaoxu.net/agentwatch/api/v1/health
curl --fail --silent --show-error https://aw.taotaoxu.net/v1/health
```

预期两个接口均返回 HTTP 200。`aw.taotaoxu.net` 必须解析到 SH VPS，并使用
正常域名证书和隐式 443 端口。旧 HK 地址在过渡期只用于兼容已有客户端。

## 历史：第一次 HK VPS 迁移（v0.4.2）

AgentWatch、ntfy、订阅站和相关代理服务从旧 HK VPS 迁移到替代服务器：

```text
旧服务器：64.90.8.184
新服务器：191.222.219.94
```

本文只记录公开地址和验证步骤。账号密码、邀请代码、computer token、App token、ntfy token、订阅私有路径和签名密钥不得写入仓库或命令日志。

## 客户端迁移

电脑端升级到 v0.4.2 后执行：

```bash
agentwatch update
agentwatch doctor --json
```

`update` 只会替换精确匹配旧项目默认值的 `AGENTWATCH_API_BASE`。自定义域名、自建服务器和其他 URL 不会被修改。

Android 端安装 `AgentWatch-android-v0.4.2.apk` 后必须至少打开一次 App。旧地址会话会被判定为待升级，并使用已保存的 app token 从新 API 换取新的 ntfy HTTPS/WSS 地址。确认首页显示“实时连接已建立”后才算完成。

## 服务端验收

在新服务器执行：

```bash
sudo /usr/local/bin/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl is-active caddy ntfy agentwatch-registration sing-box ds-dash
systemctl is-active hk-vps-healthcheck.timer hk-vps-cert-renew.timer hk-vps-daily-stats.timer
curl --fail --silent --show-error https://191.222.219.94:9444/agentwatch/api/v1/health
curl --fail --silent --show-error https://191.222.219.94:9444/v1/health
sudo -u ntfy -- python3 -I -c 'import sqlite3; db=sqlite3.connect("/var/lib/agentwatch-registration/registration.db"); print(db.execute("PRAGMA user_version").fetchone()[0]); print(db.execute("PRAGMA quick_check").fetchone()[0]); db.close()'
```

预期服务和 timer 均为 `active`，两个健康接口返回 HTTP 200，数据库 schema 为 `2`，`quick_check` 为 `ok`。匿名访问任意 ntfy topic 必须返回 403。

## 旧机过渡

旧机在观察期内只承担兼容职责：

- 9444 反向代理到新服务器，让尚未升级的电脑和 Android 会话继续工作。
- 9443 继续响应旧订阅 URL，但提供的 `clash.yaml` 必须与新机一致并指向新节点。
- 旧机 ntfy 和 AgentWatch 注册服务保持停止，避免双写数据库。
- 旧机 443 可短期保留为回滚节点，但不再由当前订阅首选。

## 数据与回滚

切换或重新部署前，使用 SQLite backup API 分别备份：

```text
/var/lib/agentwatch-registration/registration.db
/var/lib/ntfy/user.db
/var/cache/ntfy/cache.db
```

验证备份文件大小、SHA-256、表数量和 `PRAGMA quick_check`。程序、Caddy、ntfy、systemd 和 nftables 配置也要保留切换前副本。

需要回滚时，先恢复旧机 9444 本地反向代理与旧服务，再恢复对应数据库快照；不得让新旧注册服务同时接受写入。

## 下线标准

旧机至少保留 48 至 72 小时。只有以下条件全部满足后才能关闭：

1. 迁移代码已经合入远端 `main`，三平台电脑包和 Android APK 来自同一提交。
2. 电脑端 `doctor` 显示新 API、服务运行且账号认证有效。
3. Android 新版本已打开并建立 WebSocket，真实测试通知可收到。
4. 新服务器连续观察期内无健康检查、TLS、证书续期或消息投递异常。
5. 旧订阅 URL 已返回新节点配置，主要使用者已更新为新订阅地址。
6. 最终数据库备份已验证并保存在新机迁移备份目录之外的可靠位置。
