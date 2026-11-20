# Telegram 入群審核 Bot

讓申請人先私訊 Bot，再由管理員決定是否放行。支援 Telegram 群組、超級群組與頻道；每個審核目標可設定不同的問題。Bot 不會掃描已加入的成員。

## 在 Windows 啟動

1. 安裝 Python 3.10 以上版本，並透過 [@BotFather](https://t.me/BotFather) 建立 Bot。
2. 把 Bot 加入各審核目標，設為管理員並授予「邀請使用者」權限。審核管理員先私訊 Bot 並按「開始」，才能收到審核卡片。
3. 將 `config.example.json` 複製為 `config.json`，填入以下設定：
   - `owner_id`：用來比對共同群組的真人帳號 ID。
   - `reviewer_ids`：審核管理員的 Telegram ID。審核時，Bot 仍會確認操作者是目標的擁有者，或是有邀請使用者權限的管理員。
   - `targets`：填入群組或頻道的完整數字 ID 與各自的審核問題。超級群組和頻道的 ID 通常以 `-100` 開頭。
   - `admission_mode`：設為 `private_invite`，啟用先私訊審核、通過後才發邀請連結的流程。
4. 在專案資料夾執行 `python -m pip install -r requirements-owner.txt`。雙擊 `打开控制台.cmd` 開啟 Windows 控制台。在「審核設定」確認資料後按「啟動 Bot」；啟動進度與錯誤會顯示在「執行記錄」頁。首次登入或工作階段失效時，改按「登入 / 配置憑據」。

也可以在 PowerShell 執行 `./启动.ps1`。同一個 Bot Token 只能由一個執行個體接收更新；切到 Cloudflare Webhook 後，不要同時執行 Windows 輪詢。

> **對外只提供 Bot 的私訊入口。** 如果群組或頻道仍有公開加入入口、可直接加入的舊連結，或管理員手動放行，申請人就可能略過審核。要讓所有人都經過 Bot 審核，請先關閉其他加入方式。

## 共同群組檢查

預設使用真人帳號查詢共同群組：將 `common_group_mode` 設為 `owner_account`、`min_common_groups` 設為 `8`，`common_group_ids` 可留空。Bot 只計算群組及超級群組，不計廣播頻道。

1. 用真人帳號登入 [Telegram API development tools](https://my.telegram.org/apps)，取得 **API ID** 與 **API hash**。這兩項不是 Bot Token。
2. 第一次設定時，依提示輸入 Bot Token、API ID 與 API hash，也可依提示複製後按 Enter 讀取剪貼簿。再以含國碼的手機號碼登入真人帳號，輸入 Telegram 驗證碼；有開啟兩步驟驗證時，也要輸入該密碼。登入的 Telegram ID 必須與 `owner_id` 相同。
3. Windows 會加密保存 Bot Token 與 API hash，後續可重複使用。登入工作階段保存在本機 `owner.session`。如需撤銷，請到 Telegram「設定 → 裝置」終止對應的工作階段。

更換 Bot Token 時執行 `./启动.ps1 -ReplaceToken`；更換 API 憑證時執行 `./启动.ps1 -ReplaceApi`。若直連 Telegram 逾時，可在 `config.json` 設定 `telegram_http_proxy`，例如 `"http://127.0.0.1:7897"`（請以代理軟體的 HTTP 或混合連接埠為準）。

若不想登入真人帳號，可改用 `configured` 模式，在 `common_group_ids` 填入至少 8 個不同的待核對群組 ID。Bot 必須是這些群組的管理員，而且只能統計清單內的共同群組。

## 申請與審核

1. **申請人先私訊。** 按「開始」或輸入 `/apply`，再選擇要加入的群組或頻道。已加入者不必重新審核。
2. **Bot 先檢查頭像和共同群組。** 確認沒有頭像或共同群組不足，就告知拒絕原因；符合條件才發題。確認帳號已註銷時會直接拒絕，不傳送訊息。暫時無法查詢時，申請會保留，不會當作 0 個共同群組；申請人可用 `/pending` 重試。若真人帳號無法辨識申請人，申請人可先私訊真人帳號再試。
3. **管理員在同一張卡片處理。** 申請人直接回覆題目後，管理員可按「通過並發送專屬連結」或「拒絕」。拒絕時可以只送出結果，也可選擇回覆原卡片並附上原因。卡片附有申請人的資料連結；若對方沒有公開使用者名稱，能否開啟資料取決於隱私設定。漏收卡片時，請私訊 Bot 輸入 `/reviews`。
4. **專屬邀請不限時間，只能使用一次。** Telegram 收到加入請求後，Bot 只批准該申請帳號。成功加入後，Bot 會立即鎖定邀請並要求 Telegram 撤銷連結；撤銷失敗會重試，不會再次放行。申請人可用 `/pending` 找回尚未使用的連結，被拒絕後也能重新申請。

舊制的待審記錄不會自動轉為私訊申請，請讓申請人重新私訊 Bot。如果 Telegram 仍顯示有一筆更早的待審請求，請管理員先清除舊請求。

## 保留手動置頂

在頻道的連結討論群組，把 Bot 設為管理員並授予「置頂訊息」權限。有置頂權限的管理員可用個人身分執行：

| 指令 | 用途 |
| --- | --- |
| `/autounpin on`、`/autounpin off` | 開啟或關閉自動取消頻道貼文置頂。設定依群組保存，重啟後仍有效。 |
| `/unpin` | 回覆一則訊息，嘗試取消該則自動置頂；未回覆時，只檢查最近一則置頂。 |
| `/unpinall` | 清除群組中所有能確認的自動置頂。 |
| `/unpinold` | 每次最多清除 20 則能確認的舊自動置頂。 |

手動置頂與來源不明的置頂會保留。清除舊置頂時，Bot 只處理自己曾看到、且能透過真人帳號查閱近期群組操作記錄確認的貼文。取消置頂不會刪除原始貼文；成功時不另外發送確認訊息。若 Bot 有「刪除訊息」權限，也會刪除已成功執行的管理指令訊息。

## Cloudflare 與資料保護

免費 Workers 的測試、部署與切換步驟見 [Cloudflare 部署說明](cloudflare/README.md)。Windows 版的申請與審核記錄存放於本機 `reviews.sqlite3`，執行記錄可在控制台查看，也會寫入 `bot.log`。

不要提交 `config.json`、本機部署設定、Token、API hash、驗證碼、登入工作階段、審核資料庫或記錄檔。公開版只提供範例設定。
