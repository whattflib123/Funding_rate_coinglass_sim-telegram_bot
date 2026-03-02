# Funding Rate Telegram Alert (BTCUSDT)

用公開 API 近似 CoinGlass 觀察方式：
- 多交易所資金費率（Binance + Bybit + OKX）
- OI 可取得時用 OI 權重，否則退化平均
- 8 小時週期（UTC）
- 上方 K 線（綠漲紅跌）+ 下方資金費率柱狀圖（正綠負紅）
- 當最新 funding `< 0` 時，透過 Telegram 發送「文字 + 圖片」

## 專案結構

```text
funding-rate-telegram-alert/
├─ src/
│  └─ funding_alert.py
├─ assets/
│  ├─ runtime_real_multi_8h.png
│  └─ run_output.txt
├─ .env.example
├─ .gitignore
├─ requirements.txt
└─ README.md
```

## 安裝

```bash
pip install -r requirements.txt
```

## 設定 Telegram

1. 複製環境檔：

```bash
cp .env.example .env
```

2. 編輯 `.env`：

```env
TELEGRAM_BOT_TOKEN=你的bot token
TELEGRAM_CHAT_ID=你的chat id
```

## 執行

### 一般執行（不通知）

```bash
python3 src/funding_alert.py --out assets/runtime_real_multi_8h.png
```

### 啟用通知（最新 funding < 0 才送）

```bash
python3 src/funding_alert.py --notify --out assets/runtime_real_multi_8h.png
```

## Cron 每 8 小時執行一次（UTC）

```cron
CRON_TZ=UTC
0 */8 * * * /usr/bin/python3 /path/to/funding-rate-telegram-alert/src/funding_alert.py --notify --out /path/to/funding-rate-telegram-alert/assets/runtime_real_multi_8h.png >> /path/to/funding-rate-telegram-alert/cron.log 2>&1
```

## 實際執行畫面

### 1) 實際產生圖表

![runtime chart](assets/runtime_real_multi_8h.png)

### 2) 實際終端輸出（節錄）

來源檔案：`assets/run_output.txt`

```text
BTCUSDT funding history (8h, UTC) - REAL (Binance+Bybit+OKX, OI-weighted where available)
timestamp (UTC)     close        funding_rate     percent
2026-02-10 00:00      68882.10   -0.00002502    -0.00250%
2026-02-10 08:00      69251.50   -0.00002666    -0.00267%
...
2026-03-02 00:00      66541.30   -0.00000036    -0.00004%

PNG saved to: assets/runtime_real_multi_8h.png
```

## 上傳 GitHub

```bash
git init
git add .
git commit -m "Initial commit: funding rate telegram alert"
git branch -M main
git remote add origin <your-repo-url>
git push -u origin main
```
