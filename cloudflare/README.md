# Cloudflare 切换检查点

当前目标：无 Python 的 Cloudflare 免费 Workers + SQLite Durable Objects，不使用容器。迁移前保持 Windows 版可用；不要提前修改正式 Bot 的 webhook。

`node cloudflare/inventory.mjs` 只读检查数据库，输出汇总数字，不导出用户资料、邀请链接或登录凭据。

切换需保留申请 ID、待答/待审状态、管理员审核卡片索引、专属邀请及其核销状态、自动取消置顶开关和近期置顶来源。号主会话通过云端专用登录迁移，Windows 的加密凭据及 owner.session 不上传、不提交。测试 Bot 验证真实号主账号能读取共同群，以及私聊审核、邀请仅限本人一次、手动置顶保护后，再停止本地轮询、导入最终数据并切换正式 webhook；保留本地数据库和可恢复旧轮询的入口。

Cloudflare 已完成 Wrangler OAuth 授权。验证 Worker 与正式 Bot 独立，不改正式 webhook。

`owner-probe.mjs` 是 Node.js / teleproto 的只读验证入口：核对登录账号，再对测试申请人分页查询共同聊天，只输出共同群数量。不创建或变更 Telegram Webhook。此探测不是审核 Bot，部署前仍需移植业务与持久化、在测试实例实测。teleproto 会话仅通过受控运行环境的 Secret 提供，不使用或上传现有 Windows 会话文件。

## 授权并验证云端共同群

先保持本地 Bot 运行。在 `cloudflare` 目录打开本机 PowerShell，运行：

```powershell
node --use-env-proxy setup-owner-probe.mjs
```

向导会创建独立的 Telegram 登录会话并直接写入测试 Worker 的 Cloudflare Secrets。不会读取 Windows 保存的凭据、打印会话、把会话写入文件或修改正式 Bot。请准备号主账号的 API ID / API hash、Telegram 登录验证码，以及一个已知共同群数量的测试账号公开用户名。若使用 Clash，输入实际 SOCKS5 / 混合端口，默认 7897。验证完可在 Telegram 设置 → 设备撤销这次新登录；在正式迁移方案确定前不要删除或替换本机原会话。

Windows 上向导会弹出可粘贴的 API ID / API hash 输入窗口。API hash 隐藏显示，凭据只回传给本机向导，不显示在 PowerShell。

向导会用 Windows DPAPI（当前 Windows 用户）逐步保存凭据、测试账号、预期数量和已验证的登录会话；验证码、两步验证密码和手机号不保存。中断后运行相同指令继续。需要重填测试参数时加 `--edit-test`，会话失效时加 `--relogin`，更换 API 凭据时加 `--reset`。

公开版本不包含 Cloudflare 子域名或实际 Bot 用户名。使用前复制 `deployment.example.json` 为 `deployment.local.json`，填写自己的 Worker 地址及测试、正式 Bot 用户名。若改变 Worker 名称，也要同步修改 `wrangler.jsonc` 的 name。部署配置和运行凭据不提交到 Git。
