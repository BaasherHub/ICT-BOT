# ICT Signal Bot

Scans all Binance Futures pairs and sends Telegram alerts for:
- Liquidity Sweep + Market Structure Shift
- Order Block retests
- Fair Value Gap fills
- Sudden spike pumps/dumps (3%+ on 1m/5m/15m)

## Signals look like this

```
🌊 LIQ SWEEP MSS — BTCUSDT
🔴 Bias: SHORT  |  TF: 1h  |  14:32 UTC

Entry: $83,421.00
Detail: Swept high $84,200.00 → MSS bearish. Short bias.
Swept: $84,200.00

⚠️ DYOR — not financial advice
```

```
⚡ SPIKE — SOLUSDT
🔴 Bias: SHORT  |  TF: 1m  |  15:04 UTC

Entry: $142.30
Detail: PUMP +3.8% on 1m — spike short opportunity
Move: 3.8% on 1m

⚠️ DYOR — not financial advice
```

## Deploy on Railway

1. Push these 3 files to a GitHub repo
2. Railway → New Project → Deploy from GitHub
3. Add environment variables:

| Variable          | Value                  |
|-------------------|------------------------|
| TELEGRAM_TOKEN    | Your bot token         |
| TELEGRAM_CHAT_ID  | Your chat ID           |

## Config (edit in bot.py)

| Setting            | Default  | Description                        |
|--------------------|----------|------------------------------------|
| ICT_TIMEFRAMES     | 15m, 1h  | Timeframes for ICT signals         |
| SPIKE_THRESHOLD    | 3.0%     | % move to trigger spike alert      |
| MIN_VOLUME_USDT    | $5M      | Skip low-volume coins              |
| SCAN_INTERVAL      | 60s      | Full ICT scan frequency            |
| SPIKE_INTERVAL     | 15s      | Spike scan frequency               |
| COOLDOWN_SEC       | 600s     | Re-alert cooldown per signal       |
