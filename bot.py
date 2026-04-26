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
SPIKE_THRESHOLD  = 5.0        # % move to trigger alert (based on 24h priceChangePercent)
MIN_VOLUME_USDT  = 5_000_000  # ignore low-volume coins
SPIKE_INTERVAL   = 30         # seconds between scans (was 15 — doubled to halve egress)
MAX_CONCURRENT   = 5          # parallel kline fetches (reduced from 10)
COOLDOWN_SEC     = 600        # 10 min cooldown per coin per direction
PRE_FILTER_PCT   = 2.0        # only fetch klines if 24h change > this % (bulk filter)

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
        raise RuntimeError(f"Binance returned HTTP 451 for {path}. Set env BINANCE_FAPI_BASE to a proxy.")
    if r.status_code != 200:
        raise RuntimeError(f"Binance HTTP {r.status_code} for {path}: {r.text[:200]}")

async def get_futures_symbols(client: httpx.AsyncClient) -> list[str]:
    path = "/fapi/v1/exchangeInfo"
    r = await client.get(f"{BINANCE_BASE}{path}", timeout=20)
    _raise_if_error(r, path)
    return [
        s["symbol"] for s in r.json()["symbols"]
        if s["status"] == "TRADING" and s["quoteAsset"] == "USDT"
    ]

async def get_bulk_tickers(client: httpx.AsyncClient) -> list[dict]:
    """
    ONE request for ALL symbols — returns priceChangePercent, quoteVolume, lastPrice.
    This replaces per-symbol kline fetches as a pre-filter.
    """
    path = "/fapi/v1/ticker/24hr"
    r = await client.get(f"{BINANCE_BASE}{path}", timeout=20)
    _raise_if_error(r, path)
    return r.json()

async def get_klines(client: httpx.AsyncClient, symbol: str, interval: str, limit: int) -> list[dict]:
    r = await client.get(
        f"{BINANCE_BASE}/fapi/v1/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
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

async def scan_spike_klines(client: httpx.AsyncClient, symbol: str, rough_pct: float) -> dict | None:
    """
    Only called for coins already passing the bulk pre-filter.
    Fetches klines on 1m only (was 1m+5m+15m — 3x fewer requests).
    """
    try:
        async with sem:
            c1 = await get_klines(client, symbol, "1m", 5)

        if len(c1) < 4:
            return None

        base   = c1[0]["o"]
        high   = max(c["h"] for c in c1[1:])
        low    = min(c["l"] for c in c1[1:])
        last_c = c1[-1]["c"]

        pump_pct = (high - base) / base * 100
        dump_pct = (base - low)  / base * 100

        if pump_pct >= SPIKE_THRESHOLD:
            key = f"{symbol}:pump:1m"
            if not is_cooled(key):
                set_cooldown(key)
                return {"symbol": symbol, "bias": "SHORT", "tf": "1m",
                        "pct": pump_pct, "entry": last_c, "dir": "PUMP", "high": high}

        if dump_pct >= SPIKE_THRESHOLD:
            key = f"{symbol}:dump:1m"
            if not is_cooled(key):
                set_cooldown(key)
                return {"symbol": symbol, "bias": "LONG", "tf": "1m",
                        "pct": dump_pct, "entry": last_c, "dir": "DUMP", "low": low}

    except Exception as e:
        log.debug(f"Spike scan error {symbol}: {e}")
    return None

# ── Telegram ──────────────────────────────────────────────────────────────────
async def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "disable_web_page_preview": True,
            })
            await asyncio.sleep(0.3)

def format_spike(sig: dict) -> str:
    be    = "🔴" if sig["bias"] == "SHORT" else "🟢"
    dubai = datetime.now(timezone.utc) + timedelta(hours=4)
    ts    = dubai.strftime("%H:%M Dubai")

    if sig["dir"] == "PUMP":
        action  = "SHORT opportunity — consider fading the spike"
        extreme = f"Spike high: ${sig['high']:,.4f}"
    else:
        action  = "LONG opportunity — potential reversal"
        extreme = f"Spike low:  ${sig['low']:,.4f}"

    return "\n".join([
        f"⚡ SPIKE {sig['dir']} — {sig['symbol']}",
        f"{be} {sig['bias']}  |  {sig['tf']}  |  {ts}",
        f"",
        f"Move:   +{sig['pct']:.1f}% on {sig['tf']}",
        f"Entry:  ${sig['entry']:,.4f}",
        f"{extreme}",
        f"",
        f"Action: {action}",
        f"",
        f"DYOR — not financial advice",
    ])

# ── Main loop ─────────────────────────────────────────────────────────────────
async def main():
    log.info("Spike Bot (optimized) starting...")

    await send_telegram(
        f"Spike Bot started (optimized)\n"
        f"Threshold: {SPIKE_THRESHOLD}%+ move\n"
        f"Pre-filter: only fetch klines if 24h change > {PRE_FILTER_PCT}%\n"
        f"Scan every {SPIKE_INTERVAL}s — cooldown {COOLDOWN_SEC//60}min per alert"
    )

    symbols_ts = 0
    all_symbols: set[str] = set()

    while True:
        start = time.time()

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                # Refresh allowed symbol list hourly
                if time.time() - symbols_ts > 3600:
                    all_symbols = set(await get_futures_symbols(client))
                    symbols_ts  = time.time()
                    log.info(f"Symbols refreshed: {len(all_symbols)}")

                # ONE bulk request — replaces N*3 kline requests per cycle
                tickers = await get_bulk_tickers(client)

            # Pre-filter: only candidates with sufficient volume AND recent move
            candidates = [
                t for t in tickers
                if t["symbol"] in all_symbols
                and float(t.get("quoteVolume", 0)) >= MIN_VOLUME_USDT
                and abs(float(t.get("priceChangePercent", 0))) >= PRE_FILTER_PCT
            ]
            log.info(f"Pre-filter: {len(candidates)} candidates from {len(tickers)} symbols")

            # Now fetch klines only for pre-filtered candidates
            async with httpx.AsyncClient(timeout=15) as client:
                tasks   = [scan_spike_klines(client, t["symbol"], float(t["priceChangePercent"])) for t in candidates]
                results = await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            log.warning(f"Scan error: {e}")
            await asyncio.sleep(SPIKE_INTERVAL)
            continue

        spikes = [r for r in results if r and not isinstance(r, Exception)]
        log.info(f"Spike scan done — {len(spikes)} spikes in {time.time()-start:.1f}s")

        for sig in spikes:
            await send_telegram(format_spike(sig))
            await asyncio.sleep(0.3)

        await asyncio.sleep(max(0, SPIKE_INTERVAL - (time.time() - start)))


if __name__ == "__main__":
    asyncio.run(main())
