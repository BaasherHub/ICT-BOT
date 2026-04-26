import os
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ── Config ────────────────────────────────────────────────────────────────────
SPIKE_THRESHOLD  = 5.0        # % mid-candle move to trigger alert
PRE_FILTER_PCT   = 2.0        # skip coins with <2% 24h change
MIN_VOLUME_USDT  = 5_000_000  # ignore low-volume coins
SCAN_INTERVAL    = 30         # seconds between scans
MAX_CONCURRENT   = 5          # parallel kline fetches
COOLDOWN_SEC     = 600        # 10 min cooldown per coin per direction

BINANCE_BASE = os.environ.get("BINANCE_FAPI_BASE", "https://fapi.binance.com").rstrip("/")

# ── Cooldown ──────────────────────────────────────────────────────────────────
_cooldowns: dict[str, float] = {}

def is_cooled(key: str) -> bool:
    return time.time() - _cooldowns.get(key, 0) < COOLDOWN_SEC

def set_cooldown(key: str):
    _cooldowns[key] = time.time()

# ── Binance helpers ───────────────────────────────────────────────────────────
def _raise_if_error(r: httpx.Response, path: str):
    if r.status_code == 451:
        raise RuntimeError(
            f"Binance returned HTTP 451 for {path}. "
            "Set env BINANCE_FAPI_BASE to a proxy."
        )
    if r.status_code != 200:
        raise RuntimeError(f"Binance HTTP {r.status_code} for {path}: {r.text[:200]}")

async def get_futures_symbols(client: httpx.AsyncClient) -> set[str]:
    path = "/fapi/v1/exchangeInfo"
    r = await client.get(f"{BINANCE_BASE}{path}", timeout=20)
    _raise_if_error(r, path)
    return {
        s["symbol"] for s in r.json()["symbols"]
        if s["status"] == "TRADING" and s["quoteAsset"] == "USDT"
    }

async def get_bulk_tickers(client: httpx.AsyncClient) -> list[dict]:
    path = "/fapi/v1/ticker/24hr"
    r = await client.get(f"{BINANCE_BASE}{path}", timeout=20)
    _raise_if_error(r, path)
    return r.json()

async def get_5m_klines(client: httpx.AsyncClient, symbol: str) -> list[dict]:
    r = await client.get(
        f"{BINANCE_BASE}/fapi/v1/klines",
        params={"symbol": symbol, "interval": "5m", "limit": 3},
        timeout=15,
    )
    if r.status_code != 200:
        return []
    return [
        {"o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])}
        for k in r.json()
    ]

# ── Spike detection ───────────────────────────────────────────────────────────
sem = asyncio.Semaphore(MAX_CONCURRENT)

async def scan_symbol(client: httpx.AsyncClient, symbol: str) -> dict | None:
    try:
        async with sem:
            candles = await get_5m_klines(client, symbol)

        if len(candles) < 2:
            return None

        current = candles[-1]
        open_   = current["o"]
        high    = current["h"]
        low     = current["l"]
        close   = current["c"]

        pump_pct = (high  - open_) / open_ * 100
        dump_pct = (open_ - low)   / open_ * 100

        if pump_pct >= SPIKE_THRESHOLD:
            key = f"{symbol}:pump"
            if not is_cooled(key):
                set_cooldown(key)
                return {"symbol": symbol, "dir": "PUMP", "bias": "SHORT",
                        "pct": pump_pct, "open": open_, "extreme": high, "current": close}

        if dump_pct >= SPIKE_THRESHOLD:
            key = f"{symbol}:dump"
            if not is_cooled(key):
                set_cooldown(key)
                return {"symbol": symbol, "dir": "DUMP", "bias": "LONG",
                        "pct": dump_pct, "open": open_, "extreme": low, "current": close}

    except Exception as e:
        log.debug(f"Scan error {symbol}: {e}")
    return None

# ── Telegram ──────────────────────────────────────────────────────────────────
async def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            await asyncio.sleep(0.3)

def format_signal(sig: dict) -> str:
    dubai = datetime.now(timezone.utc) + timedelta(hours=4)
    ts    = dubai.strftime("%H:%M:%S Dubai")
    be    = "🔴" if sig["bias"] == "SHORT" else "🟢"

    if sig["dir"] == "PUMP":
        action        = "Fade the pump — consider SHORT"
        extreme_label = "Spike high"
    else:
        action        = "Fade the dump — consider LONG"
        extreme_label = "Spike low"

    return (
        f"⚡ <b>SPIKE {sig['dir']} — {sig['symbol']}</b>\n"
        f"{be} <b>{sig['bias']}</b>  |  5m  |  {ts}\n"
        f"\n"
        f"Move:         <b>+{sig['pct']:.2f}%</b> from candle open\n"
        f"Candle open:  ${sig['open']:,.4f}\n"
        f"{extreme_label}:    ${sig['extreme']:,.4f}\n"
        f"Current:      ${sig['current']:,.4f}\n"
        f"\n"
        f"💡 {action}\n"
        f"\n"
        f"<i>DYOR — not financial advice</i>"
    )

# ── Main loop ─────────────────────────────────────────────────────────────────
async def main():
    log.info("Spike Bot starting...")

    all_symbols: set[str] = set()
    symbols_ts: float     = 0

    await send_telegram(
        f"⚡ <b>Spike Bot started</b>\n"
        f"Timeframe: 5m (mid-candle detection)\n"
        f"Threshold: {SPIKE_THRESHOLD}%+ move from candle open\n"
        f"Scan every: {SCAN_INTERVAL}s\n"
        f"Cooldown: {COOLDOWN_SEC//60}min per alert"
    )

    while True:
        start = time.time()

        try:
            async with httpx.AsyncClient(timeout=20) as client:

                if time.time() - symbols_ts > 3600:
                    all_symbols = await get_futures_symbols(client)
                    symbols_ts  = time.time()
                    log.info(f"Symbols refreshed: {len(all_symbols)}")

                tickers = await get_bulk_tickers(client)

            candidates = [
                t["symbol"] for t in tickers
                if t["symbol"] in all_symbols
                and float(t.get("quoteVolume", 0)) >= MIN_VOLUME_USDT
                and abs(float(t.get("priceChangePercent", 0))) >= PRE_FILTER_PCT
            ]

            log.info(f"Candidates: {len(candidates)} / {len(tickers)}")

            async with httpx.AsyncClient(timeout=15) as client:
                tasks   = [scan_symbol(client, s) for s in candidates]
                results = await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            log.warning(f"Loop error: {e}")
            await asyncio.sleep(SCAN_INTERVAL)
            continue

        spikes = [r for r in results if r and not isinstance(r, Exception)]
        log.info(f"Spike scan done — {len(spikes)} spikes in {time.time()-start:.1f}s")

        for sig in spikes:
            await send_telegram(format_signal(sig))
            await asyncio.sleep(0.3)

        await asyncio.sleep(max(0, SCAN_INTERVAL - (time.time() - start)))


if __name__ == "__main__":
    asyncio.run(main())
