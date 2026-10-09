# 龍豐 Pokémon 卡牌免費監察

Python scraper + GitHub Actions，每 5 分鐘排程，新發現產品／售完轉有貨／HKD 價格變動，以 GitHub Issue Email 通知。第一次檢查建立安靜基準；冇變化唔發通知。

使用前請按以下步驟確認 GitHub Actions、Issues 同 Email 通知設定。唔需要提供郵件密碼或建立外部郵件服務。

## 成本同通知

- **只用 public repository + 標準 `ubuntu-latest` runner**。程式同 workflow 都拒絕 private repository。
- 冇付費服務、larger runner、Actions artifacts、cache、信用卡設定或 SMTP 服務。
- 使用 GitHub 內置暫時性 `GITHUB_TOKEN`；唔需要手動加入 secrets 或郵件認證資料。
- Watch 呢個 repository 嘅 **Issues**，並喺 GitHub 網頁嘅 notifications 設定啟用 **Email**。通知去 GitHub 已驗證嘅 email；唔改手機設定。GitHub 原生 Email 通知係通知渠道，Issue 成功建立唔等於已證實收到 email。
- 排程分鐘為 02、07、12、17…57，避開整點。GitHub 可延遲／丟棄排程，**唔保證每 5 分鐘準時檢查或通知**。

## 建立及啟動（未登入時需你完成一次 GitHub 登入）

1. 登入 GitHub，建立 `lung-fung-pokemon-watch` **Public** repository，可加一個 README 作初始 commit。唔需選付費方案。
2. 將本文件夾內容放喺 repository 根目錄，包括 `.github/workflows/watch.yml`、`monitor.py`、`requirements.txt`、`data/state.json` 同 `tests/fixtures/`。初次上傳後唔好覆蓋 `data/state.json`：會失去歷史及待發通知。
3. Repository 右上角 **Watch → Custom → Issues → Apply**；GitHub **Settings → Notifications → Watching → Email**。保留其他原有設定。
4. 去 **Actions → Lung Fung Pokemon Watch → Run workflow**，第一次 `test_email` 保持 false，確認 run 綠色成功，同 `data/state.json` 有產品基準。
5. 再 Run workflow，勾選 `test_email`。系統只會建立一個標示 TEST 嘅 Issue；重跑亦唔重複。查看自己 email 收件匣／垃圾郵件，確認收到同一個 TEST Issue。
6. 等下一次 scheduled run 成功，先當作雲端排程已驗證。電腦關閉後，GitHub 雲端 workflow 仍可執行。

如果無法建立／寫入 workflow，應查 GitHub repository/app 權限或 Actions policy，唔係付款。唔好喺 public repository 或聊天貼任何登入密碼、token 或 email 認證資料。

## 本地測試

Python 3.12 或以上：

```text
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python monitor.py --dry-run
python monitor.py --local --state local-state.json
```

`--dry-run` 唔寫狀態、唔發通知。`--local` 只保存本地狀態，唔開啟雲端或 email。Cloud workflow 使用自動注入嘅 `GITHUB_TOKEN` 及 `GITHUB_REPOSITORY`。

## 判斷同可靠性

以品牌、產品分類、POKEMON／寶可夢搜尋及分頁發現產品；產品標題需同時有 Pokémon 名稱及卡牌／卡包／卡盒等 TCG 用語，排除 Pokémon 聯名眼膜等普通商品。只讀自己網店 domain 嘅產品頁。尊重即時 robots.txt，單次請求相隔至少一秒，有 GET 重試、頁數／產品數／時間上限。

價錢只讀商品主 summary 嘅現售價，唔讀原价 `del` 或相關產品。HK$ 標價按龍豐香港網店解讀；明示其他貨幣、價錢範圍、未知庫存唔當作確定值。商品主頁明示售完時優先，唔靠分類頁購物車按鈕推斷。

JSON 狀態保存於 `data/state.json`，只包含公開產品資料、事件 ID 同 Issue URL。先保存 pending outbox，再發通知，再逐筆保存 acknowledgment。GitHub Contents API 用 SHA 比較避免覆蓋並行寫入；workflow 全部 run 互斥。中斷後以 Issue body 事件 ID 查找，包括 closed issues，再重試，避免重複 Issue。唔好刪除 alert Issues 或重設 JSON。

未知讀數保留上次確認值，只供下次比較，唔稱為今次已確認。產品離開分類／主頁 404 唔當作售完。分類、HTTP 或解析失敗會令 run 失敗，唔重設歷史。未能確認庫存／价格時唔推斷變動。新發現只代表首次被此程式記錄，唔證明真正上架時間。缺貨待訂單獨標示，唔當作現貨。庫存只係網站顯示，唔係留貨。

無變化時最多每日更新一次 heartbeat，減少 commit；state 有變化及每次通知 acknowledgment 會即時保存。GitHub 官方對 60 日無 repository activity 嘅 public schedule 有停用規則，應定期確認 Actions 最近期成功紀錄；唔承諾永久無人管理。請避免開啟全 run 成功 email，否則每 5 分鐘會造成大量普通通知。

## 官方來源

- [龍豐品牌頁](https://eshop.lungfung.hk/brand/pokemon/)
- [龍豐產品分類](https://eshop.lungfung.hk/product-category/pokemon/)
- [GitHub 排程規則及限制](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [Public 標準 Actions runner 免費](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [GitHub Watch／Email 通知設定](https://docs.github.com/en/subscriptions-and-notifications/get-started/configuring-notifications)
