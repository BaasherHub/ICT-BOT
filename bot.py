import os
import asyncio
import logging
import time
from datetime import datetime, timezone
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ── Config ────────────────────────────────────────────────────────────────────
ICT_TIMEFRAMES   = ["15m", "1h"]
ICT_CANDLES      = 60          # candles to fetch per symbol per TF
SPIKE_TFS        = ["1m", "5m", "15m"]
SPIKE_THRESHOLD  = 3.0         # % move to trigger spike alert
MIN_VOLUME_USDT  = 5_000_000   # ignore low-volume coins (24h volume)
SCAN_INTERVAL    = 60          # seconds between full ICT scans
SPIKE_INTERVAL   = 15          # seconds between spike scans
MAX_CONCURRENT   = 10          # parallel requests
COOLDOWN_SEC     = 600         # don't re-alert same coin+signal within 10 min

BINANCE_BASE = "https://fapi.binance.com"

# ── Cooldown tracker ──────────────────────────────────────────────────────────
_cooldowns: dict[str, float] = {}

def is_cooled(key: str) -> bool:
    t = _cooldowns.get(key, 0)
    return time.time() - t < COOLDOWN_SEC

def set_cooldown(key: str):
    _cooldowns[key] = time.time()


# ── Binance Futures helpers ───────────────────────────────────────────────────
async def get_futures_symbols(client: httpx.AsyncClient) -> list[str]:
    r = await client.get(f"{BINANCE_BASE}/fapi/v1/exchangeInfo", timeout=20)
    data = r.json()
    syms = [
        s["symbol"] for s in data["symbols"]
        if s["status"] == "TRADING" and s["quoteAsset"] == "USDT"
    ]
    return syms

async def get_24h_volume(client: httpx.AsyncClient, symbols: list[str]) -> dict[str, float]:
    r = await client.get(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=20)
    tickers = r.json()
    return {t["symbol"]: float(t["quoteVolume"]) for t in tickers if t["symbol"] in symbols}

async def get_klines(client: httpx.AsyncClient, symbol: str, interval: str, limit: int = 60) -> list[dict]:
    r = await client.get(
        f"{BINANCE_BASE}/fapi/v1/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=15,
    )
    if r.status_code != 200:
        return []
    return [
        {"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
         "c": float(k[4]), "v": float(k[5])}
        for k in r.json()
    ]


# ── ICT Detection ─────────────────────────────────────────────────────────────
def detect_liquidity_sweep_mss(candles: list[dict]) -> dict | None:
    """
    Bearish: price sweeps above swing high (stop hunt) then MSS down.
    Bullish: price sweeps below swing low then MSS up.
    Returns signal dict or None.
    """
    n = len(candles)
    if n < 10:
        return None

    # Find recent swing high/low in last 20 candles
    window = candles[-20:-3]
    if not window:
        return None

    swing_high = max(c["h"] for c in window)
    swing_low  = min(c["l"] for c in window)

    last3 = candles[-3:]
    c0, c1, c2 = last3  # c2 = most recent closed candle

    # Bearish sweep: c1 wicks above swing high, c2 closes bearish and breaks below c0 low
    bearish_sweep = (
        c1["h"] > swing_high and        # swept the high
        c1["c"] < swing_high and        # closed back below (rejection)
        c2["c"] < c2["o"] and           # last candle bearish
        c2["c"] < c0["l"]               # MSS: broke prior structure low
    )

    # Bullish sweep: c1 wicks below swing low, c2 closes bullish and breaks above c0 high
    bullish_sweep = (
        c1["l"] < swing_low and
        c1["c"] > swing_low and
        c2["c"] > c2["o"] and
        c2["c"] > c0["h"]
    )

    if bearish_sweep:
        return {"type": "LIQ_SWEEP_MSS", "bias": "SHORT",
                "detail": f"Swept high ${swing_high:,.2f} → MSS bearish. Short bias.",
                "entry": c2["c"], "sweep_level": swing_high}
    if bullish_sweep:
        return {"type": "LIQ_SWEEP_MSS", "bias": "LONG",
                "detail": f"Swept low ${swing_low:,.2f} → MSS bullish. Long bias.",
                "entry": c2["c"], "sweep_level": swing_low}
    return None


def detect_order_block(candles: list[dict]) -> dict | None:
    """
    Bearish OB: last bullish candle before a strong drop, price retesting it now.
    Bullish OB: last bearish candle before a strong pump, price retesting now.
    """
    n = len(candles)
    if n < 8:
        return None

    current_price = candles[-1]["c"]

    # Look back for OB candidates in last 30 candles
    for i in range(n - 8, max(n - 30, 1), -1):
        ob = candles[i]
        # Check if a strong move followed this candle
        move_candles = candles[i+1:i+5]
        if not move_candles:
            continue

        move_pct = abs(move_candles[-1]["c"] - ob["c"]) / ob["c"] * 100

        if move_pct < 1.0:
            continue

        # Bearish OB: bullish candle before drop, price retesting from below
        if ob["c"] > ob["o"] and move_candles[-1]["c"] < ob["o"]:
            ob_low, ob_high = ob["l"], ob["h"]
            if ob_low <= current_price <= ob_high:
                return {"type": "ORDER_BLOCK", "bias": "SHORT",
                        "detail": f"Bearish OB retest ${ob_low:,.2f}–${ob_high:,.2f}. Rejection expected.",
                        "entry": current_price, "ob_low": ob_low, "ob_high": ob_high}

        # Bullish OB: bearish candle before pump, price retesting from above
        if ob["c"] < ob["o"] and move_candles[-1]["c"] > ob["o"]:
            ob_low, ob_high = ob["l"], ob["h"]
            if ob_low <= current_price <= ob_high:
                return {"type": "ORDER_BLOCK", "bias": "LONG",
                        "detail": f"Bullish OB retest ${ob_low:,.2f}–${ob_high:,.2f}. Bounce expected.",
                        "entry": current_price, "ob_low": ob_low, "ob_high": ob_high}

    return None


def detect_fvg(candles: list[dict]) -> dict | None:
    """
    Fair Value Gap: 3-candle imbalance, price now filling the gap.
    """
    n = len(candles)
    if n < 6:
        return None

    current_price = candles[-1]["c"]

    for i in range(n - 5, max(n - 25, 1), -1):
        p, mid, nx = candles[i-1], candles[i], candles[i+1]

        # Bullish FVG: gap between prev high and next low
        if mid["c"] > mid["o"]:
            fvg_lo, fvg_hi = p["h"], nx["l"]
            if fvg_hi > fvg_lo:
                gap_pct = (fvg_hi - fvg_lo) / mid["c"] * 100
                if gap_pct > 0.1 and fvg_lo <= current_price <= fvg_hi:
                    return {"type": "FVG", "bias": "LONG",
                            "detail": f"Bullish FVG fill ${fvg_lo:,.2f}–${fvg_hi:,.2f} ({gap_pct:.2f}% gap). Bounce setup.",
                            "entry": current_price, "fvg_lo": fvg_lo, "fvg_hi": fvg_hi}

        # Bearish FVG: gap between prev low and next high
        if mid["c"] < mid["o"]:
            fvg_hi, fvg_lo = p["l"], nx["h"]
            if fvg_hi > fvg_lo:
                gap_pct = (fvg_hi - fvg_lo) / mid["c"] * 100
                if gap_pct > 0.1 and fvg_lo <= current_price <= fvg_hi:
                    return {"type": "FVG", "bias": "SHORT",
                            "detail": f"Bearish FVG fill ${fvg_lo:,.2f}–${fvg_hi:,.2f} ({gap_pct:.2f}% gap). Short setup.",
                            "entry": current_price, "fvg_lo": fvg_lo, "fvg_hi": fvg_hi}

    return None


def detect_spike(candles_1m: list[dict], candles_5m: list[dict], candles_15m: list[dict]) -> dict | None:
    """
    Detects sudden 3%+ move on 1m, 5m, or 15m.
    """
    checks = [
        ("1m",  candles_1m,  3),
        ("5m",  candles_5m,  2),
        ("15m", candles_15m, 2),
    ]
    for tf, candles, lookback in checks:
        if len(candles) < lookback + 1:
            continue
        recent = candles[-(lookback+1):]
        base   = recent[0]["o"]
        high   = max(c["h"] for c in recent[1:])
        low    = min(c["l"] for c in recent[1:])
        last_c = recent[-1]["c"]

        pump_pct = (high - base) / base * 100
        dump_pct = (base - low)  / base * 100

        if pump_pct >= SPIKE_THRESHOLD:
            return {"type": "SPIKE", "bias": "SHORT",
                    "tf": tf, "pct": pump_pct,
                    "detail": f"PUMP +{pump_pct:.1f}% on {tf} — spike short opportunity",
                    "entry": last_c, "spike_high": high}

        if dump_pct >= SPIKE_THRESHOLD:
            return {"type": "SPIKE", "bias": "LONG",
                    "tf": tf, "pct": dump_pct,
                    "detail": f"DUMP -{dump_pct:.1f}% on {tf} — potential reversal long",
                    "entry": last_c, "spike_low": low}

    return None


# ── Telegram ──────────────────────────────────────────────────────────────────
SIGNAL_EMOJI = {
    "LIQ_SWEEP_MSS": "🌊",
    "ORDER_BLOCK":   "🧱",
    "FVG":           "📐",
    "SPIKE":         "⚡",
}
BIAS_EMOJI = {"SHORT": "🔴", "LONG": "🟢"}

async def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for chunk in chunks:
            await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "disable_web_page_preview": True,
            })
            await asyncio.sleep(0.3)

def format_signal(symbol: str, tf: str, signal: dict) -> str:
    sig_emoji  = SIGNAL_EMOJI.get(signal["type"], "📊")
    bias_emoji = BIAS_EMOJI.get(signal["bias"], "⚪")
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")

    lines = [
        f"{sig_emoji} {signal['type'].replace('_',' ')} — {symbol}",
        f"{bias_emoji} Bias: {signal['bias']}  |  TF: {tf}  |  {now}",
        f"",
        f"Entry: ${signal['entry']:,.4f}",
        f"Detail: {signal['detail']}",
    ]

    if signal["type"] == "SPIKE":
        lines.append(f"Move: {signal['pct']:.1f}% on {signal['tf']}")

    elif signal["type"] == "LIQ_SWEEP_MSS":
        lines.append(f"Swept: ${signal['sweep_level']:,.4f}")

    elif signal["type"] == "ORDER_BLOCK":
        lines.append(f"OB Zone: ${signal['ob_low']:,.4f} – ${signal['ob_high']:,.4f}")

    elif signal["type"] == "FVG":
        lines.append(f"Gap: ${signal['fvg_lo']:,.4f} – ${signal['fvg_hi']:,.4f}")

    lines += [
        f"",
        f"⚠️ DYOR — not financial advice",
    ]
    return "\n".join(lines)


# ── Scanner tasks ─────────────────────────────────────────────────────────────
sem = asyncio.Semaphore(MAX_CONCURRENT)

async def scan_symbol_ict(client: httpx.AsyncClient, symbol: str):
    signals = []
    for tf in ICT_TIMEFRAMES:
        try:
            async with sem:
                candles = await get_klines(client, symbol, tf, ICT_CANDLES)
            if len(candles) < 15:
                continue

            for detector in [detect_liquidity_sweep_mss, detect_order_block, detect_fvg]:
                sig = detector(candles)
                if sig:
                    key = f"{symbol}:{tf}:{sig['type']}:{sig['bias']}"
                    if not is_cooled(key):
                        set_cooldown(key)
                        signals.append((symbol, tf, sig))
        except Exception as e:
            log.debug(f"ICT scan error {symbol} {tf}: {e}")
    return signals


async def scan_symbol_spike(client: httpx.AsyncClient, symbol: str):
    try:
        async with sem:
            c1, c5, c15 = await asyncio.gather(
                get_klines(client, symbol, "1m",  10),
                get_klines(client, symbol, "5m",  10),
                get_klines(client, symbol, "15m", 10),
            )
        sig = detect_spike(c1, c5, c15)
        if sig:
            key = f"{symbol}:spike:{sig['bias']}"
            if not is_cooled(key):
                set_cooldown(key)
                return (symbol, sig["tf"], sig)
    except Exception as e:
        log.debug(f"Spike scan error {symbol}: {e}")
    return None


async def ict_scan_loop(symbols: list[str]):
    log.info(f"ICT scan loop started — {len(symbols)} symbols, TFs: {ICT_TIMEFRAMES}")
    while True:
        start = time.time()
        batch_signals = []
        async with httpx.AsyncClient(timeout=20) as client:
            tasks = [scan_symbol_ict(client, s) for s in symbols]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, list):
                batch_signals.extend(res)

        log.info(f"ICT scan done — {len(batch_signals)} signals found in {time.time()-start:.1f}s")

        for symbol, tf, sig in batch_signals:
            msg = format_signal(symbol, tf, sig)
            await send_telegram(msg)
            await asyncio.sleep(0.5)

        elapsed = time.time() - start
        await asyncio.sleep(max(0, SCAN_INTERVAL - elapsed))


async def spike_scan_loop(symbols: list[str]):
    log.info(f"Spike scan loop started — {len(symbols)} symbols")
    while True:
        start = time.time()
        async with httpx.AsyncClient(timeout=20) as client:
            tasks = [scan_symbol_spike(client, s) for s in symbols]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        spikes = [r for r in results if r and not isinstance(r, Exception)]
        log.info(f"Spike scan done — {len(spikes)} spikes in {time.time()-start:.1f}s")

        for symbol, tf, sig in spikes:
            msg = format_signal(symbol, tf, sig)
            await send_telegram(msg)
            await asyncio.sleep(0.3)

        elapsed = time.time() - start
        await asyncio.sleep(max(0, SPIKE_INTERVAL - elapsed))


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    log.info("Fetching Binance Futures symbols...")
    async with httpx.AsyncClient(timeout=20) as client:
        symbols = await get_futures_symbols(client)
        volumes = await get_24h_volume(client, symbols)

    # Filter by minimum volume
    symbols = [s for s in symbols if volumes.get(s, 0) >= MIN_VOLUME_USDT]
    symbols.sort(key=lambda s: volumes.get(s, 0), reverse=True)
    log.info(f"Scanning {len(symbols)} symbols with >${MIN_VOLUME_USDT/1e6:.0f}M 24h volume")

    await send_telegram(
        f"ICT Signal Bot started\n"
        f"Scanning {len(symbols)} Binance Futures pairs\n"
        f"ICT: {', '.join(ICT_TIMEFRAMES)} | Spike: 3%+ on 1m/5m/15m\n"
        f"Signals: Liq Sweep+MSS, Order Block, FVG, Spikes"
    )

    await asyncio.gather(
        ict_scan_loop(symbols),
        spike_scan_loop(symbols),
    )

if __name__ == "__main__":
    asyncio.run(main())
