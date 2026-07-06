# price-tracker

機票（特定航班）＋飯店（特定日期／房型）價格追蹤器。每天 21:10（台北）由 GitHub Actions 自動執行，價格寫進 `data/prices.csv`，達警報條件推播到 iPhone（ntfy）。**電腦不用開機。**

## 新增/修改追蹤目標

改 `watchlist.json` 後 push 即可。欄位：

| 欄位 | 說明 |
|------|------|
| `type` | `flight` 或 `hotel` |
| `enabled` | `false` = 暫停（不用刪掉） |
| `threshold` | 低於此價推播 |
| `every_n_days` | SerpAPI 查詢頻率（免費額度 100 查詢/月，額度由 `data/state.json` 自動記帳，超過 `serpapi_monthly_budget` 自動停查） |
| `room_keyword` | regex，只追蹤名稱符合的房型/方案，例：`"運河|Canal"`、`"和室"`。房型明細主要靠樂天來源 |
| `flight_numbers` | 只記含這些航班號的行程，例：`["BR116","BR115"]` |
| `rakuten_hotel_no` | 樂天飯店編號；留 `null` 會自動用飯店名搜尋並快取 |

警報條件（`settings`）：低於 `threshold`／30 天新低／單日跌幅 ≥ `drop_alert_pct`%。同目標 3 天冷卻避免洗版。

## Secrets（repo Settings → Secrets and variables → Actions）

| Secret | 用途 | 必要性 |
|--------|------|--------|
| `SERPAPI_KEY` | Google Flights / Google Hotels（各 OTA 比價） | 機票必要；飯店建議 |
| `NTFY_TOPIC` | iPhone 推播 topic | 要推播就必要 |
| `RAKUTEN_APP_ID` | 樂天 Travel（免費、日本飯店房型/方案明細） | 要追「特定房型」建議設 |

## 手動跑一次

GitHub → Actions → track prices → Run workflow。本機測試：

```bash
SERPAPI_KEY=xxx NTFY_TOPIC=xxx python3 tracker.py
```

## 走勢圖

<https://aesthetic-baklava-7492d5.netlify.app/price-tracker.html>（直接讀本 repo 的 CSV，天天自動更新，不吃 Netlify 部署額度）
