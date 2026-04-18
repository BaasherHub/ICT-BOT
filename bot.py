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
SPIKE_THRESHOLD  = 3.0        # % move to trigger alert
MIN_VOLUME_USDT  = 5_000_000  # ignore low-volume coins
SPIKE_INTERVAL   = 15         # seconds between scans
MAX_CONCURRENT   = 10
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
            "Your Railway region may be blocked. Set env BINANCE_FAPI_BASE to a proxy."
        )
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

async def get_24h_volume(client: httpx.AsyncClient, symbols: list[str]) -> dict[str, float]:
    path = "/fapi/v1/ticker/24hr"
    r = await client.get(f"{BINANCE_BASE}{path}", timeout=20)
    _raise_if_error(r, path)
    return {t["symbol"]: float(t["quoteVolume"]) for t in r.json() if t["symbol"] in symbols}

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

async def scan_spike(client: httpx.AsyncClient, symbol: str) -> dict | None:
    try:
        async with sem:
            c1, c5, c15 = await asyncio.gather(
                get_klines(client, symbol, "1m",  5),
                get_klines(client, symbol, "5m",  4),
                get_klines(client, symbol, "15m", 3),
            )

        for tf, candles, lookback in [("1m", c1, 3), ("5m", c5, 2), ("15m", c15, 2)]:
            if len(candles) < lookback + 1:
                continue

            recent   = candles[-(lookback + 1):]
            base     = recent[0]["o"]
            high     = max(c["h"] for c in recent[1:])
            low      = min(c["l"] for c in recent[1:])
            last_c   = recent[-1]["c"]
            pump_pct = (high  - base) / base * 100
            dump_pct = (base  - low)  / base * 100

            if pump_pct >= SPIKE_THRESHOLD:
                key = f"{symbol}:pump:{tf}"
                if not is_cooled(key):
                    set_cooldown(key)
                    return {"symbol": symbol, "bias": "SHORT", "tf": tf,
                            "pct": pump_pct, "entry": last_c, "dir": "PUMP",
                            "high": high}

            if dump_pct >= SPIKE_THRESHOLD:
                key = f"{symbol}:dump:{tf}"
                if not is_cooled(key):
                    set_cooldown(key)
                    return {"symbol": symbol, "bias": "LONG", "tf": tf,
                            "pct": dump_pct, "entry": last_c, "dir": "DUMP",
                            "low": low}

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
async def spike_loop(symbols: list[str]):
    log.info(f"Spike loop started — {len(symbols)} symbols")
    symbols_ts = time.time()

    while True:
        start = time.time()

        # Refresh symbol list every hour
        if time.time() - symbols_ts > 3600:
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    new_syms = await get_futures_symbols(client)
                    vols     = await get_24h_volume(client, new_syms)
                symbols   = [s for s in new_syms if vols.get(s, 0) >= MIN_VOLUME_USDT]
                symbols.sort(key=lambda s: vols.get(s, 0), reverse=True)
                symbols_ts = time.time()
                log.info(f"Symbols refreshed: {len(symbols)}")
            except Exception as e:
                log.warning(f"Symbol refresh failed: {e}")

        async with httpx.AsyncClient(timeout=15) as client:
            tasks   = [scan_spike(client, s) for s in symbols]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        spikes = [r for r in results if r and not isinstance(r, Exception)]
        log.info(f"Spike scan done — {len(spikes)} spikes in {time.time()-start:.1f}s")

        for sig in spikes:
            await send_telegram(format_spike(sig))
            await asyncio.sleep(0.3)

        await asyncio.sleep(max(0, SPIKE_INTERVAL - (time.time() - start)))


async def main():
    log.info("Fetching Binance Futures symbols...")
    async with httpx.AsyncClient(timeout=20) as client:
        symbols = await get_futures_symbols(client)
        volumes = await get_24h_volume(client, symbols)

    symbols = [s for s in symbols if volumes.get(s, 0) >= MIN_VOLUME_USDT]
    symbols.sort(key=lambda s: volumes.get(s, 0), reverse=True)
    log.info(f"Loaded {len(symbols)} symbols (>{MIN_VOLUME_USDT/1e6:.0f}M vol)")

    await send_telegram(
        f"Spike Bot started\n"
        f"Scanning {len(symbols)} Binance Futures pairs\n"
        f"Threshold: {SPIKE_THRESHOLD}%+ move on 1m / 5m / 15m\n"
        f"Scan every {SPIKE_INTERVAL}s — cooldown {COOLDOWN_SEC//60}min per alert"
    )

    await spike_loop(symbols)


if __name__ == "__main__":
    asyncio.run(main())
