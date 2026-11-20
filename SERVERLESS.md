# Telegram Serverless 部署

云端版在 `tgcloud/`；Windows Python 版保留。云端使用 Telegram 内置数据库，不会自动读取本地 `reviews.sqlite3`。如果已有待审数据，先处理完再切换。建议先在一只新 Bot 和测试群、测试频道上部署验证。

1. 在 [@BotFather](https://t.me/BotFather) 选择要部署的 Bot → **Serverless → CLI Access**。若没有该入口，先开通 Serverless 权限。CLI Access Token 与普通 Bot Token 不同；只在交互终端输入，不发到聊天或写进文件。
2. 编辑 `tgcloud/lib/config.js`：`owner` 是号主数字 ID；`reviewers` 是管理员数字 ID 数组；`groups` 是核验群 ID 数组；`targets` 是目标群或频道 ID 到入群问题的映射，例如 `{'-1001234567890': '为什么想加入？'}`。问题最多 1000 字。
3. 在本项目根目录运行。首次使用这只 Bot 时，确认状态中没有要保留的云端模块；若有，先核对变更，不要直接全量覆盖。

```powershell
npx --package @tgcloud/cli@0.2.0 tgcloud login
npx --package @tgcloud/cli@0.2.0 tgcloud fetch
npx --package @tgcloud/cli@0.2.0 tgcloud status
npx --package @tgcloud/cli@0.2.0 tgcloud push
npx --package @tgcloud/cli@0.2.0 tgcloud migrate --safe
npx --package @tgcloud/cli@0.2.0 tgcloud webhook
```

若 webhook 显示不同步，且确定要切换该 Bot 的更新入口，运行 `npx --package @tgcloud/cli@0.2.0 tgcloud webhook sync`。不丢弃待处理更新。不要同时运行本地轮询。

4. 给 Bot 授予各目标的邀请用户权限，管理员先私聊点击开始。在测试群和频道分别用需审批的邀请链接确认：无头像自动拒绝；有头像收到题目，回答后管理员能看共同群数并批准或带理由拒绝；非管理员无法审批。共同群仍只统计配置群。
